"""Seed the sandbox Odoo DB with fictional data and create the limited `ai_agent` user.

Run on the server after the odoo_demo DB is initialised. Writes /opt/odoo-agent/demo.env (0600) for the demo service.
Idempotent enough for the nightly rebuild, which always starts from a fresh DB.
"""
import datetime
import os
import random
import secrets
import xmlrpc.client

URL, DB = "http://127.0.0.1:8069", "odoo_demo"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PWFILE = os.path.join(ROOT, "demo.admin_pw")

admin_pw = open(PWFILE).read().strip() if os.path.exists(PWFILE) else "admin"
common = xmlrpc.client.ServerProxy(URL + "/xmlrpc/2/common")
uid = common.authenticate(DB, "admin", admin_pw, {})
if not uid and admin_pw != "admin":
    admin_pw = "admin"; uid = common.authenticate(DB, "admin", "admin", {})
assert uid, "cannot log in to demo DB as admin"
m = xmlrpc.client.ServerProxy(URL + "/xmlrpc/2/object", allow_none=True)
ex = lambda model, method, args, kw=None: m.execute_kw(DB, uid, admin_pw, model, method, args, kw or {})

# 1. lock the sandbox admin account behind a random password nobody is given
new_admin = secrets.token_urlsafe(24)
ex("res.users", "write", [[uid], {"password": new_admin}])
open(PWFILE, "w").write(new_admin); os.chmod(PWFILE, 0o600)
admin_pw = new_admin

# 2. fictional customers and products
CUSTOMERS = [("Northwind Traders", "orders@northwind.example"), ("Contoso Retail", "buyer@contoso.example"),
             ("Globex Manufacturing", "purchasing@globex.example"), ("Initech Services", "ap@initech.example"),
             ("Umbrella Logistics", "ops@umbrella.example"), ("Stark Fabrication", "supply@stark.example"),
             ("Wayne Supplies", "orders@wayne.example"), ("Acme Hardware", "sales@acme.example")]
pid = {n: ex("res.partner", "create", [{"name": n, "email": e, "customer_rank": 1}]) for n, e in CUSTOMERS}
PRODUCTS = [("Standing Desk", 450, "consu"), ("Ergonomic Chair", 220, "consu"), ('Monitor 27"', 310, "consu"),
            ("Laptop Stand", 45, "consu"), ("Wireless Keyboard", 70, "consu"), ("HD Webcam", 85, "consu"),
            ("Desk Lamp", 38, "consu"), ("Onboarding Workshop", 1200, "service")]
prod = {n: ex("product.product", "create", [{"name": n, "list_price": p, "type": t, "sale_ok": True}]) for n, p, t in PRODUCTS}

# 3. a little history so reads are interesting: 2 draft quotes, 1 confirmed order, 2 posted unpaid invoices
def quote(partner, lines):
    return ex("sale.order", "create", [{"partner_id": pid[partner],
              "order_line": [(0, 0, {"product_id": prod[p], "product_uom_qty": q}) for p, q in lines]}])
quote("Northwind Traders", [("Ergonomic Chair", 4), ("Desk Lamp", 4)])
quote("Contoso Retail", [("Standing Desk", 2)])
ex("sale.order", "action_confirm", [[quote("Globex Manufacturing", [("Monitor 27\"", 6), ("Laptop Stand", 6)])]])
for partner, (p, q) in [("Initech Services", ("Onboarding Workshop", 1)), ("Stark Fabrication", ("Standing Desk", 3))]:
    inv = ex("account.move", "create", [{"move_type": "out_invoice", "partner_id": pid[partner],
             "invoice_date": "2026-09-20", "invoice_line_ids": [(0, 0, {"product_id": prod[p], "quantity": q})]}])
    ex("account.move", "action_post", [[inv]])

