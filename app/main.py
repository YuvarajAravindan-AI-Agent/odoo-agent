"""HTTP layer. Two distinct tokens: AGENT_TOKEN may chat; only APPROVER_TOKEN may approve/reject."""
import hmac
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import Body, Depends, FastAPI, Header, HTTPException  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

from .agent import Agent  # noqa: E402
from .llm import make_generate  # noqa: E402
from .odoo import Odoo  # noqa: E402
from .store import Store  # noqa: E402


def _bearer(expected: str):
    def dep(authorization: str | None = Header(default=None)) -> None:
        if not expected:
            raise HTTPException(500, "token not configured")
        if not hmac.compare_digest(authorization or "", f"Bearer {expected}"):
            raise HTTPException(401, "Unauthorized")
    return dep


def create_app(agent: Agent | None = None, agent_token: str | None = None, approver_token: str | None = None) -> FastAPI:
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
    is_agent, is_approver = _bearer(agent_token), _bearer(approver_token)

    @app.get("/", response_class=HTMLResponse)
    def ui():
        return (Path(__file__).parent / "ui.html").read_text()

    @app.get("/health")
    def health():
        ok, detail = agent.odoo.ping()
        return {"status": "ok", "odoo_reachable": ok, "detail": detail}

    @app.post("/chat", dependencies=[Depends(is_agent)])
    def chat(session_id: str = Body(..., max_length=64), message: str = Body(..., max_length=2000)):
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
