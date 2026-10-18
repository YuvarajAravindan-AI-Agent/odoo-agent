"""The agent's whole capability surface.

Every tool is a fixed function with a fixed model and validated arguments; the LLM never
supplies a model name, a domain, or a method name. Tools are either:
  read   - run immediately, results go back to the LLM
  write  - NEVER run by the agent. The agent loop turns a call into a *proposal*; only a
           human approval (separate token) reaches run().
"""
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Callable

from .policy import PolicyDenied, denied_unavailable

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _int(v, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v or v <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return int(v)


def _text(v, name: str, maxlen: int = 120) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{name} must be a non-empty string")
    v = v.strip()
    if len(v) > maxlen:
        raise ValueError(f"{name} too long (max {maxlen})")
    return v


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    write: bool
    validate: Callable[[dict], dict]
    run: Callable  # (odoo, args, ctx) -> dict
    preview: Callable | None = None  # (odoo, args, ctx) -> {"summary", "details", "risk"}


# ---------------------------------------------------------------- read tools
def _tokens(q: str) -> list[str]:
    """Forgiving match: every word must appear (any order); a trailing plural 's' is ignored."""
    return [t[:-1] if len(t) > 3 and t.endswith("s") else t for t in q.lower().split()]


def _search_partners(odoo, a, ctx):
    domain = [["user_ids", "=", False]]  # customers, not staff/service users
    domain += [["name", "ilike", t] for t in _tokens(a["query"])]
    return odoo.search_read("res.partner", domain, ["name", "email", "phone", "city"], limit=10)


def _list_customers(odoo, a, ctx):
    return odoo.search_read("res.partner", [["user_ids", "=", False], ["customer_rank", ">", 0]],
                            ["name", "email", "city"], limit=25, order="name")


def _search_products(odoo, a, ctx):
    domain = [["sale_ok", "=", True]] + [["name", "ilike", t] for t in _tokens(a["query"])]
    return odoo.search_read("product.product", domain, ["name", "list_price", "default_code"], limit=10)


def _list_products(odoo, a, ctx):
    return odoo.search_read("product.product", [["sale_ok", "=", True]], ["name", "list_price"], limit=25, order="name")


def _list_orders(odoo, a, ctx):
    domain = []
    if a.get("partner_id"):
        domain.append(["partner_id", "=", a["partner_id"]])
    if a.get("state"):
        domain.append(["state", "=", a["state"]])
    if a.get("name"):
        domain.append(["name", "ilike", a["name"]])
    return odoo.search_read(
        "sale.order", domain, ["name", "partner_id", "amount_total", "state", "date_order"],
        limit=15, order="date_order desc")


def _get_order(odoo, a, ctx):
    o = odoo.search_read("sale.order", [["id", "=", a["order_id"]]],
                         ["name", "partner_id", "amount_total", "state", "date_order", "currency_id"], limit=1)
    if not o:
        return {"error": "order not found"}
    o[0]["lines"] = odoo.search_read(
        "sale.order.line", [["order_id", "=", a["order_id"]]],
        ["product_id", "product_uom_qty", "price_unit", "price_subtotal"], limit=50)
    return o[0]


def _list_invoices(odoo, a, ctx):
    domain = [["move_type", "=", "out_invoice"], ["state", "=", "posted"]]
    if a.get("name"):
        domain.append(["name", "ilike", a["name"]])
    if a.get("partner_id"):
        domain.append(["partner_id", "=", a["partner_id"]])
    if a.get("payment_state"):
        domain.append(["payment_state", "in", ["paid", "in_payment"] if a["payment_state"] == "paid" else ["not_paid", "partial"]])
    return odoo.search_read("account.move", domain, ["name", "partner_id", "invoice_date", "amount_total", "payment_state"],
                            limit=15, order="invoice_date desc, id desc")


def _receivables(odoo, a, ctx):
    return odoo.search_read(
        "account.move", [["move_type", "=", "out_invoice"], ["state", "=", "posted"],
                         ["payment_state", "in", ["not_paid", "partial"]]],
        ["name", "partner_id", "invoice_date", "amount_total", "amount_residual"], limit=20, order="invoice_date desc")


# --------------------------------------------------------------- write tools
def _v_partner(a):
    out = {"name": _text(a.get("name"), "name")}
    if a.get("email"):
        out["email"] = _text(a["email"], "email", 120)
        if not EMAIL_RE.match(out["email"]):
            raise ValueError("email is not valid")
    if a.get("phone"):
        out["phone"] = _text(a["phone"], "phone", 30)
    return out


def _p_partner(odoo, a, ctx):
    dupes = odoo.search_read("res.partner", [["name", "=ilike", a["name"]]], ["name", "email"], limit=3)
    warn = [f"A contact named '{d['name']}' already exists (id {d['id']})" for d in dupes]
    return {"summary": f"Create contact '{a['name']}'", "details": a, "warnings": warn, "risk": "low"}


def _r_partner(odoo, a, ctx):
    pid = odoo.call("res.partner", "create", [a])
    return {"id": pid, "name": a["name"]}


def _v_quote(a):
    lines = a.get("lines")
    if not isinstance(lines, list) or not 1 <= len(lines) <= 20:
        raise ValueError("lines must be a list of 1-20 items")
    clean = []
    for ln in lines:
        q = ln.get("quantity")
        if isinstance(q, bool) or not isinstance(q, (int, float)) or not 0 < q <= 1000:
            raise ValueError("quantity must be a number in (0, 1000]")
        clean.append({"product_id": _int(ln.get("product_id"), "product_id"), "quantity": q})
    return {"partner_id": _int(a.get("partner_id"), "partner_id"), "lines": clean}


def _p_quote(odoo, a, ctx):
    partner = odoo.search_read("res.partner", [["id", "=", a["partner_id"]]], ["name"], limit=1)
    if not partner:
        raise ValueError(f"partner {a['partner_id']} does not exist")
    rows, total = [], 0.0
    for ln in a["lines"]:
        p = odoo.search_read("product.product", [["id", "=", ln["product_id"]], ["sale_ok", "=", True]],
                             ["name", "list_price"], limit=1)
        if not p:
            raise ValueError(f"product {ln['product_id']} does not exist or is not sellable")
        sub = p[0]["list_price"] * ln["quantity"]
        total += sub
        rows.append({"product": p[0]["name"], "quantity": ln["quantity"], "unit_price": p[0]["list_price"], "subtotal": sub})
    risk = "high" if total >= ctx["high_value_threshold"] else "low"
    warn = [f"Estimated pre-tax total {total:,.2f} is at or above the high-value threshold"] if risk == "high" else []
    return {"summary": f"Draft quotation for {partner[0]['name']}, est. total {total:,.2f} (pre-tax, list prices)",
            "details": {"customer": partner[0]["name"], "lines": rows, "estimated_total": total},
            "warnings": warn, "risk": risk}


def _r_quote(odoo, a, ctx):
    lines = [(0, 0, {"product_id": l["product_id"], "product_uom_qty": l["quantity"]}) for l in a["lines"]]
    oid = odoo.call("sale.order", "create", [{"partner_id": a["partner_id"], "order_line": lines}])
    o = odoo.search_read("sale.order", [["id", "=", oid]], ["name", "amount_total", "state"], limit=1)
    return o[0] if o else {"id": oid}


def _v_order_id(a):
    return {"order_id": _int(a.get("order_id"), "order_id")}


def _order_for(odoo, oid, allowed):
    o = odoo.search_read("sale.order", [["id", "=", oid]], ["name", "partner_id", "amount_total", "state"], limit=1)
    if not o:
        raise ValueError(f"order {oid} does not exist")
    if o[0]["state"] not in allowed:
        raise ValueError(f"order {o[0]['name']} is in state '{o[0]['state']}', expected one of {sorted(allowed)}")
    return o[0]


def _p_confirm(odoo, a, ctx):
    o = _order_for(odoo, a["order_id"], {"draft", "sent"})
    return {"summary": f"CONFIRM sale order {o['name']} for {o['partner_id'][1]} ({o['amount_total']:,.2f})",
            "details": o, "warnings": ["Confirming turns the quotation into a binding sale order"], "risk": "high"}


def _r_confirm(odoo, a, ctx):
    o = _order_for(odoo, a["order_id"], {"draft", "sent"})  # re-check: state may have changed since approval was requested
    odoo.call("sale.order", "action_confirm", [[a["order_id"]]])
    return {"id": o["id"], "name": o["name"], "state": "sale"}


def _p_cancel(odoo, a, ctx):
    o = _order_for(odoo, a["order_id"], {"draft", "sent", "sale"})
    return {"summary": f"CANCEL sale order {o['name']} for {o['partner_id'][1]} ({o['amount_total']:,.2f})",
            "details": o, "warnings": ["Cancelling cannot be undone from this agent"], "risk": "high"}


def _r_cancel(odoo, a, ctx):
    o = _order_for(odoo, a["order_id"], {"draft", "sent", "sale"})
    odoo.call("sale.order", "action_cancel", [[a["order_id"]]], {"context": {"disable_cancel_warning": True}})
    return {"id": o["id"], "name": o["name"], "state": "cancel"}


# ------------------------------------------------------------------ refunds
# The LLM picks WHICH invoices and a reason code. Everything the policy judges (amounts, payment state, age,
# earlier refunds, today's refund total) is read from Odoo here, then OPA decides. The same check runs twice:
# before a human ever sees a proposal, and again at execution, because facts change while it waits.
REASONS = ["damaged_goods", "late_delivery", "billing_error", "duplicate_charge", "goodwill"]
FACT_FIELDS = ["name", "partner_id", "move_type", "state", "payment_state", "amount_total", "invoice_date", "journal_id"]
MAX_SCAN = 20000


def _date(v, name: str) -> str:
    try:
        return date.fromisoformat(_text(v, name, 10)).isoformat()
    except ValueError:
        raise ValueError(f"{name} must be a date like 2026-10-01")


def _v_refunds(a):
    ids, since = a.get("invoice_ids"), a.get("invoice_date_from")
    if bool(ids) == bool(since):
        raise ValueError("give either invoice_ids or invoice_date_from, not both")
    if a.get("reason") not in REASONS:
        raise ValueError(f"reason must be one of {REASONS}")
    out = {"reason": a["reason"]}
    if ids:
        if not isinstance(ids, list) or len(ids) > 500:
            raise ValueError("invoice_ids must be a list of at most 500 ids")
        out["invoice_ids"] = sorted({_int(i, "invoice id") for i in ids})
    else:
        out["invoice_date_from"] = _date(since, "invoice_date_from")
    return out


def _refund_facts(odoo, a, ctx) -> dict:
    today = ctx["today"]()
    if "invoice_ids" in a:
        rows = odoo.search_read("account.move", [["id", "in", a["invoice_ids"]]], FACT_FIELDS, limit=len(a["invoice_ids"]))
        missing = sorted(set(a["invoice_ids"]) - {r["id"] for r in rows})
        if missing:
            raise ValueError(f"invoice id(s) {missing} do not exist")
    else:  # "everything paid since X": the set is computed here, so it can be far larger than any LLM context
        rows = odoo.search_read("account.move", [["move_type", "=", "out_invoice"], ["state", "=", "posted"],
                                ["payment_state", "in", ["paid", "in_payment"]], ["invoice_date", ">=", a["invoice_date_from"]]],
                                FACT_FIELDS, limit=MAX_SCAN, order="invoice_date desc, id desc")
    ids = [r["id"] for r in rows]
    refunded = defaultdict(float)
    if ids:
        for r in odoo.search_read("account.move", [["move_type", "=", "out_refund"], ["state", "=", "posted"],
                                  ["reversed_entry_id", "in", ids]], ["reversed_entry_id", "amount_total"], limit=MAX_SCAN):
            refunded[r["reversed_entry_id"][0]] += r["amount_total"]
    today_rows = odoo.search_read("account.move", [["move_type", "=", "out_refund"], ["state", "=", "posted"],
                                  ["invoice_date", "=", today.isoformat()]], ["amount_total"], limit=MAX_SCAN)
    invoices = [{
        "id": r["id"], "name": r["name"], "partner": r["partner_id"][1] if r["partner_id"] else "",
        "move_type": r["move_type"], "state": r["state"], "payment_state": r["payment_state"] or "",
        "amount": r["amount_total"], "refunded": round(refunded.get(r["id"], 0.0), 2),
        "age_days": (today - date.fromisoformat(r["invoice_date"])).days if r["invoice_date"] else 99999,
        "journal_id": r["journal_id"][0] if r["journal_id"] else None,
    } for r in rows]
    return {"request": {"reason": a["reason"], "selector": "ids" if "invoice_ids" in a else "invoice_date_from"},
            "context": {"today": today.isoformat(), "refunded_today": round(sum(r["amount_total"] for r in today_rows), 2)},
            "invoices": invoices}


def _decide(facts, ctx) -> dict:
    policy = ctx.get("policy")
    return policy.decide(facts) if policy else denied_unavailable()


def _p_refunds(odoo, a, ctx):
    facts = _refund_facts(odoo, a, ctx)
    d = _decide(facts, ctx)
    if not d["allow"]:
        raise PolicyDenied(d)
    rows = [{"invoice": i["name"], "customer": i["partner"], "amount": i["amount"], "age_days": i["age_days"]}
            for i in facts["invoices"]]
    return {"summary": f"REFUND {d['count']} invoice(s), total {d['total']:,.2f}, reason: {a['reason'].replace('_', ' ')}",
            "details": {"refunds": rows, "policy": {
                "result": "allowed", "decision_id": d.get("decision_id"), "version": d.get("policy_version"),
                "refunds_today_after": d.get("exposure_today"), "daily_cap": d.get("daily_cap")}},
            "warnings": ["Posts a credit note in Odoo for each invoice", "Policy is checked again at execution"],
            "risk": d["risk"]}


def _r_refunds(odoo, a, ctx):
    facts = _refund_facts(odoo, a, ctx)
    d = _decide(facts, ctx)
    if not d["allow"]:
        reasons = "; ".join(sorted({v["msg"] for v in d["violations"]}))
        raise PermissionError(f"policy re-check denied at execution ({d.get('decision_id')}): {reasons}"[:300])
    done = []
    for inv in facts["invoices"]:
        wid = odoo.call("account.move.reversal", "create", [{"move_ids": [(6, 0, [inv["id"]])], "journal_id": inv["journal_id"],
                        "reason": a["reason"].replace("_", " ")}])
        odoo.call("account.move.reversal", "refund_moves", [[wid]])
        cn = odoo.search_read("account.move", [["reversed_entry_id", "=", inv["id"]], ["move_type", "=", "out_refund"],
                              ["state", "=", "draft"]], ["id"], limit=1, order="id desc")
        if not cn:
            raise RuntimeError(f"Odoo did not create a credit note for {inv['name']}")
        odoo.call("account.move", "action_post", [[cn[0]["id"]]])
        name = odoo.search_read("account.move", [["id", "=", cn[0]["id"]]], ["name"], limit=1)[0]["name"]
        done.append({"invoice": inv["name"], "credit_note": name, "amount": inv["amount"]})
    return {"refunds": done, "policy_decision_id": d.get("decision_id")}


def _v_list_invoices(a):
    out = {}
    if a.get("name"):
        out["name"] = _text(a["name"], "name", 30)
    if a.get("partner_id"):
        out["partner_id"] = _int(a["partner_id"], "partner_id")
    if a.get("payment_state"):
        if a["payment_state"] not in {"paid", "unpaid"}:
            raise ValueError("payment_state must be paid or unpaid")
        out["payment_state"] = a["payment_state"]
    return out


def _v_query(a):
    return {"query": _text(a.get("query"), "query", 60)}


def _v_list_orders(a):
    out = {}
    if a.get("name"):
        out["name"] = _text(a["name"], "name", 30)
    if a.get("partner_id"):
        out["partner_id"] = _int(a["partner_id"], "partner_id")
    if a.get("state"):
        if a["state"] not in {"draft", "sent", "sale", "cancel"}:
            raise ValueError("state must be draft, sent, sale or cancel")
        out["state"] = a["state"]
    return out


S, I = {"type": "string"}, {"type": "integer"}
TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("search_partners", "Find customers/contacts by name or email.",
         {"type": "object", "properties": {"query": S}, "required": ["query"]}, False, _v_query, _search_partners),
    Tool("search_products", "Find sellable products by name or code; returns list prices.",
         {"type": "object", "properties": {"query": S}, "required": ["query"]}, False, _v_query, _search_products),
    Tool("list_customers", "List our customers (up to 25).",
         {"type": "object", "properties": {}}, False, lambda a: {}, _list_customers),
    Tool("list_products", "List sellable products with list prices (up to 25).",
         {"type": "object", "properties": {}}, False, lambda a: {}, _list_products),
    Tool("list_sales_orders", "List recent sales orders, optionally by order number (name, e.g. S00007), partner or state. Use it to find an order's id.",
         {"type": "object", "properties": {"name": S, "partner_id": I, "state": {"type": "string", "enum": ["draft", "sent", "sale", "cancel"]}}},
         False, _v_list_orders, _list_orders),
    Tool("get_sales_order", "Get one sales order with its lines.",
         {"type": "object", "properties": {"order_id": I}, "required": ["order_id"]}, False, _v_order_id, _get_order),
    Tool("list_receivables", "List unpaid customer invoices with outstanding amounts.",
         {"type": "object", "properties": {}}, False, lambda a: {}, _receivables),
    Tool("list_invoices", "List recent posted customer invoices (newest first), optionally by invoice number (name, "
         "e.g. INV/2026/00012), partner or payment state. Use it to find an invoice's id.",
         {"type": "object", "properties": {"name": S, "partner_id": I, "payment_state": {"type": "string", "enum": ["paid", "unpaid"]}}},
         False, _v_list_invoices, _list_invoices),
    Tool("create_partner", "PROPOSE creating a new contact. Needs human approval before it happens.",
         {"type": "object", "properties": {"name": S, "email": S, "phone": S}, "required": ["name"]},
         True, _v_partner, _r_partner, _p_partner),
    Tool("create_quotation", "PROPOSE a draft quotation for a customer. Needs human approval before it happens. "
         "Use real ids from search_partners / search_products.",
         {"type": "object", "properties": {"partner_id": I, "lines": {"type": "array", "items": {
             "type": "object", "properties": {"product_id": I, "quantity": {"type": "number"}}, "required": ["product_id", "quantity"]}}},
          "required": ["partner_id", "lines"]}, True, _v_quote, _r_quote, _p_quote),
    Tool("confirm_sale_order", "PROPOSE confirming a draft quotation into a sale order. High risk; needs human approval.",
         {"type": "object", "properties": {"order_id": I}, "required": ["order_id"]}, True, _v_order_id, _r_confirm, _p_confirm),
    Tool("cancel_sale_order", "PROPOSE cancelling a sale order. High risk; needs human approval.",
         {"type": "object", "properties": {"order_id": I}, "required": ["order_id"]}, True, _v_order_id, _r_cancel, _p_cancel),
    Tool("issue_refunds", "PROPOSE refunding customer invoices in full (one credit note each). Give EITHER invoice_ids "
         "(from list_invoices) OR invoice_date_from to refund every paid invoice dated on or after that date. "
         "A refund policy checks every request against Odoo's records; if it blocks one, explain why to the user.",
         {"type": "object", "properties": {"invoice_ids": {"type": "array", "items": I}, "invoice_date_from": S,
                                           "reason": {"type": "string", "enum": REASONS}}, "required": ["reason"]},
         True, _v_refunds, _r_refunds, _p_refunds),
]}


def declarations() -> list[dict]:
    return [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in TOOLS.values()]
