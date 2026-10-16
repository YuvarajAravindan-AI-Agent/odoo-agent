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


def test_lockout_after_failed_tokens_and_chat_cap(env, monkeypatch):
    odoo, store, build = env
    monkeypatch.setenv("AUTH_FAIL_LIMIT", "3")
    monkeypatch.setenv("CHAT_PER_HOUR", "2")
    ag = build(scripted(*([[{"text": "hi"}]] * 5)))
    c = TestClient(create_app(ag, "agent-secret", "approver-secret"))
    bad = {"Authorization": "Bearer nope"}
    assert [c.get("/proposals", headers=bad).status_code for _ in range(3)] == [401, 401, 401]
    assert c.get("/proposals", headers=bad).status_code == 429
    A = {"Authorization": "Bearer agent-secret"}
    assert c.post("/chat", json={"session_id": "s", "message": "x"}, headers=A).status_code == 429  # same IP now locked out


def test_chat_hourly_cap(env, monkeypatch):
    odoo, store, build = env
    monkeypatch.setenv("CHAT_PER_HOUR", "2")
    ag = build(scripted(*([[{"text": "hi"}]] * 5)))
    c = TestClient(create_app(ag, "agent-secret", "approver-secret"))
    A = {"Authorization": "Bearer agent-secret"}
    codes = [c.post("/chat", json={"session_id": "s", "message": "x"}, headers=A).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


# ------------------------------------------------------------------ demo (visitor) mode
def demo_client(build, steps, monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    return TestClient(create_app(build(scripted(*steps)), mode="demo"))


def signup(c, email="a@x.com", pw="password1"):
    r = c.post("/signup", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_demo_requires_login(env, monkeypatch):
    c = demo_client(env[2], [], monkeypatch)
    assert c.post("/chat", json={"message": "hi"}).status_code == 401
    assert c.get("/proposals").status_code == 401
    assert c.get("/config").json()["mode"] == "demo"


def test_demo_signup_login_validation(env, monkeypatch):
    c = demo_client(env[2], [], monkeypatch)
    assert c.post("/signup", json={"email": "bad", "password": "password1"}).status_code == 400
    assert c.post("/signup", json={"email": "a@x.com", "password": "short"}).status_code == 400
    signup(c)
    assert c.post("/signup", json={"email": "A@x.com", "password": "password1"}).status_code == 400  # dupe, case-insens.
    assert c.post("/login", json={"email": "a@x.com", "password": "wrongpass1"}).status_code == 401
    assert c.post("/login", json={"email": "a@x.com", "password": "password1"}).status_code == 200


def test_demo_self_approval_and_isolation(env, monkeypatch):
    odoo, store, build = env
    c = demo_client(build, [[call("create_partner", name="Mine")], [{"text": "ok"}]], monkeypatch)
    A, B = signup(c, "a@x.com"), signup(c, "b@x.com")
    r = c.post("/chat", json={"message": "add Mine"}, headers=A).json()
    pid = r["proposals"][0]["id"]
    assert odoo.writes == []                                          # proposing never writes
    assert c.get("/proposals", headers=B).json()["proposals"] == []   # B cannot see A's proposals
    assert c.post(f"/proposals/{pid}/approve", json={}, headers=B).status_code == 404
    assert c.post(f"/proposals/{pid}/reject", headers=B).status_code == 404
    assert c.post(f"/proposals/{pid}/approve", json={}, headers=A).json()["status"] == "executed"
    assert any(a["event"] == "executed" for a in c.get("/audit", headers=A).json()["audit"])
    assert c.get("/audit", headers=B).json()["audit"] == []


def test_demo_high_risk_still_needs_typed_id(env, monkeypatch):
    odoo, store, build = env
    odoo.orders[100] = {"id": 100, "name": "S100", "partner_id": [1, "A"], "amount_total": 5.0, "state": "draft"}
    c = demo_client(build, [[call("confirm_sale_order", order_id=100)], [{"text": "ok"}]], monkeypatch)
    A = signup(c)
    pid = c.post("/chat", json={"message": "confirm"}, headers=A).json()["proposals"][0]["id"]
    assert c.post(f"/proposals/{pid}/approve", json={}, headers=A).status_code == 409
    assert c.post(f"/proposals/{pid}/approve", json={"confirm": pid}, headers=A).json()["status"] == "executed"


def test_demo_daily_quota(env, monkeypatch):
    c = demo_client(env[2], [[{"text": "hi"}]] * 3, monkeypatch, USER_CHATS_PER_DAY=2)
    A = signup(c)
    codes = [c.post("/chat", json={"message": "x"}, headers=A).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert c.get("/me", headers=A).json()["chats_left_today"] == 0


def test_demo_signup_ip_limit_and_logout(env, monkeypatch):
    c = demo_client(env[2], [], monkeypatch, SIGNUPS_PER_IP_PER_DAY=2)
    A = signup(c, "a@x.com"); signup(c, "b@x.com")
    assert c.post("/signup", json={"email": "c@x.com", "password": "password1"}).status_code == 429
    c.post("/logout", headers=A)
    assert c.get("/me", headers=A).status_code == 401


def test_demo_has_no_token_routes(env, monkeypatch):
    c = demo_client(env[2], [], monkeypatch)
    assert c.get("/proposals", headers={"Authorization": "Bearer anything"}).status_code == 401
