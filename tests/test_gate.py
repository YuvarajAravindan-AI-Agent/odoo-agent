import time

import pytest
from fastapi.testclient import TestClient

from app.agent import Agent
from app.main import create_app
from app.store import Store


class FakeOdoo:
    """In-memory stand-in for the few Odoo calls the tools make."""
    def __init__(self):
        self.partners = {1: {"id": 1, "name": "Azure Interior", "email": "a@x.com", "phone": "", "city": ""}}
        self.products = {7: {"id": 7, "name": "Office Chair", "list_price": 100.0, "default_code": "CH"}}
        self.orders = {}
        self.writes = []

    def ping(self):
        return True, "ok"

    def search_read(self, model, domain, fields, limit=20, order=""):
        flat = [d for d in domain if isinstance(d, list)]
        eq = {d[0]: d[2] for d in flat if d[1] in ("=", "=ilike")}
        if model == "res.partner":
            rows = list(self.partners.values())
            if "id" in eq:
                rows = [r for r in rows if r["id"] == eq["id"]]
            if "name" in eq:
                rows = [r for r in rows if r["name"].lower() == eq["name"].lower()]
            return rows[:limit]
        if model == "product.product":
            return [p for p in self.products.values() if "id" not in eq or p["id"] == eq["id"]][:limit]
        if model == "sale.order":
            return [o for o in self.orders.values() if "id" not in eq or o["id"] == eq["id"]][:limit]
        return []

    def call(self, model, method, args, kwargs=None):
        self.writes.append((model, method, args))
        if (model, method) == ("res.partner", "create"):
            i = max(self.partners) + 1
            self.partners[i] = {"id": i, **args[0]}
            return i
        if (model, method) == ("sale.order", "create"):
            i = len(self.orders) + 100
            tot = sum(self.products[l[2]["product_id"]]["list_price"] * l[2]["product_uom_qty"] for l in args[0]["order_line"])
            self.orders[i] = {"id": i, "name": f"S{i}", "partner_id": [args[0]["partner_id"], "Azure Interior"], "amount_total": tot, "state": "draft"}
            return i
        if (model, method) == ("sale.order", "action_confirm"):
            self.orders[args[0][0]]["state"] = "sale"
            return True
        if (model, method) == ("sale.order", "action_cancel"):
            self.orders[args[0][0]]["state"] = "cancel"
            return True
        raise AssertionError(f"unexpected write {model}.{method}")


def scripted(*steps):
    """LLM stub: each step is a list of parts the 'model' returns, in order."""
    it = iter(steps)
    return lambda body: {"candidates": [{"content": {"role": "model", "parts": next(it)}}]}


def call(tool, /, **args):
    return {"functionCall": {"name": tool, "args": args}}


@pytest.fixture
def env(tmp_path):
    odoo = FakeOdoo()
    store = Store(str(tmp_path / "t.db"))
    def build(gen, ttl=900):
        return Agent(odoo, store, gen, ttl=ttl, high_value_threshold=500)
    return odoo, store, build


def test_agent_cannot_write(env):
    odoo, store, build = env
    ag = build(scripted([call("create_quotation", partner_id=1, lines=[{"product_id": 7, "quantity": 2}])], [{"text": "Waiting for approval."}]))
    out = ag.chat("s", "quote 2 chairs")
    assert odoo.writes == []  # nothing reached Odoo
    assert len(out["proposals"]) == 1 and out["proposals"][0]["status"] == "pending"


def test_approve_executes_once(env):
    odoo, store, build = env
    ag = build(scripted([call("create_partner", name="Zed Corp")], [{"text": "ok"}]))
    pid = ag.chat("s", "add Zed")["proposals"][0]["id"]
    done = ag.approve(pid, "me", None)
    assert done["status"] == "executed" and odoo.partners[2]["name"] == "Zed Corp"
    with pytest.raises(PermissionError):
        ag.approve(pid, "me", None)  # double-approve blocked
    assert sum(1 for w in odoo.writes if w[1] == "create") == 1


def test_reject_never_executes(env):
    odoo, store, build = env
    ag = build(scripted([call("create_partner", name="Nope")], [{"text": "ok"}]))
    pid = ag.chat("s", "add")["proposals"][0]["id"]
    assert store.reject(pid, "me")
    with pytest.raises(PermissionError):
        ag.approve(pid, "me", None)
    assert odoo.writes == []


