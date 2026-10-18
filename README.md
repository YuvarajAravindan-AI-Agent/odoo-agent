# Odoo approval-gated agent

Chat agent (Gemini via vertex-gateway) that can read Odoo freely but can only *propose* changes.
A human approves each one with a separate token.

## The gate
- Write tools (`create_partner`, `create_quotation`, `confirm_sale_order`, `cancel_sale_order`) are never run by the agent loop; a call becomes a stored proposal (`app/agent.py`). Only `Agent.approve()` runs them, and it is reachable only with `APPROVER_TOKEN`.
- Two different tokens (startup refuses if equal). The agent token cannot approve.
- High risk (confirm, cancel, quotes >= `HIGH_VALUE_THRESHOLD`) needs the proposal id typed.
- Atomic claim (no double-run), 15-min expiry, args hash check, re-validation and state re-check at execution time.
- Runs as a dedicated least-privilege Odoo user (`ai_agent`: Salesperson, read-only Accounting, Contact/Creation), so Odoo's own ACLs are the last line of defence.
- Append-only audit log (`/audit`): reads, proposals, approvals, rejections, results.

## Refund policy (OPA)
`issue_refunds` is the one tool whose proposals are filtered by policy. The LLM supplies only invoice ids (or a date) and a reason code.
`app/tools.py` reads every fact the policy judges from Odoo (amount, payment state, age, earlier refunds, refunded today) and posts them to
Open Policy Agent (`app/policy.py`, fail-closed: an unreachable or malformed answer is a deny). A denial creates **no proposal**; it is audited
as `policy_denied` with OPA's `decision_id`. An allowed request becomes a normal proposal that carries the same id, and the policy runs again
inside `approve()` because facts change while a proposal waits.

- Rules: `policy/refunds.rego` (batch limit, daily cap, per-refund max, 60-day window, paid only, never twice, approved reasons, fail-closed on missing facts).
- Limits: `policy/data.json` (finance edits this, not the agent). Tests: `opa test policy/` (15) and `tests/test_refunds.py` (runs a real `opa`).
- Deploy (Contabo): `docker run -d --name opa-refunds --restart unless-stopped -p 127.0.0.1:8181:8181 -v /opt/odoo-agent/policy:/policy:ro --read-only --cap-drop ALL --security-opt no-new-privileges openpolicyagent/opa:1.21.2 run --server --addr 0.0.0.0:8181 --authorization=basic --ignore "*_test.rego" --set decision_logs.console=true /policy`
- `policy/system/authz.rego` locks OPA's own API: only `POST /v1/data/refunds/decision` and `GET /health`; policies and data cannot be changed over HTTP.
- Agent env: `OPA_URL` (default `http://127.0.0.1:8181/v1/data/refunds/decision`).
- Sandbox seed: `demo/seed.py` now creates ~300 invoices over 120 days (most paid, three already refunded) and gives `ai_agent` the Billing group so an approved refund can post its credit note.

## Run / test
    python -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pytest
    uvicorn app.main:app_factory --factory --host 127.0.0.1 --port 8795

Deployed on Contabo: `/opt/odoo-agent`, `systemctl status odoo-agent`, localhost-only.
UI over a tunnel: `ssh -N -L 8795:127.0.0.1:8795 contabo-tally` -> http://localhost:8795
Tokens are in `/opt/odoo-agent/.env` (AGENT_TOKEN, APPROVER_TOKEN).

## Extending
Add a `Tool` in `app/tools.py` with `write=True`, a `validate`, a `preview` (what the human sees) and a `run`.
Nothing else changes: the gate applies automatically.

## Known gaps
- Chat history is in memory; proposals/audit are in SQLite.

## Public exposure
`https://odoo-agent.ai-agentic-enterprises.com` via Caddy (needs A record -> 169.58.69.167 at Spaceship).
Hardening: bearer tokens (two), per-IP lockout after `AUTH_FAIL_LIMIT` (10) failures / 15 min, global `CHAT_PER_HOUR` (60) cap on LLM spend,
CSP + HSTS + noindex, 16 KB request cap. Rotate tokens by editing `/opt/odoo-agent/.env` and `systemctl restart odoo-agent`.
