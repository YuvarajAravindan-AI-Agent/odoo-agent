"""Refund gate: agent proposes, OPA (the real policy/ in a local `opa run`) decides, a human approves.

Skips when the opa binary is not installed. Odoo is an in-memory fake of the few account.move calls the tool makes.
"""
import shutil
import socket
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
import requests

from app.agent import Agent
from app.policy import Policy
from app.store import Store

TODAY = date(2026, 10, 18)
POLICY_DIR = Path(__file__).resolve().parent.parent / "policy"


class FakeOdoo:
    def __init__(self):
        self.moves: dict[int, dict] = {}
        self.wizards: dict[int, list[int]] = {}
        self.writes = []

    def add_invoice(self, amount, age, paid=True, partner="Northwind Traders"):
        i = len(self.moves) + 1
        self.moves[i] = {"id": i, "name": f"INV/2026/{i:05d}", "partner_id": [1, partner], "move_type": "out_invoice",
                         "state": "posted", "payment_state": "paid" if paid else "not_paid", "amount_total": amount,
                         "invoice_date": (TODAY - timedelta(days=age)).isoformat(), "reversed_entry_id": False,
                         "journal_id": [1, "Sales"]}
        return i

    def ping(self):
        return True, "ok"

    @staticmethod
    def _match(row, cond):
        f, op, v = cond
        x = row.get(f)
        if isinstance(x, list):  # many2one [id, name]
            x = x[0]
        return {"=": lambda: x == v, "in": lambda: x in v, ">=": lambda: x is not False and x >= v}[op]()

    def search_read(self, model, domain, fields, limit=20, order=""):
        assert model == "account.move", model
        rows = [r for r in self.moves.values() if all(self._match(r, c) for c in domain)]
        return [dict(r) for r in sorted(rows, key=lambda r: -r["id"])[:limit]]

    def call(self, model, method, args, kwargs=None):
        self.writes.append((model, method))
        if (model, method) == ("account.move.reversal", "create"):
            wid = len(self.wizards) + 1
            self.wizards[wid] = args[0]["move_ids"][0][2]
            return wid
        if (model, method) == ("account.move.reversal", "refund_moves"):
            for inv_id in self.wizards[args[0][0]]:
                inv = self.moves[inv_id]
                i = len(self.moves) + 1
                self.moves[i] = {**inv, "id": i, "name": "/", "move_type": "out_refund", "state": "draft",
                                 "payment_state": "not_paid", "invoice_date": TODAY.isoformat(),
                                 "reversed_entry_id": [inv_id, inv["name"]]}
            return {}
        if (model, method) == ("account.move", "action_post"):
            for i in args[0]:
                self.moves[i].update(state="posted", name=f"RINV/2026/{i:05d}")
            return True
        raise AssertionError(f"unexpected write {model}.{method}")