def test_high_risk_needs_typed_confirmation(env):
    odoo, store, build = env
    odoo.orders[100] = {"id": 100, "name": "S100", "partner_id": [1, "Azure Interior"], "amount_total": 200.0, "state": "draft"}
    ag = build(scripted([call("confirm_sale_order", order_id=100)], [{"text": "ok"}]))
    p = ag.chat("s", "confirm")["proposals"][0]
    assert p["risk"] == "high"
    with pytest.raises(PermissionError):
        ag.approve(p["id"], "me", None)
    with pytest.raises(PermissionError):
        ag.approve(p["id"], "me", "wrong")
    assert odoo.orders[100]["state"] == "draft"
    assert ag.approve(p["id"], "me", p["id"])["status"] == "executed"
    assert odoo.orders[100]["state"] == "sale"


def test_high_value_quote_is_high_risk(env):
    odoo, store, build = env
    ag = build(scripted([call("create_quotation", partner_id=1, lines=[{"product_id": 7, "quantity": 10}])], [{"text": "ok"}]))
    assert ag.chat("s", "big")["proposals"][0]["risk"] == "high"  # 1000 >= threshold 500


def test_expired_proposal_cannot_run(env):
    odoo, store, build = env
    ag = build(scripted([call("create_partner", name="Late")], [{"text": "ok"}]), ttl=0)
    pid = ag.chat("s", "x")["proposals"][0]["id"]
    time.sleep(0.01)
    with pytest.raises(PermissionError):
        ag.approve(pid, "me", None)
    assert odoo.writes == []


def test_state_drift_fails_safely(env):
    odoo, store, build = env
    odoo.orders[100] = {"id": 100, "name": "S100", "partner_id": [1, "A"], "amount_total": 1.0, "state": "draft"}
    ag = build(scripted([call("confirm_sale_order", order_id=100)], [{"text": "ok"}]))
    p = ag.chat("s", "c")["proposals"][0]
    odoo.orders[100]["state"] = "cancel"  # someone cancelled it meanwhile
    done = ag.approve(p["id"], "me", p["id"])
    assert done["status"] == "failed" and not any(w[1] == "action_confirm" for w in odoo.writes)


def test_tampered_args_rejected(env):
    odoo, store, build = env
    ag = build(scripted([call("create_partner", name="Real")], [{"text": "ok"}]))
    pid = ag.chat("s", "x")["proposals"][0]["id"]
    store._q("UPDATE proposals SET args_json='{\"name\": \"Evil\"}' WHERE id=?", (pid,))
    with pytest.raises(PermissionError):
        ag.approve(pid, "me", None)
    assert odoo.writes == []


def test_invalid_args_never_become_proposals(env):
    odoo, store, build = env
    ag = build(scripted([call("create_quotation", partner_id=1, lines=[{"product_id": 7, "quantity": -5}])], [{"text": "sorry"}]))
    assert ag.chat("s", "x")["proposals"] == []
    ag = build(scripted([call("create_partner", name="X", email="not-an-email")], [{"text": "sorry"}]))
    assert ag.chat("s", "x")["proposals"] == []


def test_unknown_tool_and_injection_text_do_nothing(env):
    odoo, store, build = env
    ag = build(scripted([call("execute_sql", q="drop table")], [{"text": "can't"}]))
    assert ag.chat("s", "x")["proposals"] == [] and odoo.writes == []


def test_http_token_separation(env):
    odoo, store, build = env
    ag = build(scripted([call("create_partner", name="Web")], [{"text": "ok"}]))
    c = TestClient(create_app(ag, "agent-secret", "approver-secret"))
    A, P = {"Authorization": "Bearer agent-secret"}, {"Authorization": "Bearer approver-secret"}
    assert c.post("/chat", json={"session_id": "s", "message": "hi"}).status_code == 401
    r = c.post("/chat", json={"session_id": "s", "message": "add Web"}, headers=A)
    pid = r.json()["proposals"][0]["id"]
    assert c.post(f"/proposals/{pid}/approve", json={}, headers=A).status_code == 401  # agent token can't approve
    assert c.get("/proposals", headers=A).status_code == 401
    assert c.post(f"/proposals/{pid}/approve", json={}, headers=P).json()["status"] == "executed"
    assert c.post("/chat", json={"session_id": "s", "message": "x"}, headers=P).status_code == 401


def test_tokens_must_differ(env):
    odoo, store, build = env
    with pytest.raises(RuntimeError):
        create_app(build(scripted()), "same", "same")
