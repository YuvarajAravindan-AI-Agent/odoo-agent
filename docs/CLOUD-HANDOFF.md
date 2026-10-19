# Handoff to the cloud session: finish the testing story for the refund-policy agent

Repo: YuvarajAravindan-AI-Agent/odoo-agent (private). Branches: `refund-policy-gate` (the OPA gate, pushed),
`testing-evidence` (this work, on top of it). Read `docs/TESTING.md` and `docs/PROBES.md` first.

## Already done on the desktop (do not redo)
- `tests/mutation_check.py`: 19 deliberate policy bugs, each must make `opa test` fail. First run 13/19, now 19/19.
- 6 boundary tests added to `policy/refunds_test.rego` (21 total); pytest suite is 28 tests, all passing.
- `.github/workflows/ci.yml` written (opa test, pytest, mutation check). NOT yet seen running on GitHub.
- Live adversarial probes run on the public sandbox; demo video built; LinkedIn post and a reply in the
  discussion thread published from the desktop (needs the user's logged-in Chrome, so cloud cannot do it).

## For you (cloud) to do, in this order
1. **Open a PR** `testing-evidence` -> `refund-policy-gate` (and note `refund-policy-gate` is itself unmerged into
   the default branch). Watch the `tests` workflow. Likely failure points: the OPA download URL, Python deps.
   Fix in the workflow, not by loosening tests. Report the run URL.
2. **Mutation-test the Python gate** the same way: `pip install mutmut`, target `app/` (gate, policy client, tools),
   run against `tests/`. For each surviving mutant either add a test that kills it or write why it is equivalent
   in `docs/TESTING.md`. Keep a count before/after like the Rego one.
3. **Property test the policy** with `hypothesis`: generate random invoice batches and contexts, call
   `opa eval -d policy 'data.refunds.decision'` (or the OPA Python wheel), and compare `allow` to an independent
   pure-Python oracle written from the written limits in `policy/data.json`. Any disagreement is a finding.
4. **Turn `docs/PROBES.md` into `tests/live_probes.py`**: signs up a throwaway profile on the sandbox
   (`$SANDBOX_URL/signup`), sends each probe to `/chat`, asserts on the returned `blocked[].counts` and on
   proposals, never on the model's wording. Skip cleanly when `SANDBOX_URL` is unset. Add a nightly workflow
   (`schedule:`) that runs it. Secrets/credentials: none in the repo; the profile is generated per run.
   Never print tokens. The sandbox resets nightly at 21:30 UTC, so schedule at 22:30 UTC.
5. Update `docs/TESTING.md` with the new numbers, push, and give the user the run links.

6. Verify `.github/CODEOWNERS` is picked up (open a PR touching `policy/` and confirm the owner is requested
   as reviewer). Do not change repository settings yourself; tell the user which switches are still off.

## Needs the desktop or the user (do not attempt from cloud)
- Anything on the Contabo box (`ssh contabo-tally`), the user's logged-in LinkedIn, local video files in
  `~/Videos/agent-testing-demo`.
- Merging to the default branch: ask the user first.
- Turning on branch protection (require Code Owner review, required `tests` check): a repo setting the user does.

## Definition of done
CI green on GitHub for `testing-evidence`; mutmut report summarised; hypothesis test passing or its
disagreements listed as issues; nightly live-probe workflow present; docs updated; nothing secret committed.
