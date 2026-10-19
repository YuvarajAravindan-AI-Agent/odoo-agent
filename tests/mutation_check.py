"""Do the policy tests actually test anything?  python tests/mutation_check.py

Applies one small deliberate bug at a time to a COPY of policy/refunds.rego and runs `opa test`.
A mutant that still passes ("survived") means the suite has a blind spot. Exit 1 if any survive.
"""
import pathlib, shutil, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
POLICY = ROOT / "policy"

# (name, text to find, replacement) -- each must change refunds.rego exactly once
MUTANTS = [
    # Not listed: dropping well_formed from `allow` is an equivalent mutant, because `violations` already
    # contains malformed_input whenever well_formed fails (two independent fail-closed paths on purpose).
    ("window: > becomes >=", "i.age_days > lim.window_days", "i.age_days >= lim.window_days"),
    ("window: > becomes <", "i.age_days > lim.window_days", "i.age_days < lim.window_days"),
    ("batch limit: > becomes >=", "count(invoices) > lim.max_batch", "count(invoices) >= lim.max_batch"),
    ("batch limit: rule removed", "count(invoices) > lim.max_batch", "false"),
    ("daily cap: ignores refunds already posted", "exposure_today > lim.daily_cap", "total > lim.daily_cap"),
    ("daily cap: > becomes >=", "exposure_today > lim.daily_cap", "exposure_today >= lim.daily_cap"),
    ("per-refund limit: > becomes >=", "i.amount > lim.per_refund_max", "i.amount >= lim.per_refund_max"),
    ("per-refund limit: rule removed", "i.amount > lim.per_refund_max", "false"),
    ("already refunded: > becomes >=", "i.refunded > 0", "i.refunded >= 0"),
    ("already refunded: rule removed", "i.refunded > 0", "false"),
    ("paid states: in_payment no longer counts", '{"paid", "in_payment"}', '{"paid"}'),
    ("unpaid check inverted", "not i.payment_state in paid_states", "i.payment_state in paid_states"),
    ("reason check inverted", "not input.request.reason in lim.reasons", "input.request.reason in lim.reasons"),
    ("posted check inverted", 'i.state != "posted"', 'i.state == "posted"'),
    ("vendor bills allowed", 'i.move_type != "out_invoice"', "false"),
    ("high-risk threshold: > becomes >=", "total > lim.approval_high_over", "total >= lim.approval_high_over"),
    ("missing amount no longer malformed", "\tis_number(i.amount)\n", ""),
    ("missing refunded_today no longer malformed", "\tis_number(input.context.refunded_today)\n\tis_string", "\tis_string"),
    ("empty request allowed", "count(invoices) == 0", "false"),
]


def opa_test(directory):
    return subprocess.run(["opa", "test", str(directory)], capture_output=True, text=True).returncode == 0


def main():
    src = (POLICY / "refunds.rego").read_text()
    if not opa_test(POLICY):
        sys.exit("baseline policy tests do not pass; fix those first")
    survived = []
    for name, find, repl in MUTANTS:
        if src.count(find) != 1:
            sys.exit(f"mutant '{name}': pattern found {src.count(find)} times, expected 1")
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(POLICY, tmp, dirs_exist_ok=True)
            (pathlib.Path(tmp) / "refunds.rego").write_text(src.replace(find, repl))
            killed = not opa_test(tmp)
        print(f"{'KILLED  ' if killed else 'SURVIVED'}  {name}", flush=True)
        if not killed:
            survived.append(name)
    print(f"\n{len(MUTANTS) - len(survived)}/{len(MUTANTS)} mutants caught")
    sys.exit(1 if survived else 0)


main()