# 3b. refund scenario: ~300 invoices over the last 120 days (dates relative to the rebuild day, so the demo
#     looks the same every morning), most paid, three already refunded today. Northwind's newest invoice is a
#     clean, eligible one for the "single refund" path.
rng, today = random.Random(42), datetime.date.today()
GOODS = [n for n, _, t in PRODUCTS if t == "consu"]
names = list(pid)
vals = []
for k in range(300):
    partner = "Northwind Traders" if k == 0 else names[rng.randrange(len(names))]
    if k == 0:
        lines = [("Ergonomic Chair", 2)]
    elif k % 37 == 5:
        lines = [("Onboarding Workshop", 2)]  # above the single-refund limit
    else:
        lines = [(p, rng.choice([1, 1, 2, 3, 4])) for p in rng.sample(GOODS, rng.choice([1, 1, 2]))]
    vals.append({"move_type": "out_invoice", "partner_id": pid[partner],
                 "invoice_date": (today - datetime.timedelta(days=int(k * 120 / 300))).isoformat(),
                 "invoice_line_ids": [(0, 0, {"product_id": prod[p], "quantity": q}) for p, q in lines]})
inv_ids = []
for i in range(0, len(vals), 50):
    chunk = ex("account.move", "create", [vals[i:i + 50]])
    ex("account.move", "action_post", [chunk])
    inv_ids += chunk
paid = [i for n, i in enumerate(inv_ids) if n == 0 or rng.random() < 0.88]
for i in range(0, len(paid), 50):
    w = ex("account.payment.register", "create", [{}], {"context": {"active_model": "account.move", "active_ids": paid[i:i + 50]}})
    ex("account.payment.register", "action_create_payments", [[w]])
small = ex("account.move", "search_read", [[["id", "in", paid[1:80]], ["amount_total", "<", 500]]], {"fields": ["id", "journal_id"], "limit": 3})
for r in small:
    w = ex("account.move.reversal", "create", [{"move_ids": [(6, 0, [r["id"]])], "journal_id": r["journal_id"][0], "reason": "damaged goods"}])
    ex("account.move.reversal", "refund_moves", [[w]])
    cn = ex("account.move", "search_read", [[["reversed_entry_id", "=", r["id"]], ["state", "=", "draft"]]], {"fields": ["id"], "limit": 1})
    ex("account.move", "action_post", [[cn[0]["id"]]])

# 4. least-privilege service user for the agent (Billing so an APPROVED refund can post its credit note)
groups = []
for mod, name in [("sales_team", "group_sale_salesman_all_leads"), ("account", "group_account_readonly"),
                  ("account", "group_account_invoice"), ("base", "group_partner_manager")]:
    r = ex("ir.model.data", "search_read", [[["module", "=", mod], ["name", "=", name]]], {"fields": ["res_id"], "limit": 1})
    assert r, f"missing group {mod}.{name}"
    groups.append(r[0]["res_id"])
agent_pw = secrets.token_urlsafe(24)
ex("res.users", "create", [{"name": "AI Agent (sandbox)", "login": "ai_agent", "password": agent_pw, "group_ids": [(6, 0, groups)]}])

# 5. env for the demo service (Gemini settings are copied from the private instance's .env, never printed)
src = dict(l.rstrip("\n").split("=", 1) for l in open(os.path.join(ROOT, ".env")) if "=" in l and not l.startswith("#"))
lines = ["MODE=demo", f"ODOO_URL={URL}", f"ODOO_DB={DB}", "ODOO_USER=ai_agent", f"ODOO_PASSWORD={agent_pw}",
         f"GEMINI_BASE={src['GEMINI_BASE']}", f"GEMINI_API_KEY={src['GEMINI_API_KEY']}", "GEMINI_MODEL=gemini-2.5-flash",
         f"DB_PATH={ROOT}/demo-agent.db", "PROPOSAL_TTL_SECONDS=1800", "HIGH_VALUE_THRESHOLD=2000",
         "USER_CHATS_PER_DAY=20", "CHAT_PER_HOUR=120", "SIGNUPS_PER_IP_PER_DAY=5", ""]
open(os.path.join(ROOT, "demo.env"), "w").write("\n".join(lines)); os.chmod(os.path.join(ROOT, "demo.env"), 0o600)
print("seeded: %d customers, %d products, %d invoices (%d paid, %d refunded); demo.env written"
      % (len(pid), len(prod), len(inv_ids), len(paid), len(small)))
