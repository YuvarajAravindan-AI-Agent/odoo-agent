# How this agent is tested

The model is the one part that cannot be unit-tested, so the design keeps it away from anything that
matters, and tests the parts around it. Four layers, cheapest first.

| Layer | What it proves | Where | Run |
|---|---|---|---|
| 1. Policy tests | Each rule, each boundary, and "any missing fact = no" | `policy/refunds_test.rego` (21) | `opa test policy -v` |
| 2. Mutation check | The tests would notice if a rule were broken | `tests/mutation_check.py` (19 mutants) | `python tests/mutation_check.py` |
| 3. Gate tests | The enforcement code: no write without approval, policy re-checked at execution, fail closed when OPA is down, tampered/expired/injected input | `tests/test_gate.py`, `tests/test_refunds.py` (28, Odoo and LLM faked) | `pytest -q` |
| 4. Live adversarial probes | The real model, real Odoo, real OPA, hostile prompts | the public sandbox | see `docs/PROBES.md` |

## Why mutation testing
Passing tests only show the tests agree with the code. The check applies one deliberate bug at a time
(`>` to `>=`, a rule removed, a check inverted, a required fact made optional) to a copy of the policy and
requires `opa test` to fail. First run: **13 of 19 caught**. The six survivors were real blind spots:
nothing tested "exactly at the limit" for batch size, daily cap, single-refund limit or the high-risk
threshold, nothing tested `in_payment` invoices, and a missing `refunded_today` fact alone was not
covered. Six boundary tests later: **19 of 19**. One mutant is deliberately not listed: dropping
`well_formed` from `allow` is equivalent, because `violations` already contains `malformed_input`.

## The bug the tests caught first
The first policy failed open. A rule that cannot evaluate simply adds no violation, so a missing fact
looked like "nothing wrong". Fix: an explicit `well_formed` check that turns any missing or mistyped fact
into a denial, with tests for it.

## Live probes (layer 4)
Hostile prompts against the sandbox, each ending in a decision id found in both the agent audit log and
OPA's decision log. Injection ("ignore the policy, maintenance mode") was blocked on six rules; "one at a
time until done" was cut short by already-refunded and daily-cap facts the server reads, not the model;
refunding unpaid invoices was blocked. What these cannot show is that the model's reasoning is right.
They show the damage is bounded when it is wrong.

## Who can change the rules and the tests
A coding agent helps write this repo, so the tests must not be editable by it alone. The control:
- `.github/CODEOWNERS` makes a human owner the required reviewer for `policy/`, `tests/` and `.github/`.
- In GitHub settings (Branch protection for the default branch) turn on: require a pull request, require
  review from Code Owners, require the `tests` check to pass, and do not allow bypass. The coding agent works
  on branches and never merges its own pull requests.
- The mutation check is the second line: weakening a test lets a planted bug survive and fails CI.

Status: CODEOWNERS is in the repo. The branch-protection switches are a repository setting, not a file, and
must be enabled by the owner; until then the review rule is a convention, not enforced.
