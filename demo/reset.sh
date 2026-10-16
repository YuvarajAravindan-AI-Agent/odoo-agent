#!/usr/bin/env bash
# Rebuild the sandbox: fresh Odoo DB + seed data + clear agent proposals/audit (visitor accounts are kept).
# Nightly via systemd timer. SKIP_REBUILD=1 skips the drop/init (when the DB was just created).
set -euo pipefail
cd /opt/odoo-agent
systemctl stop odoo-agent-demo 2>/dev/null || true
if [ -z "${SKIP_REBUILD:-}" ]; then
  docker exec odoo-db-1 psql -U odoo -d postgres -c "DROP DATABASE IF EXISTS odoo_demo WITH (FORCE)"
  rm -f demo.admin_pw
  (cd /opt/odoo && docker compose run --rm -T odoo /opt/odoo-src/odoo-bin -c /etc/odoo/odoo.conf \
     -d odoo_demo -i sale_management,account,contacts --without-demo=all --stop-after-init)
  (cd /opt/odoo && docker compose restart odoo)   # drop stale registry for the recreated DB
  sleep 25
fi
.venv/bin/python demo/seed.py
.venv/bin/python - <<'PY'
import os
from app.store import Store
Store("/opt/odoo-agent/demo-agent.db").reset_demo_state()
PY
systemctl start odoo-agent-demo
