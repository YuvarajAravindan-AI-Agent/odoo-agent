# opa test policy/ -v   (runs against the real limits in data.json)
package refunds_test

import data.refunds

inv(name, amount, age) := {
	"id": 1, "name": name, "partner": "Northwind Traders", "move_type": "out_invoice",
	"state": "posted", "payment_state": "paid", "amount": amount, "age_days": age, "refunded": 0,
}

req(reason, today, invs) := {"request": {"reason": reason}, "context": {"refunded_today": today}, "invoices": invs}

test_single_eligible_refund_is_allowed if {
	d := refunds.decision with input as req("damaged_goods", 0, [inv("INV/1", 450, 10)])
	d.allow
	d.risk == "low"
	count(d.violations) == 0
}

test_bulk_refund_of_everything_is_denied if {
	invs := [inv(sprintf("INV/%v", [n]), 300, 5) | some n in numbers.range(1, 250)]
	d := refunds.decision with input as req("late_delivery", 0, invs)
	not d.allow
	d.count == 250
	d.counts.batch_limit == 1
	d.counts.daily_cap == 1
}

test_unpaid_invoice_is_denied if {
	i := object.union(inv("INV/2", 450, 10), {"payment_state": "not_paid"})
	d := refunds.decision with input as req("damaged_goods", 0, [i])
	not d.allow
	d.counts.not_paid == 1
}

test_already_refunded_invoice_is_denied if {
	i := object.union(inv("INV/3", 450, 10), {"refunded": 450})
	d := refunds.decision with input as req("damaged_goods", 0, [i])
	d.counts.already_refunded == 1
}

test_old_invoice_is_outside_window if {
	d := refunds.decision with input as req("damaged_goods", 0, [inv("INV/4", 450, 61)])
	d.counts.outside_window == 1
}

test_window_boundary_is_inclusive if {
	d := refunds.decision with input as req("damaged_goods", 0, [inv("INV/5", 450, 60)])
	d.allow
}

test_large_single_refund_is_denied if {
	d := refunds.decision with input as req("damaged_goods", 0, [inv("INV/6", 2400, 3)])
	d.counts.over_limit == 1
}

test_unapproved_reason_is_denied if {
	d := refunds.decision with input as req("goodwill", 0, [inv("INV/7", 100, 3)])
	d.counts.reason == 1
}

test_daily_cap_counts_refunds_already_posted_today if {
	d := refunds.decision with input as req("damaged_goods", 4800, [inv("INV/8", 450, 3)])
	not d.allow
	d.counts.daily_cap == 1
	d.exposure_today == 5250
}

test_vendor_bill_cannot_be_refunded_as_customer_refund if {
	i := object.union(inv("BILL/1", 100, 3), {"move_type": "in_invoice"})
	d := refunds.decision with input as req("billing_error", 0, [i])
	d.counts.not_customer_invoice == 1
}

test_total_over_threshold_needs_high_risk_approval if {
	d := refunds.decision with input as req("damaged_goods", 0, [inv("INV/9", 600, 3), inv("INV/10", 600, 3)])
	d.allow
	d.risk == "high"
}

test_empty_request_is_denied if {
	d := refunds.decision with input as req("damaged_goods", 0, [])
	not d.allow
	d.counts.empty == 1
}

test_missing_input_fails_closed if {
	not refunds.allow with input as {}
	d := refunds.decision with input as {}
	d.counts.malformed_input == 1
}

test_missing_invoice_fact_fails_closed if {
	i := object.remove(inv("INV/11", 450, 10), {"refunded"})
	d := refunds.decision with input as req("damaged_goods", 0, [i])
	not d.allow
	d.counts.malformed_input == 1
}

test_string_amount_fails_closed if {
	i := object.union(inv("INV/12", 450, 10), {"amount": "450"})
	d := refunds.decision with input as req("damaged_goods", 0, [i])
	not d.allow
}

# ---- boundaries: each limit is inclusive, one past it is denied (found by tests/mutation_check.py)
test_batch_of_exactly_the_limit_is_allowed if {
	invs := [inv(sprintf("INV/%v", [n]), 10, 3) | some n in numbers.range(1, 5)]
	d := refunds.decision with input as req("damaged_goods", 0, invs)
	d.allow
	d2 := refunds.decision with input as req("damaged_goods", 0, array.concat(invs, [inv("INV/6", 10, 3)]))
	d2.counts.batch_limit == 1
}

test_daily_cap_exactly_reached_is_allowed if {
	d := refunds.decision with input as req("damaged_goods", 4550, [inv("INV/13", 450, 3)])
	d.allow
	d.exposure_today == 5000
}

test_refund_of_exactly_the_single_limit_is_allowed if {
	d := refunds.decision with input as req("damaged_goods", 0, [inv("INV/14", 2000, 3)])
	d.allow
	d.risk == "high"
}

test_total_of_exactly_the_high_risk_threshold_stays_low if {
	d := refunds.decision with input as req("damaged_goods", 0, [inv("INV/15", 1000, 3)])
	d.allow
	d.risk == "low"
}

test_invoice_in_payment_is_refundable if {
	i := object.union(inv("INV/16", 450, 10), {"payment_state": "in_payment"})
	d := refunds.decision with input as req("damaged_goods", 0, [i])
	d.allow
}

test_missing_refunded_today_alone_fails_closed if {
	d := refunds.decision with input as {"request": {"reason": "damaged_goods"}, "context": {}, "invoices": [inv("INV/17", 450, 10)]}
	not d.allow
	d.counts.malformed_input == 1
}
