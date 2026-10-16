"""HTTP layer. Two distinct tokens: AGENT_TOKEN may chat; only APPROVER_TOKEN may approve/reject."""
import hmac
import os
import time
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.responses import HTMLResponse, PlainTextResponse  # noqa: E402

from .accounts import Accounts  # noqa: E402
from .agent import Agent  # noqa: E402
from .llm import make_generate  # noqa: E402
from .odoo import Odoo  # noqa: E402
from .store import Store  # noqa: E402


class Window:
    """Sliding-window counter."""
    def __init__(self, limit: int, seconds: int):
        self.limit, self.seconds, self.hits = limit, seconds, defaultdict(deque)

    def _trim(self, key):
        q, now = self.hits[key], time.time()
        while q and q[0] < now - self.seconds:
            q.popleft()
        return q

    def blocked(self, key) -> bool:
        return len(self._trim(key)) >= self.limit

    def add(self, key) -> None:
        self._trim(key).append(time.time())


def _bearer(expected: str, failures: Window):
    def dep(request: Request, authorization: str | None = Header(default=None)) -> None:
        if not expected:
            raise HTTPException(500, "token not configured")
        ip = request.client.host if request.client else "?"  # real client IP via uvicorn --proxy-headers behind Caddy
        if failures.blocked(ip):
            raise HTTPException(429, "Too many failed attempts, try later")
        if not hmac.compare_digest(authorization or "", f"Bearer {expected}"):
            failures.add(ip)
            raise HTTPException(401, "Unauthorized")
    return dep