@pytest.fixture(scope="module")
def opa_url():
    exe = shutil.which("opa")
    if not exe:
        pytest.skip("opa binary not installed")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([exe, "run", "--server", "--addr", f"127.0.0.1:{port}", "--authorization=basic", "--ignore", "*_test.rego",
                             "--set", "decision_logs.console=true", str(POLICY_DIR)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if requests.get(base + "/health", timeout=0.5).ok:
                break
        except requests.RequestException:
            time.sleep(0.05)
    yield base + "/v1/data/refunds/decision"
    proc.terminate()
    proc.wait(5)


def scripted(*steps):
    it = iter(steps)
    return lambda body: {"candidates": [{"content": {"role": "model", "parts": next(it)}}]}


def call(tool, /, **args):
    return {"functionCall": {"name": tool, "args": args}}


DONE = [{"text": "ok"}]


@pytest.fixture
def env(tmp_path, opa_url):
    odoo = FakeOdoo()
    store = Store(str(tmp_path / "t.db"))

    def build(gen, url=opa_url):
        return Agent(odoo, store, gen, ttl=900, high_value_threshold=500, policy=Policy(url), today=lambda: TODAY)
    return odoo, store, build


def test_bulk_refund_is_blocked_before_any_human_sees_it(env):
    odoo, store, build = env
    for n in range(240):
        odoo.add_invoice(300 + n, age=n % 120)
    out = build(scripted([call("issue_refunds", invoice_date_from=(TODAY - timedelta(days=90)).isoformat(),
                                 reason="late_delivery")], DONE)).chat("s", "refund everyone")
    assert out["proposals"] == [] and odoo.writes == []
    b = out["blocked"][0]
    assert b["count"] == sum(1 for n in range(240) if n % 120 <= 90)
    assert b["counts"]["batch_limit"] == 1 and b["counts"]["daily_cap"] == 1 and b["counts"]["outside_window"] > 0
    assert b["decision_id"]  # OPA decision logging is on: the denial is traceable in OPA's own log
    ev = [a for a in store.audit_log() if a["event"] == "policy_denied"]
    assert ev and ev[0]["detail"]["decision_id"] == b["decision_id"]
    assert store.audit_for_owner("s")[0]["event"] == "policy_denied"


def test_eligible_refund_needs_approval_then_posts_a_credit_note(env):
    odoo, store, build = env
    inv = odoo.add_invoice(450, age=10)
    ag = build(scripted([call("issue_refunds", invoice_ids=[inv], reason="damaged_goods")], DONE))
    p = ag.chat("s", "refund it")["proposals"][0]
    assert p["risk"] == "low" and p["preview"]["details"]["policy"]["decision_id"]
    proposed = [a for a in store.audit_log() if a["event"] == "proposed"][0]
    assert proposed["detail"]["decision_id"] == p["preview"]["details"]["policy"]["decision_id"]
    assert odoo.writes == []  # proposed only
    done = ag.approve(p["id"], "boss", None)
    assert done["status"] == "executed", done
    assert done["result"]["refunds"][0]["credit_note"].startswith("RINV/")
    # the same invoice cannot be refunded twice
    again = build(scripted([call("issue_refunds", invoice_ids=[inv], reason="damaged_goods")], DONE)).chat("s", "again")
    assert again["proposals"] == [] and again["blocked"][0]["counts"] == {"already_refunded": 1}


def test_policy_is_checked_again_at_execution(env):
    odoo, store, build = env
    ids = [odoo.add_invoice(1800, age=5) for _ in range(3)]
    props = [build(scripted([call("issue_refunds", invoice_ids=[i], reason="billing_error")], DONE)).chat("s", "x")["proposals"][0]
             for i in ids]
    assert all(p["risk"] == "high" for p in props)  # 1800 > 1000
    assert build(None).approve(props[0]["id"], "boss", props[0]["id"])["status"] == "executed"
    assert build(None).approve(props[1]["id"], "boss", props[1]["id"])["status"] == "executed"
    third = build(None).approve(props[2]["id"], "boss", props[2]["id"])  # 3600 already today + 1800 > 5000 cap
    assert third["status"] == "failed" and "policy re-check denied" in third["error"] and "daily cap" in third["error"]
    assert sum(1 for m in odoo.moves.values() if m["move_type"] == "out_refund") == 2


def test_unreachable_policy_engine_fails_closed(env):
    odoo, store, build = env
    inv = odoo.add_invoice(100, age=1)
    out = build(scripted([call("issue_refunds", invoice_ids=[inv], reason="damaged_goods")], DONE),
                url="http://127.0.0.1:9/v1/data/refunds/decision").chat("s", "x")
    assert out["proposals"] == [] and out["blocked"][0]["counts"] == {"policy_unavailable": 1}


def test_unpaid_invoice_is_blocked(env):
    odoo, store, build = env
    inv = odoo.add_invoice(100, age=1, paid=False)
    out = build(scripted([call("issue_refunds", invoice_ids=[inv], reason="damaged_goods")], DONE)).chat("s", "x")
    assert out["blocked"][0]["counts"] == {"not_paid": 1}


def test_unknown_invoice_id_is_an_error_not_a_proposal(env):
    odoo, store, build = env
    out = build(scripted([call("issue_refunds", invoice_ids=[999], reason="damaged_goods")], DONE)).chat("s", "x")
    assert out["proposals"] == [] and out["blocked"] == []


def test_llm_cannot_send_both_selectors(env):
    odoo, store, build = env
    inv = odoo.add_invoice(100, age=1)
    out = build(scripted([call("issue_refunds", invoice_ids=[inv], invoice_date_from="2026-10-01", reason="damaged_goods")],
                         DONE)).chat("s", "x")
    assert out["proposals"] == [] and out["blocked"] == []
