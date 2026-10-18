# Refund policy. The agent proposes, OPA decides, a human approves.
#
# Every fact in `input` is read from Odoo by the enforcement point (app/tools.py), never taken
# from the LLM. The LLM only chooses WHICH invoices and a reason code. Limits live in data.json,
# so finance can change them without touching the agent.
package refunds

lim := data.refund_limits

paid_states := {"paid", "in_payment"}

# Defaults keep the decision defined for any input; well_formed below still denies bad input.
invoices := input.invoices if {
	is_array(input.invoices)
} else := []

refunded_today := input.context.refunded_today if {
	is_number(input.context.refunded_today)
} else := 0

total := sum([i.amount | some i in invoices; is_number(i.amount)])

exposure_today := refunded_today + total

money(x) := round(x * 100) / 100

# ---------------------------------------------------------------- input shape (fail closed)
# A rule that cannot evaluate adds no violation, so a missing fact would otherwise read as "allowed".
well_formed if {
	is_array(input.invoices)
	is_number(input.context.refunded_today)
	is_string(input.request.reason)
}

invoice_ok(i) if {
	is_string(i.name)
	is_string(i.move_type)
	is_string(i.state)
	is_string(i.payment_state)
	is_number(i.amount)
	is_number(i.age_days)
	is_number(i.refunded)
}

violations contains {"code": "malformed_input", "msg": "The request facts are incomplete."} if not well_formed

violations contains {"code": "malformed_input", "msg": msg} if {
	some idx, i in invoices
	not invoice_ok(i)
	msg := sprintf("Invoice fact #%v is incomplete.", [idx])
}

# ---------------------------------------------------------------- request-level rules
violations contains {"code": "empty", "msg": "No invoices matched the request."} if count(invoices) == 0

violations contains {"code": "batch_limit", "msg": msg} if {
	count(invoices) > lim.max_batch
	msg := sprintf("%v refunds in one request; the limit is %v.", [count(invoices), lim.max_batch])
}

violations contains {"code": "daily_cap", "msg": msg} if {
	exposure_today > lim.daily_cap
	msg := sprintf("Refunds today would reach %v; the daily cap is %v.", [money(exposure_today), lim.daily_cap])
}

violations contains {"code": "reason", "msg": msg} if {
	not input.request.reason in lim.reasons
	msg := sprintf("'%v' is not an approved refund reason.", [input.request.reason])
}

# ---------------------------------------------------------------- per-invoice rules
violations contains {"code": "not_customer_invoice", "invoice": i.name, "msg": msg} if {
	some i in invoices
	i.move_type != "out_invoice"
	msg := sprintf("%v is not a customer invoice.", [i.name])
}

violations contains {"code": "not_posted", "invoice": i.name, "msg": msg} if {
	some i in invoices
	i.state != "posted"
	msg := sprintf("%v is not posted.", [i.name])
}

violations contains {"code": "not_paid", "invoice": i.name, "msg": msg} if {
	some i in invoices
	not i.payment_state in paid_states
	msg := sprintf("%v has not been paid, so there is nothing to refund.", [i.name])
}

violations contains {"code": "already_refunded", "invoice": i.name, "msg": msg} if {
	some i in invoices
	i.refunded > 0
	msg := sprintf("%v was already refunded (%v).", [i.name, money(i.refunded)])
}

violations contains {"code": "outside_window", "invoice": i.name, "msg": msg} if {
	some i in invoices
	i.age_days > lim.window_days
	msg := sprintf("%v is %v days old; refunds are allowed within %v days.", [i.name, i.age_days, lim.window_days])
}

violations contains {"code": "over_limit", "invoice": i.name, "msg": msg} if {
	some i in invoices
	i.amount > lim.per_refund_max
	msg := sprintf("%v is %v; a single refund may not exceed %v.", [i.name, money(i.amount), lim.per_refund_max])
}

# ---------------------------------------------------------------- decision
default allow := false

allow if {
	well_formed
	count(violations) == 0
}

risk := "high" if {
	total > lim.approval_high_over
} else := "low"

counts[code] := n if {
	some v in violations
	code := v.code
	n := count([1 | some w in violations; w.code == code])
}

decision := {
	"allow": allow,
	"risk": risk,
	"count": count(invoices),
	"total": money(total),
	"refunded_today": money(refunded_today),
	"exposure_today": money(exposure_today),
	"daily_cap": lim.daily_cap,
	"counts": counts,
	"violations": violations,
	"policy_version": data.refund_policy_version,
}