def create_app(agent: Agent | None = None, agent_token: str | None = None, approver_token: str | None = None,
               mode: str | None = None) -> FastAPI:
    mode = mode or os.getenv("MODE", "private")  # "private": two shared tokens | "demo": visitor accounts, self-approval
    agent_token = agent_token or os.getenv("AGENT_TOKEN", "")
    approver_token = approver_token or os.getenv("APPROVER_TOKEN", "")
    if agent_token and agent_token == approver_token:
        raise RuntimeError("AGENT_TOKEN and APPROVER_TOKEN must differ")
    if agent is None:
        odoo = Odoo(os.getenv("ODOO_URL", "http://127.0.0.1:8069"), os.getenv("ODOO_DB", "odoo"),
                    os.getenv("ODOO_USER", ""), os.getenv("ODOO_PASSWORD", ""))
        agent = Agent(
            odoo, Store(os.getenv("DB_PATH", "./agent.db")),
            make_generate(os.environ["GEMINI_BASE"], os.environ["GEMINI_API_KEY"], os.getenv("GEMINI_MODEL", "gemini-2.5-flash")),
            ttl=int(os.getenv("PROPOSAL_TTL_SECONDS", "900")),
            high_value_threshold=float(os.getenv("HIGH_VALUE_THRESHOLD", "50000")))

    app = FastAPI(title="Odoo approval-gated agent")
    failures = Window(int(os.getenv("AUTH_FAIL_LIMIT", "10")), 900)  # shared across both tokens, per client IP
    is_agent, is_approver = _bearer(agent_token, failures), _bearer(approver_token, failures)
    chats = Window(int(os.getenv("CHAT_PER_HOUR", "60")), 3600)  # global cap: bounds LLM spend even with a valid token

    @app.get("/", response_class=HTMLResponse)
    def ui():
        return HTMLResponse((Path(__file__).parent / "ui.html").read_text(),
                            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"})

    @app.get("/robots.txt", response_class=PlainTextResponse)
    def robots():
        return "User-agent: *\nDisallow: /\n"

    @app.get("/config")
    def config():
        return {"mode": mode, "daily_chats": int(os.getenv("USER_CHATS_PER_DAY", "20"))}

    @app.get("/health")
    def health():
        ok, detail = agent.odoo.ping()
        return {"status": "ok", "odoo_reachable": ok, "detail": detail}


    if mode == "demo":
        accounts = Accounts(agent.store)
        daily = int(os.getenv("USER_CHATS_PER_DAY", "20"))
        signups = Window(int(os.getenv("SIGNUPS_PER_IP_PER_DAY", "5")), 86400)

        def ip_of(request: Request) -> str:
            return request.client.host if request.client else "?"

        def user(request: Request, authorization: str | None = Header(default=None)) -> str:
            ip = ip_of(request)
            if failures.blocked(ip):
                raise HTTPException(429, "Too many failed attempts, try later")
            email = accounts.user_for((authorization or "").removeprefix("Bearer ").strip())
            if not email:
                failures.add(ip)
                raise HTTPException(401, "Please log in")
            return email

        @app.post("/signup")
        def signup(request: Request, email: str = Body(..., max_length=200), password: str = Body(..., max_length=100)):
            ip = ip_of(request)
            if signups.blocked(ip):
                raise HTTPException(429, "Too many sign-ups from this address today")
            signups.add(ip)
            try:
                return {"token": accounts.signup(email, password, ip), "email": email.strip().lower()}
            except ValueError as e:
                raise HTTPException(400, str(e))

        @app.post("/login")
        def login(request: Request, email: str = Body(..., max_length=200), password: str = Body(..., max_length=100)):
            ip = ip_of(request)
            if failures.blocked(ip):
                raise HTTPException(429, "Too many failed attempts, try later")
            tok = accounts.login(email, password)
            if not tok:
                failures.add(ip)
                raise HTTPException(401, "Wrong email or password")
            return {"token": tok, "email": email.strip().lower()}

        @app.post("/logout")
        def logout(authorization: str | None = Header(default=None), email: str = Depends(user)):
            accounts.logout((authorization or "").removeprefix("Bearer ").strip())
            return {"ok": True}

        @app.get("/me")
        def me(email: str = Depends(user)):
            return {"email": email, "chats_left_today": accounts.remaining(email, daily)}

        @app.post("/chat")
        def d_chat(message: str = Body(..., max_length=2000, embed=True), email: str = Depends(user)):
            if chats.blocked("all"):
                raise HTTPException(429, "The sandbox is busy, try again in a few minutes")
            if not accounts.charge_chat(email, daily):
                raise HTTPException(429, f"Daily limit of {daily} messages reached, come back tomorrow")
            chats.add("all")
            try:
                out = agent.chat(email, message)
            except RuntimeError as e:
                raise HTTPException(502, str(e))
            return {**out, "chats_left_today": accounts.remaining(email, daily)}

        def owned(pid: str, email: str) -> dict:
            p = agent.store.get(pid)
            if not p or p["session_id"] != email:
                raise HTTPException(404, "no such proposal")
            return p

        @app.get("/proposals")
        def d_proposals(email: str = Depends(user)):
            return {"proposals": agent.store.list_proposals(owner=email)}

        @app.post("/proposals/{pid}/approve")
        def d_approve(pid: str, confirm: str | None = Body(default=None, embed=True), email: str = Depends(user)):
            owned(pid, email)
            try:
                return agent.approve(pid, email, confirm)
            except PermissionError as e:
                raise HTTPException(409, str(e))

        @app.post("/proposals/{pid}/reject")
        def d_reject(pid: str, email: str = Depends(user)):
            owned(pid, email)
            if not agent.store.reject(pid, email):
                raise HTTPException(409, "proposal is not pending")
            return agent.store.get(pid)

        @app.get("/audit")
        def d_audit(email: str = Depends(user)):
            return {"audit": agent.store.audit_for_owner(email)}

        return app

    # ---------------------------------------------------------------- private mode (two shared tokens)
    @app.post("/chat", dependencies=[Depends(is_agent)])
    def chat(session_id: str = Body(..., max_length=64), message: str = Body(..., max_length=2000)):
        if chats.blocked("all"):
            raise HTTPException(429, "Hourly chat limit reached")
        chats.add("all")
        try:
            return agent.chat(session_id, message)
        except RuntimeError as e:
            raise HTTPException(502, str(e))

    @app.get("/proposals", dependencies=[Depends(is_approver)])
    def proposals(status: str | None = None):
        return {"proposals": agent.store.list_proposals(status)}

    @app.post("/proposals/{pid}/approve", dependencies=[Depends(is_approver)])
    def approve(pid: str, confirm: str | None = Body(default=None, embed=True)):
        try:
            return agent.approve(pid, "human-approver", confirm)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except PermissionError as e:
            raise HTTPException(409, str(e))

    @app.post("/proposals/{pid}/reject", dependencies=[Depends(is_approver)])
    def reject(pid: str):
        if not agent.store.reject(pid, "human-approver"):
            raise HTTPException(409, "proposal is not pending")
        return agent.store.get(pid)

    @app.get("/audit", dependencies=[Depends(is_approver)])
    def audit():
        return {"audit": agent.store.audit_log()}

    return app


def app_factory():
    return create_app()
