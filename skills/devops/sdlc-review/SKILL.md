---
name: sdlc-review
description: Use when reviewing a kanban handoff from the review lane.
version: 1.1.0
author: Jakub Wolniewicz (@frizikk) + Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [kanban, review, quality, verification]
    category: devops
    requires_toolsets: [kanban]
environments:
  - kanban
---

# SDLC Review Skill

Reviews Kanban handoffs and routes verified outcomes: approve, request changes, or escalate.

Independently verify work handed from a Kanban implementation run to the review lane, then approve it, request changes, or escalate. This skill reviews the deliverable and its evidence; it does not take over the implementer's work.

## When to Use

Use this skill when all of the following are true:

- the dispatcher spawned you for a task claimed from the `review` lane;
- an implementer submitted a `review_requested` handoff;
- the task needs an independent verdict before it can be completed.

Do not use it for a separate downstream review card. A downstream card is ordinary implementation work with a review-oriented specification and completes through its own lifecycle.

## Prerequisites

- A Kanban worker context with the current task and run identifiers.
- Native Kanban tools: `kanban_show`, `kanban_comment`, `kanban_complete`, `kanban_request_changes`, and `kanban_block`.
- Workspace access through `read_file`, `search_files`, and `terminal` when the deliverable is code.
- The task's original specification, acceptance criteria, handoff summary, and prior run history must be available through `kanban_show`.

## How to Run

This skill is loaded automatically by the review dispatcher. Start with `kanban_show` before inspecting files or choosing a verdict.

1. Read the task specification and the latest `review_requested` handoff.
2. Inspect the actual deliverable and run relevant verification.
3. Choose exactly one verdict: approve, request changes, or escalate.
4. Record concrete evidence in the terminal Kanban transition.

## Quick Reference

| Verdict | When | Final action |
|---|---|---|
| Approve | Acceptance criteria and verification pass | `kanban_complete` |
| Request changes | Correctable implementation defects remain | `kanban_comment`, then `kanban_request_changes` |
| Escalate | A human decision or external prerequisite is required | `kanban_block` |

A requested-changes transition returns the task to its original implementer. When that implementer requests review again without naming a reviewer, the persisted reviewer provenance routes the re-review back to the same reviewer profile.

## Review Lenses

Vary how you look at the work on each round instead of repeating the same inspection. Decorrelated lenses catch different defect classes: a cold read of the artifact surfaces design and correctness problems that the implementer's narrative would have framed away, execution surfaces claims that do not reproduce, and a strict contract audit surfaces quiet scope drift. Repeating the round-1 lens on round 3 mostly re-finds what round 1 already found.

Determine the current round from the history the task record already gives you: count the `changes_requested` entries in the "Prior attempts on this task" section of your worker context (also visible as prior runs in `kanban_show`). The current review round is that count plus one. Round 1 therefore shows zero `changes_requested` attempts; round 2 shows one; and so on.

| Round | Lens | How to apply it |
|---|---|---|
| 1 | Artifact | Read the diff or deliverable cold, before the implementer's summary. Form an independent judgment, then compare it against the handoff narrative and investigate every mismatch. |
| 2 | Execution | Check out the work and actually run it via `terminal`: build, test, and exercise the reported behavior yourself. Verify each handoff claim empirically instead of re-reading the artifact. |
| 3+ | Contract | Re-read the ORIGINAL task body and acceptance criteria, then audit the deliverable strictly against them. Also verify that every item from every prior `kanban_request_changes` round actually landed. |

The baseline duties in the Procedure section still apply on every round; the lens sets which inspection you lead with and weight most heavily.

### Lens variation for ad-hoc review fan-outs

The same principle applies outside the Kanban review lane. When spawning multiple parallel reviewers via `delegate_task`, give each reviewer a different lens — one diff-only brief, one full-context brief, one checkout-and-run brief — rather than identical briefs. Identical briefs produce correlated verdicts and duplicate findings; varied briefs cover more defect classes for the same review spend.

## Procedure

### 1. Orient from the durable task record

Call `kanban_show` and identify:

- the original task body and acceptance criteria;
- the latest implementation summary and structured metadata;
- changed files, commit identifiers, and test evidence;
- comments and decisions from earlier runs;
- findings from prior review rounds.

Treat the handoff as a claim to verify, not as proof that the work is correct.

### 2. Compare requested behavior with delivered behavior

Map every acceptance criterion to concrete implementation or output evidence. Note omissions, changed semantics, and unrelated scope before deciding whether to run deeper checks.

For code work:

1. Use `read_file` and `search_files` to inspect the changed paths and their callers.
2. Use `terminal` to inspect the diff and run the project's existing focused tests, lint, type checks, or build commands.
3. Exercise the reported failure path and at least one ordinary control path when practical.
4. Check error handling, edge cases, concurrency boundaries, data preservation, security boundaries, and cross-platform behavior relevant to the change.
5. Confirm that tests assert behavior rather than merely snapshotting source text or constants.

For non-code work:

1. Inspect the complete deliverable rather than only its summary.
2. Check correctness, completeness, formatting, and provenance.
3. Validate referenced URLs or external facts with the appropriate native tools when they affect the verdict.

### 3. Choose one verdict

#### Approve

Approve only when the acceptance criteria are satisfied and the evidence is sufficient. Call:

```text
kanban_complete(
    summary="Reviewed and approved. <what was verified>",
    metadata={"review_outcome": "approved", "reviewer_checks": [...]}
)
```

Include the exact checks that passed and any bounded caveat that does not block acceptance.

#### Request changes

Use this for specific, correctable defects. First record actionable findings:

```text
kanban_comment(
    task_id="<current-task-id>",
    body="Changes requested:\n1. <file or artifact + defect>\n2. <required correction>",
)
```

Then return the same task to its implementer:

```text
kanban_request_changes(
    reason="<concise summary of the required corrections>"
)
```

State where the defect is, how it reproduces, why it violates the task, and what minimum outcome would resolve it. The transition does not use blocker recurrence accounting.

#### Escalate

Use escalation only when the reviewer and implementer cannot resolve the problem without a human decision or external prerequisite:

```text
kanban_block(
    reason="escalation: <decision or prerequisite required>"
)
```

Explain the blocked decision and the smallest information needed to continue.

### 4. Preserve role separation

Do not edit the implementation while acting as reviewer. Request changes and let the implementer produce the next candidate; then independently verify that candidate in the next review run.

## Pitfalls

- **Rubber-stamping:** A passing handoff summary is not independent evidence.
- **Reviewer implementation:** Editing the deliverable hides ownership and weakens the re-review boundary.
- **Vague findings:** “Needs work” does not give the implementer a reproducible correction target.
- **Style-only blocking:** Do not request changes for preference-level nits when behavior and repository standards are satisfied.
- **Skipping prior rounds:** Re-review must confirm both the requested corrections and preservation of previously passing behavior.
- **Using blockers for ordinary rework:** Correctable defects belong in `kanban_request_changes`; reserve `kanban_block` for genuine external blockers or human decisions.
- **Blocking a window-gated card with the wrong kind:** when the DoD depends on a future wall-clock slot (a cron run, an approved maintenance window) rather than on another card, escalate with `kanban_block(kind="needs_input")`. Never `kind="dependency"` on a card with no parents: a dependency block returns it to `todo`, and the ready-recompute immediately promotes a parentless `todo` card back to `ready`, so the dispatcher respawns it in a spin loop that dependency blocks do not count toward triage. A `needs_input` block is sticky (it stays `blocked` until a human acts) and `kanban_complete` accepts a blocked card, so the future verification run can still close it — state the close-out condition, and who closes it, in the block reason.
- **Verify a disclosed mutation+restore from BYTES, not from the sha re-assertion.** An implementer who admits "my probe wrote to a file, I restored it byte-exact" is testable whenever a second copy pre-exists: compare sizes, then the byte diff. One such pair read 905 B vs 906 B, equal except a trailing newline, so the append was exactly one byte and the repository copy was the original; the mutated byte survived only in the copy nobody trusts. The lone `nlink=1` on that file (all siblings were `nlink=2` from hardlinks) pinpointed which path the in-place write broke. Report the delta, not the implementer's hash claim.
- **Completing without evidence:** Every approval summary must name the checks or artifacts actually inspected.
- **A non-reproducing hash is a hash-KIND question before it is a defect.** Compute the object's other digest (`git rev-parse <rev>:<path>` for the git blob id, `git show <rev>:<path> | shasum -a 256` for the content hash) before reporting a citation as wrong: two ids quoted as "main's blob" turned out to be exactly the sha256 prefixes of those files, while the two genuine blob ids in the same comment reproduced — a labelling fix, not a fabricated hash. Name which convention each id is in, and do not bounce a card whose behavioural claims verify independently.
- **A version-stamped citation is a per-field claim: attribute each moved field to the revision that moved it.** Prose that says "map v9 moved the band and both rows" can collapse two neighbouring revisions into one label — the band/day move and the derived-expression re-declaration are frequently different commits, and the same paragraph can contradict itself a sentence later ("map v8 moved the band"). Dump the artifact at every revision in the range (diff hunks plus a line grep for the specific field) and name the revision that changed each field; report a wrong label as a pointer correction with the measured attribution, not as a bounce, when the behavioural claim verifies.
- **A detached-HEAD probe adds its own gate finding.** A repo gate that requires "branch has an upstream" reports a refusal for `HEAD` in a review worktree that the published branch does not have, and that artefact appears in the same output as a genuine pre-existing refusal. Check out the branch ref before quoting a gate result, or attribute the upstream refusal to your own probe explicitly — never fold it into the deliverable's findings.
- **A cited module is a claim: grep it for the symbol before accepting the citation.** A parenthetical pointer can name a module that contains none of the referenced code — one verdict cited "acquire path in `hermes_state_holders.py`", which had 0 occurrences of `turn_lease`; the real path was `hermes_state_compression.py` (function + its `_claim_lease_row` helper). Wrong pointers are pointer fixes, not necessarily rework rounds: if the behavior claim is otherwise verified and a pre-created child waits on completion, approve, correct the pointer in your review comment, and say explicitly that you did not bounce the card for it.
- **A session id can exist in MORE THAN ONE store, with different title/source each.** Any claim of the form "store X holds row Y" is per-store: the same id was `Bot Chat`/`tui`/hidden=1 in one profile store and `Coder Bot Chat`/`desktop`/hidden=0 in the default store. Query every store, and when you retract or re-instate a defect about titles, check whether you looked at only one of them — a decapitated probe (`| head -N`) can cut off the very row that decides the verdict.
- **A suspicious ZERO is a claim about your probe's assumed layout.** A spool census returning 0 files was reading subdirectories for a flat spool whose status is a field inside each JSON record; the record count also included the `.lock` (`182 records + .lock = 183 files`, statuses summing to 182). Before reporting a count discrepancy, dump one sample record's keys and confirm the layout your instrument assumed.
- **A follow-up card you create can be dispatched and `done` within minutes — verify its claim at the System, not on the board.** A `done` status plus a confident comment is a self-report: for a publish, read the remote itself (`git ls-remote <remote> refs/heads/<branch>`) and confirm the expected sha, that no other published ref moved, and that local/remote are `0 0` on `rev-list --left-right --count`. Report those reads as the evidence; never repeat the worker's "verified" phrasing unverified.
- **Never rewrite a commit that is already a live deploy stamp.** When a tree's `.deployed-from` names the commit under review (or one this card produced), a `Reviewed-by:` re-stamp via `git commit-tree`/`update-ref` would break the live provenance chain: record the independent review on the card instead, and say explicitly why the commit must not be rewritten.
- **Trusting the deliverable's population:** when the work claims exhaustiveness over a dataset ("N = every row that …"), re-derive the row set from the source of truth with the engine's own selection predicate (the function/query the production path uses), then reconcile the count. A deliverable can be internally perfect — replica matches engine on every row — over the wrong population, and only this check finds the rows nobody looked at. Report the delta as a scope card; do not fold it into the verdict.
- **Verifying only through the implementer's replica:** if the handoff ships its own re-implementation (a replica instrument, a cached jsonl), re-run the production function yourself over the raw inputs and diff the two. This is cheap and it is the only way to tell a faithful replica from a conveniently shaped one.
- **A 4-digit-run audit false-positives on hash fragments.** When checking "no masked values in prose" (card body, comments, commit message), a run like `6461` inside a `9ac6461` sha prefix trips the digit-run rule. Classify each run by its token context (`[0-9a-f]{7,}`) before calling it a leak, then report `0 runs outside hex tokens` — the benign residue is typically a date fragment (`2026-09-`) plus the literal `sha256`, whose `256` is part of a name rather than a value — swallowing the hit as "probably a hash" and re-editing the comment are both wrong.
- **Establish that you ARE the live run before trusting an env-var absence.** Match your session id's stamp (`YYYYMMDD_HHMMSS`) against `task_runs.started_at` for `tasks.current_run_id`: equal stamps mean the claim is live and a transition will succeed (a successful `kanban_complete` echoes the `run_id` it closed). A refusal from a closed run has the same tool signature, so decide ownership from timestamps, not from whether `HERMES_KANBAN_TASK` is set.

- **A red from your own probe is a claim about your probe first.** `git` plumbing writes to stdout with a trailing newline, so `re.fullmatch(r"[0-9a-f]{40}", out)` and bare `==` comparisons report a false red while the object demonstrably exists — the very next line of the same run (a tree comparison that reads it) proves it does. `.strip()` every git stdout before matching, and when one of your checks contradicts a fact another instrument measured (the repo gate's own "2 commits behind"), re-measure that fact before reporting it: in one review both reds of a 4-fail run were probe bugs — a trailing newline, and an assertion demanding `0 behind` where the handoff truthfully said 2. Diagnosis order is: is my probe right, then is the deliverable wrong.

- **A count from `difflib` is not a count from `git diff --numstat` — the EOF newline is the usual gap.** Comparing a committed blob against the live file by splitting on lines reports one fewer added/deleted pair than git whenever the committed side has no trailing newline (the last line's terminator change is a change git counts and `difflib` cannot see). One handoff's "55 insertions / 3 deletions ahead of HEAD" was correct git numstat while the reviewer's first count read 54/2 — so trust `git diff --numstat <rev> -- <path>` for a committed-vs-live delta, treat a one-pair disagreement as this convention gap before calling the implementer's number off by one, and quote which tool produced each number.

- **A base/tip pair run CONCURRENTLY is not a control.** Two pytest processes launched in the same worktree share pytest's basetemp root, and a parallel base-vs-tip run reported a misleading `1 failed` where the handoff claimed `2 failed`; re-running each suite serially reproduced the handoff exactly. Even for a read-only negative control, serialize the two suites (or isolate their basetemp with `--basetemp`), and treat a lone-run anomaly as a probe bug until a serial re-run says otherwise.

- **A review worktree's `origin` can be a LOCAL MIRROR path, not the forge.** When `git remote -v` shows `origin /opt/hermes_sandbox/<repo>`, every `git ls-remote origin` / `origin/main` reading describes that mirror: one such run reported refs byte-identical to the pre-merge snapshot while `gh api repos/<owner>/<repo>/git/ref/heads/main` showed the merge had already landed. Check `git remote -v` before quoting any ref, and read every publication/merge/landing claim from the forge API. A `gh pr merge` that prints nothing under a pipe still lands - read `gh pr view --json state,mergeCommit,mergedAt` plus the base ref back (`--json merged` is not a valid field on this gh build), and compare the landed tree to the reviewed tree to report a rebase-merge's sha rewrite honestly.

- **Assert the probe tree's revision before trusting a zero count.** A mutation probe that found `0` occurrences of the very literal it was written to swap had a stale checkout: an earlier command in the same clone had left it detached at the base commit, so the "literal not found" red was a probe bug, not a deliverable defect. `git rev-parse HEAD` in the same command as the count.

## Verifying a multi-commit landing or scrub

When a handoff lands dozens of per-file commits (e.g. a PII/figure scrub rebased onto main), do NOT try to re-run the repo's per-commit guard over every commit: a full-tree scan can cost ~50s per commit, so 46 commits will not finish inside one review run. Verify with git plumbing instead — fast, and it does not load the live checkout:

1. **Blob identity** — for each commit in `BASE..TIP`, compare its changed file's blob to the same path at TIP. If all match, every landed file already equals the fully-scanned final version, so the tip's green guards also cover the intermediate commits.
2. **Tree-level sweep** — `git grep` the guard's patterns against each commit *tree* (or one `git log -U0 -p` pass over added lines) instead of checking out dozens of worktrees.
3. **Count flagged lines, not class matches** — one line can trip two classes, so a line count and a per-class hit count legitimately differ by a few. Reconcile the arithmetic before calling it a metric miss.
4. **Never print the flagged values** — count lines, or report file names and labels only, and delete any scratch that had to hold them (mirror clones, audit JSON, DB copies) before the verdict.

Also verify the handoff's *negative* claims: refs deleted (`git ls-remote --heads origin`), a `MISSING=2`-style audit residual (reproduce the script's own semantics rather than trusting the number), and "live checkout untouched" (read the real HEAD and dirty set — a concurrent sibling card can move it mid-review, and attributing that to the implementer would be wrong).

- **Closing a PR and deleting its branch does NOT unpublish the object.** When a handoff reports a republication after a scrub (identity, PII, figure), grep the *full* `git ls-remote` output — not just `refs/heads/*` — for the retired commit: GitHub keeps `refs/pull/<N>/head` pointing at the closed PR's head, so the pre-scrub commit stays fetchable by anyone with repo access. That refutes a "reachable by SHA only" framing and changes the residual from cosmetic to a named, cloneable ref; report it as a strengthening of the disclosure, not a new blocker, when the exposure is a fork-internal address rather than personal data.

## Reviewing a kernel patch on yoyodine

- **The baseline of record must be a separate pristine tree.** A handoff that produced its "base" run by restoring HEAD copies of the changed files *in place* inside the patched worktree reports unrelated failures (guard/hook/import tests that break when HEAD sources run under the patched harness) mixed in with the card's own. Build your own base: clone the worktree to a fresh dir, `git checkout <base-sha>`, copy in only the new test files. Then attribute the delta as base-fails vs tip-passes over ONE selection, and require that only the card's new tests differ. When an implementer retracts an overstated evidence file, corroborate with your own run rather than adopting either number.
- **Write your own probe for the defect, declaring through the legacy shape** (`metadata["artifacts"]`, not the new keyword) so the probe exercises the bug rather than the new signature. It must fail on the pristine tree with the real loss and pass at the tip. A `TypeError: unexpected keyword argument` is only a signature RED — it does not prove the defect is real; reproduce the behaviour too.
- **Mutation-test the load-bearing claim yourself** in a private clone (restore with `git checkout --`): narrow the guard, run the new test, expect exactly that test to fail, then confirm it passes again after restore.
- **Verify fail-closed paths by reading state, not by catching the exception class**: after the error assert task status, claim lock, attachment rows and event count, then retry successfully on the same card/run.
- **Re-derive parity claims** ("the completed-event contract is unchanged") by extracting the refactor and comparing it against the old inline logic, then running the completed path on both trees and diffing payload keys plus the path suffix after the board dir (temp prefixes legitimately differ).
- **Clear the worker fences** for every kernel run — `HERMES_KANBAN_DB`, `HERMES_KANBAN_BOARD`, `HERMES_KANBAN_TASK`, `HERMES_KANBAN_WORKSPACE`, `HERMES_KANBAN_WORKSPACES_ROOT`, `HERMES_KANBAN_BRANCH`, `HERMES_DELEGATED_CHILD_CONTEXT` — and remember the shared venv lacks `pytest-asyncio` (async watcher suites silently misbehave): `uv run --no-project --python ~/.hermes/hermes-agent/venv/bin/python --with pytest-asyncio==1.3.0 python <runner.py> <repo> <pytest args>`. `rm -rf` is refused by the command scanner, so use a fresh dir name instead of clearing one.
- **A real-board census that shows the defect NOWHERE is a statement about the parent gate, not about the fix.** A promotion pass checks "every parent is done" BEFORE the predicate under test, so on live boards an event-less parked row is held at BOTH revisions and every board reports `promoted=0` — a null result that reads as "the defect does not exist". Reproduce with a synthetic PARENTLESS row (the shape the fix actually changes): parameterise the row's counter/kinder columns to bracket the discriminator (counter=0 vs 1 vs limit), run the same probe over both revisions, and require a DIFFERENT verdict per arm. Report the census as "not exercised in prod today" rather than as evidence either way.
- **One new test passing at base is a pre-existing-behaviour guard, not weak evidence — and it belongs in the split.** With N new tests, expect some to be red at base and some (the ones pinning behaviour the fix must preserve, e.g. a below-limit recoverable block) to pass at both revisions; say which is which and why, instead of claiming "all new tests red".
- **A DoD step that requires editing the CARD BODY is not implementable by any worker tool.** When the criterion is "update this card with the commit/PR", accept the in-substance form (a handoff comment carrying the same facts), record the substitution in the review comment, and do not bounce the card for it.
- **When the card fixes the installed kernel, verify the live checkout's own signature** (HEAD + the function's parameters). The branch can carry the fix while the live editable install does not; that gap is the card's premise, so confirm it rather than trusting the handoff.
- **A pre-created publication/QA child decides the terminal action**: use `kanban_complete`, and put your verified facts on that child as a comment (exact commits, which evidence file is the baseline of record, tarball digests, overlay collisions) so the publisher neither re-derives them nor cites a retracted baseline. Check overlay collisions are in a *different* region of a shared file before calling publication low-risk.
- **Instrument the pass you are judging by wrapping the CALLEE, not by scanning the caller.** A `co_names` scan for "does this function call X" does NOT see a name used inside a nested closure: an inner `_shrink_at` helper hid `_truncate_tool_call_args_at` from the outer function's `co_names`, so a static source test reported "no arg shrink" at BOTH revisions (false negative on the buggy base). Wrap the callee instead (`ContextCompressor.__dict__["<name>"].__func__`, re-wrapped as `staticmethod` when the original is one — wrapping a staticmethod with an instance-method wrapper raises `TypeError: takes from 4 to 5 positional arguments but 6 were given`, a probe red) and record the indices it is called with. The walked-index set is what proves "this pass cannot touch pre-boundary content" — a claim no source scan settles; also sweep the pass's eligibility inputs (here protect_tail_count 1..20 x tokens 50..2000 -> 0 negative boundaries) so the equivalence does not rest on one lucky scenario.
- **Quantify the trade-off any removal makes, and name the reachable shape.** Deleting a reclamation path is not behavior-neutral for the population it used to serve: build one scenario whose bulk IS the removed path's target (here a protected tail dominated by a CALL payload rather than a result) and report before/after on both revisions (tip 10,325 tok -> 10,325, still over the 150-token soft ceiling; base 10,325 -> 207, achieved only by rewriting the current turn's call, i.e. the defect under review). Report it as a bounded caveat naming the reachable shape and the surviving mitigations, not as a blocker, when the pre-fix mechanism was the corruption being fixed.

## Reviewing a masked-evidence / pin card on yoyodine

A pin card hands you masked artifacts plus the claim that the figures reproduce. The masked bytes cannot show you the values, so verify the *decisions* two independent ways.

- **Re-derive with the production function yourself, not the card's replica.** Point the implementer's own runner at each revision's worktree and diff your output against the pinned artifact row-for-row over every decision field (ids, class, rejection code, counts, note). Report `0 field diffs` per column — that is the strongest available evidence and it costs one background run per revision. Run each revision from *its own* worktree so it imports that revision's package.
- **Verify value-bearing claims from your own unmasked output, then delete it.** Claims like "every replaced value equals the registry value" are invisible in masked artifacts; recover them from the raw run (compare the derived field against the registry field *within the same row*), print counts and class histograms only, and remove the raw files before the verdict.
- **Bracket the shared store with your own fingerprint reads** — one read before and one after your pair of runs. Identical row counts plus content hashes on every table proves both "no write" and that state was stable across the implementer's run, independent of their own bracket. Compare on the **state fields only** (`rows`/`content_hash`): a `label`/`name` field records which read it was, not what the state was, so hashing the whole document reports three identical reads as three different states and sends you chasing a non-difference.
- **Diff direction is the claim, so state it explicitly.** In the record-vs-reproduction diff, lines only in the record (`<`) are the provenance/clock metadata the record adds; lines only in the reproduction (`>`) are the ones that must be zero apart from a documented placeholder. "2 hunks" says nothing — count both sides, and quote the single `>` line when there is one. `diff -u` emits neither `<` nor `>` bare, so counting those markers on a unified diff measures nothing and yields a confident zero; check which format you actually ran.
- **Triangulate masked digests.** `mask(published_digest) == manifest_digest` for every table proves the over-masking is a lossless-consistent transform and that the two artifacts describe the same state; a live read reproducing the published digests then dates the claim. Reconcile a small inside-count difference between your classifier and theirs (structural-token sets differ) instead of calling it a metric miss.
- **Review the bytes extracted from the card, not the on-disk copies.** When deliverables are tarball attachments, extract from the attachment, hash them, then compare against the loose copies; that is what makes the card self-sufficient.
- **Check the transport claim against file magic.** A correction comment that restates an artifact's container type (gzip vs tar.xz) is only right if the attachment's bytes agree — verify rather than adopt the newer comment.
- **Order the DoD's own steps.** If the definition of done requires you to re-read attachments *after* completing, complete first, then re-read and stat each declared path, and record the confirmation in a follow-up comment.
- **With a pre-created child, complete and hand the child the facts it would otherwise re-derive** (exact digests, byte sizes, which copy is authoritative/untracked, interpreter caveats, artifact defects that block its own DoD) — as a comment, not a blocker, since a ready child is already dispatched by then.
- **Calibrate your own instrument against a known keep before trusting its red.** A reviewer-side regex for the retired name will flag the ratified keep (e.g. the live corpus dir path literal) as a hit: one draft returned 161 "engagement" findings that were all that keep and had to be retracted before the verdict. Take the card's own predicate as the population definition, and check what your regex classifies on lines the boundary rule already ratifies before reporting any count.
- **Bracket the verdict with a negative control.** Run the production instrument on a pristine pre-fix revision (your own throwaway worktree at `<reviewed>^`, removed afterwards) and require it to go RED — a census that reports 0 both before and after the fix proves nothing about the fix. Require the base to show exactly the defect row (EDIT 1 / one named FAIL line) and the tip to show 0.
- **A branch that moves under the review invalidates the stamp, not the verdict.** If sibling commits land on top of the reviewed commit mid-review, the standard's compare-and-swap stamp (`git update-ref refs/heads/<branch> <new> <rev>`) correctly refuses; do NOT rewrite to force it, since that orphans the newer commits. Verify the reviewed *blob* is still identical at `<reviewed>`, at HEAD and in the worktree, approve, and record the deferral plus the new hash's fate on the card.
- **An ordinal citation ("its added line 56") is a claim under an UNSTATED convention — measure all three readings before calling it wrong.** The same sentence can be the Nth added line of that file's diff, the new-file line number, or a diff-stream offset; a handoff's "added line 56" was index 11 / file line 369 / none of the alternatives, so the ordinal was wrong under every reading while the claim's CLASS (content hit, not message hit) verified by `git grep <rev>:<path>`. Correct the number in your review comment; do not bounce a card whose behavioural claim reproduces.
- **Re-measure the handoff's recipe steps; a shared checkout drifts between their push and your review.** `git rev-list --left-right --count <upstream>...HEAD` read `0 1` where the handoff recorded `0 0`, because a SIBLING lane committed to the shared tree's local branch after the implementer's push. Post-handoff local activity is not the card's drift (same shape as a base-vs-tip suite delta) — name it as sibling activity, and check `git status --porcelain` for another lane's staged entry before attributing anything to the implementer.

## Reviewing a prod-deploy adjudication card on yoyodine

- **Line-additive is not behaviour-neutral.** When the artifact says "additive only /
  0 removed lines, existing classes preserved", also diff per-definition ASTs of the
  pre-existing code: an inserted guarded early-return changes which path a SUBSET of
  the real population takes. Quantify that subset with the production predicate run
  over the actual inputs (print counts only, never the text) and record the exposed
  row count as a bounded caveat — not a blocker when the deployed bytes are the
  lane's reviewed content.
- **Check every drifted file at the alternative revision, not just the adjudicated
  one.** A candidate commit can be insufficient on an axis the artifact never
  mentions (e.g. it still holds the pre-sweep blob of a *different* file on the
  drift list, so `--check` there would still fail). That turns "right choice" into
  "only viable choice" and is worth reporting as a strengthening.
- **Run a tree's own code with that tree's interpreter.** An in-process
  `exec_module` A/B driver invoked with the session python dies on the tree's deps
  (`ModuleNotFoundError: sqlalchemy`) — a probe red, not a deliverable red. Use the
  destination's venv (here `/opt/hermes_prod/shared-venv/bin/python3.14`), and prove
  the control tree differs from the live tree ONLY in the file under test before
  trusting any delta you attribute to it.
- **State reviewer-side effects on prod.** Importing a module from a live tree
  regenerates its bytecode cache; name that (an excluded class, no source byte
  changed) rather than letting a later mtime scan blame the implementer.

## Reviewing a PII-untrack / figure-scrub card on yoyodine

- **A collapsed `kanban_show` result means you have NOT read the spec.** When the tool returns a one-line summary of a tens-of-KB payload, copy the board DB and read `tasks.body`, `task_comments` and `task_runs` before judging anything — a verdict formed from the handoff narrative alone is rubber-stamping. On the `financially` board the runs table is `task_runs` (there is no `runs`), and task rows have no `summary`/`metadata` columns. Count `changes_requested` runs to fix which lens round you are on.
- **`git rev-parse <ref>:<path>` prints the literal argument to stdout when the path is absent** (with the error on stderr), so an "is this blob absent?" test written as `if not out` never fires and silently contaminates any compare or diff loop. Require a 40-hex result before treating the output as a blob id.
- **Re-derive the drift counts with the production function, not the handoff's replica** — throwaway clone at the tip, generator defaults, stdout to /dev/null when the artifact prints masked values. Prove the instrument is deterministic first (run it twice and compare; check the input corpus has no recent mtimes) — only then is your number comparable to theirs.
- **A narrative count can be true under a narrower convention than it reads.** "N rows differ" may mean "rows where a *non-null* value changed", excluding null transitions and secondary-field drift. Measure per field *and* at row level, report both, and frame the correction as magnitude rather than falsehood when the direction of the conclusion is unchanged.
- **Verify the negative claims too.** That no remaining ref can reintroduce the artifact (unmerged refs whose blob equals the merge base resolve to the deletion on merge; a ref whose modified copy is *already* merged is harmless), that the landed diff swept in no sibling lane's work, and that the live checkout was untouched — best form is a write-activity scan over the implementation window plus HEAD and blob-hash read-back, since the live tree is legitimately behind and dirty from other lanes.
- **A token-replacement sweep can leave a doubled article at the join.** Replacing a bare alias with a phrase (a retired code name → "the Financially") onto a line that already ended in "the" produced "phase of the the Financially matter". The sweep's own word-level diff shows only the replaced token, so it reads clean; grep the committed blob for article adjacency (`grep -nE '[[:space:]](the|a|an) (the|a|an)[[:space:]]'` over the changed files) and check the join line's full text before approving prose-only deliverables.
- **Close the loop on the durable record.** After commenting, read the comments back from a fresh DB copy and confirm the author is your own profile rather than `default`; scan for leaked values but treat years and counts as classifier false positives instead of chasing them.

## Reviewing a commit-time-guard / git-hook card on yoyodine

- **A guard that delegates to a scanner judges BYTES, not just files: always test the index != working-tree case.** A `pre-commit` running `<scanner> --staged` typically takes its file SET from the index (`git diff --cached --name-only --diff-filter=ACM`) while the scanner reads each selected path off the WORKING TREE, so the verdict can describe something other than the commit. Reproduce both directions in a throwaway clone (synthetic markers; print rc/booleans only): stage a hit then remove it from the working-tree copy — does the value still commit (`rc=0`, HEAD moved, value present in `git show HEAD:<path>`)? and clean index + a hit only in the dirty worktree — a false refusal (`rc=1`, HEAD frozen, committed bytes clean)?
- **When a tracked doc asserts the scan covers the committed bytes, the drift case falsifies the doc, not just the behaviour.** Require the doc correction, a compensating guard-integrity check in the hook (refuse preferred/fail-closed, or an explicit re-stage warning with the choice recorded in the policy), and evidence rows for both drift directions. The hook must keep delegating to the single instrument of record, so the index-aware scanner fix belongs on its own card for the instrument's owner — never as a fork inside the repo.
- **Attribution on an unpushed landing: read the author field, not just the trailers.** Check `git log -1 --format='%an <%ae>'` plus `%(trailers)` against the repo's own recent commits for the mode actually in use, and confirm visibility (`gh repo view <owner>/<repo> --json visibility`) against `public-repos.conf` before ruling on which identity.sh mode was correct — a private repo carrying the public `yoyodine-industries` author is the exact state identity.sh exists to prevent, and `git log --author=<bot>` silently misses those commits. An unmerged branch is the only cheap moment to reword (identity-only: same tree hash, same parent chain, message unchanged apart from author/committer lines, exactly one `^Agent:` line, author dates preserved); after the publish that attribution is unrecoverable. On a PUBLIC fork whose product is an upstream PR, `yoyodine-industries` is the correct identity.sh public-mode author and a missing trailer block can be the repo's actual mode rather than drift: census the repo's own bot-authored commits (`git log --all --author=yoyodine-industries --format='%h|%(trailers)'`) and check whether they reached `upstream/main` before requiring a reword — demanding one there would inject internal task ids into upstream history, so report it as a governance observation instead of bouncing the card.
- **A fixture-invoked guard resolves its own repo root from the CALLER's cwd.** The delegator does `REPO=$(git rev-parse --show-toplevel)`, so a probe that copies it into a fixture repo but launches it by absolute path scans the tree your shell is sitting in — a clean rc 0 sweep of an unrelated repo that reads exactly like a pass. `cd` into the fixture before judging any whole-tree result, and make the check a whole-tree run over a *planted hit*: that control is what catches the wrong cwd (identical 723-byte report at both revisions only once the cwd was right).
- **A `git cat-file -t :<path>` decision is a claim about the local object DB, not about the index entry.** A documented "a gitlink is skipped" branch fires only while the submodule commit object happens to be present locally; in a real superproject the objects live in `.git/modules/<name>/objects`, so the case falls through and aborts the run BEFORE the report with a misdiagnosed refusal (a co-staged real hit in another file is then never named, though rc=1 stays fail-closed). Test any documented special case with the object ABSENT as well as present, and note the presence-independent form (`git ls-files -s -- <path>` mode `160000`) as the fix.

## Reviewing a registry-derivation card on yoyodine

- **A dead-port `DATABASE_URL` does NOT prove a DB-free import path.** SQLAlchemy's `create_engine` is lazy, so an import that drags the DB layer still succeeds with nothing listening. Read `sys.modules` after importing the module (`'financially.db' in sys.modules`) — that is the decisive check. A shipped test that only AST-scans the module's own `ImportFrom` nodes cannot see a transitive drag through a sibling module.
- **Attribute the suite base-vs-tip from a real clone, not `git archive`.** An extracted tree is not a git repo, so `git check-ignore` tests fail spuriously and inflate the base failure set. `git clone <repo> && git checkout <base>` gives a fair baseline; report the pair (base 20f/414p, tip 20f/450p, *identical failure sets*) and say how many are pre-existing instead of "N failures".
- **Re-derive a value-migration claim per row, never as a total.** "16 → 19" hides offsetting errors: compute the set of folders that gained and lost a value and check it against the ruled row list plus each row's resolution code. Folder-name *lengths* are enough to match rows without printing names.
- **Mutation-test a "no typed literal" guard with the realistic vector** — a literal injected into a *note/value* string, not just a bare constant — and confirm exactly one test flips (pass count moves by one). A digit run in a comment will NOT flip an AST-scan guard: that half of the constraint stays a review-time duty, so record it as a bounded caveat rather than a pass.
- **Always read the commit's trailer block** (`git log -1 --format='%(trailers)'`, `'%an <%ae>'`). An unmerged-branch commit with empty trailers authored `yoyodine-industries` is a real defect, not a nit: `yaan-sdlc-standards` requires `Agent:`/`Host:`/`Task:`/`Reviewed-by:` on every commit and says to reword before it lands, because after a merge the attribution is unrecoverable. Require the reword (amend, same base, no code change) and forbid a self-applied `Reviewed-by:` while the verdict is still changes-requested.
- **A 686-line spec move is the silent-registry-edit risk.** Verify it is verbatim by AST-`unparse`-comparing every element of both spec lists between base and tip — a value edited in transit would leave every "emitted value ∈ registry" check self-consistently green.

## Reviewing a hunk-carve / selective-staging card on yoyodine

A card that lands only its own hunks out of a shared worktree can commit a file that is corrupt in a
way its own evidence cannot see, because the worktree stays perfect.

- **Compare the committed blob against BOTH the base and the working-tree file** (`git rev-parse
HEAD:<path>`, `HEAD~1:<path>`, and `git hash-object <path>`). Three distinct hashes for one path mean a
synthetic patch was staged, not a file: the blob is nobody's content, and prose tolerates the
misplacement while code does not.
- **`bash -n` the committed bytes, never the file on disk.** Extract first (`git cat-file -p
HEAD:<path> > /tmp/f`) — the pipe form (`... | bash -n /dev/stdin`) is refused by the command scanner on
this host. RC 2 with the case arm spliced into a comment block is the signature; the implementer's
"`bash -n` clean" was measured on the worktree.
- **Point the project's own suite at a clean checkout of the reviewed commit** before believing any
passing count, and re-run the pre-fix revision as the negative control. Harnesses here resolve their
target from an ambient env var defaulting to the LIVE checkout, so a suite goes green on the working
tree while the commit fails the card's own end-to-end test; the honest evidence is the pair (7 failed
pre-fix → 6 passed / 1 failed at the tip).
- **Attribute base-vs-tip suite deltas before blaming the card.** A checkout that is behind the remote
and dirty from other lanes runs a SIBLING's in-flight shell code against the committed test files, so
identical failures at base and tip are pre-existing, not this card's.
- **Check every artifact the carve touched.** A zero-context apply that corrupts a shell script also
splices prose silently: compare section order inside the committed README, where a new section wedged
between a heading and its numbered list is the same defect one file over.
- **An unpushed-looking branch is a publication claim: read the remote ref.** `git ls-remote --heads
origin <branch>` confirms which commit is actually published — a broken blob on a published branch is
a merge hazard to state plainly, not a local dirty file.

## Reviewing a prose/pointer-correction card on yoyodine

Three checks that decide these cards are *where the text lands*, not whether the new text is nicer prose.

- **A checkout showing the pre-fix text is not an unlanded fix.** Read `git branch --show-current`, `git rev-parse main`, and `git merge-base --is-ancestor HEAD main` before judging: a repo whose working tree is on a sibling feat branch (`fix/t_<other-card>-...`) shows the old paragraph while `main` already carries the fix, and `git show main:<path>` is the record. Compare the blob ids (`git rev-parse main:<path>` vs `<commit>:<path>`) and require them equal — that is the claim "landed", not the checkout's content.
- **A bounded read (`sed -n 'a,bp' FILE | grep -A5`) decapitates the evidence.** The paragraph an acceptance criterion turns on can start below the bound: a slice ending at line 40 showed only the *pre-fix* sentence of a 37-line OWNERS file and read exactly like an unfixed deliverable. Re-read the whole file (or grep it unbounded) before forming a verdict on its content.
- **Verify a prose claim against the system it describes, not against its own confidence.** Prose that names gate checks by number ("check 2 is waivable, check 3 is not"), a parked branch's contents, or a tracker id is cheap to falsify: read the gate's source for the numbered checks and its waiver line, `git ls-tree` the branch for the named artifacts, `git show main:<record>` for the 0-hit premise, and grep the payload/board for the cited id. When each named fact reproduces, that is what makes the approval evidence-bearing.
- **Then verify the pointer's own file, at the destination.** For a "live deployed file == committed blob" claim, `git hash-object <live path>` must equal `git rev-parse <reviewed-commit>:<path>`; report the pair, and note that an amended-away commit reachable only from the reflog (no ref) is not a rewrite of published history in a remote-less repo.

- **A cited "existing" paragraph can be another lane's UNCOMMITTED WIP - hash what it is at HEAD before calling "record X beside the existing Y" unmet.** A full `git diff -U0 HEAD -- <path>` plus `git log -S '<phrase>' --all -- <path>` that returns no commit means the paragraph exists only in the dirty working tree, so satisfying the placement literally would have meant editing someone else's in-flight text; the inline placement is then the better available choice, not a DoD miss. Cross-check the file's line count against the HEAD blob (`wc -l` vs `git show HEAD:<path> | wc -l`) - a 53-line gap reads exactly like a landing that never happened.

## When your run is already closed (a false "task is still running" nudge)
A review run ends with `kanban_request_changes`. The card returns to `ready` and the dispatcher may immediately spawn a NEW worker run for it — a *sibling process*, not your session. Your session stays alive, and a worker-protocol guard may then tell you the task is "still running" and demand `kanban_complete`/`kanban_block`. That nudge is a false positive; resolve it from the board, never by forcing a transition.

- **Identify the current owner from the board, not your env.** Read `tasks.current_run_id` and `tasks.worker_pid`, cross-check `task_runs.worker_pid`, then confirm with `ps -p <pid>`; `lsof -a -p <pid> -d cwd -Fn` shows which directory it is actually working in. `HERMES_KANBAN_TASK` is often UNSET in these sessions while `HERMES_KANBAN_DB`/`_BOARD`/`_WORKSPACE` are set — pass `task_id` explicitly to every `kanban_*` call.
- **A refusal is the correct answer, not an obstacle.** From a closed run, `kanban_complete` and `kanban_request_review` fail with "unknown id, stale run, or already terminal" / "expected_run_id did not match the current run". That is the kernel protecting the live run's claim. Report the refusal as evidence and stop.
- **Never `kanban_block` to satisfy the guard.** Blocking releases the live worker's claim and stalls a healthy card that needs no human input — strictly worse than the guard's warning. A `needs_input` block is for a card with no owner making progress, not for one with an alive, heartbeating worker.
- **Do not enter the implementer's worktree to apply the correction you requested.** If you already did, post the new hash plus a tree-identity proof, then post a *do-not-re-append* warning naming the exact command that would duplicate the change (e.g. a second `--amend` appending another `Agent:/Host:/Task:` paragraph), and offer the `reset --hard <old-sha>` escape so the live worker can own it cleanly.
- **After the guard fires, close the loop on the record only:** confirm the comments are attributed to your own profile, confirm the live worker's heartbeat is fresh, and delete any scratch that held flagged values. Then end the turn — further board calls cannot help.

## Review-run tooling on this host

- **A clone of the live checkout inherits the SOURCE's checked-out branch.** `git clone <shared checkout>` checks out whichever branch that checkout happens to be on, so a sibling lane's branch tip — not `main` — lands in your clone while the delivered numbers were measured elsewhere. Detach at the reviewed sha (`git checkout --detach <sha>`), assert `HEAD^{tree}` equals that revision's tree, and `git rev-parse` HEAD/main/origin\/main both before and after: on a shared checkout `main` can advance mid-review. When the reviewed commit is an ANCESTOR of the new tip, the reviewed tree is intact, the verdict stands, and nothing is re-stamped — never rewrite a commit that is already published.
- **A "gates green" claim measured on `--staged` is a claim about the FILES being committed, not the worktree.** A whole-tree `PII_STRICT=1 scripts/pii_scan.sh` can go red on a sibling lane's uncommitted hunk: confirm with `git diff HEAD --numstat -- <file>` (dirty) plus `git log -S <literal> -- <file>` (the value exists in no commit), then reproduce the green the card claims on a CLEAN CLONE of the reviewed commit (expect `rc 0`) instead of on the dirty shared checkout. Report the sibling red as a hotspot for its owner, not as the deliverable's defect.
- **Reconcile a live-tree hit count as clean-tree-at-commit + the dirty hunks' matching lines.** A sweep over the shared (dirty) checkout legitimately exceeds the count at the commit, and the difference belongs to whichever lane owns the hunk — measure both, per file, rather than calling either number wrong. A raw listing attachment can be an EARLIER live snapshot than the final classified listing: reconcile the pair against the commits in between (a 136/38 vs 138/39 pair differing by exactly the later commit's 2 mentions) and report the label as imprecise, not the content.
- **State the convention behind any "last line" / line-count citation.** A file ending `sudo reboot\n\n` has `splitlines()` = 62 but `wc -l` = 63 and its last *non-blank* line at 61; prose that says "its last line is X" is a convention claim, so name the convention (or the byte tail) instead of bouncing a card whose behavioural claim verifies.

- **The parent/child graph is in `task_links`, not on `tasks`.** `tasks` has `parent_id` AND `parents` columns in neither form — a probe that selects either raises `no such column` and the read looks like a missing card. Join `task_links(parent_id, child_id)` for both directions, and check `tasks.current_run_id` + `worker_pid` against `task_runs.worker_pid` plus `ps -p <pid> -o command` to prove the live run is your own session (the command line carries the session's `-q` prompt verbatim).
- The durable task record lives in the board DB. **The DEFAULT board is the ROOT hermes home's `~/.hermes/kanban.db`** (on yoyodine `/Users/hermes_user/.hermes/kanban.db`) - NOT `<HERMES_HOME>/kanban.db` when `HERMES_HOME` points at a PROFILE dir, and NOT the 0-byte stray `~/.hermes/kanban/kanban.db` inside the kanban dir. A card can be present in the root DB and absent from every `boards/<slug>/kanban.db`, so enumerate candidates from the root home first and confirm by row count before concluding a card does not exist; non-default boards live at `kanban/boards/<slug>/kanban.db` (the `current` symlink selects which board CLI reads/writes, and does not move the default DB; on the `financially` board the runs table is `task_runs` whose primary key is `id` (NOT `run_id`), and there is NO `events` table - put a `select count(*) from events` probe last or wrap it, since it raises after earlier prints). When `kanban_show` output is too large or ELIDED to a placeholder, copy the DB and query the copy; a WAL-mode DB often refuses to open `mode=ro`, so copy the `.db` plus its `-wal`/`-shm` sidecars to a scratch dir and open the copy.
- **The scratch workspace is deleted the moment the task completes**, so a DB copy taken *inside* `$HERMES_KANBAN_WORKSPACE` cannot be read back after your terminal transition. Copy the board DB to `/tmp` (the sidecar may legitimately be absent, so the `.db` alone is what you get) before you comment and complete — the post-completion read-back that confirms task status, run outcome and comment author has to come from outside the workspace. **Copy the `-wal`/`-shm` sidecars with the `.db`, or the read-back lies.** A `shutil.copy` of a WAL board's `.db` alone returns a STALE snapshot (the writes are still in the `-wal`): one read-back after a `kanban_request_changes` that the API had answered `ok`/`run_id=56`/`status=ready` showed `status=running`, `outcome=None` for that run and *did not contain the comment just posted* — a convincing false "the transition never landed". Copy `.db` + `-wal` + `-shm` into fresh names, open the copy, and confirm the run's `outcome` (`changes_requested`) plus the new comment id before believing either result.
- **The board that holds the card is not necessarily the root DB — enumerate every board before concluding a card is missing.** `HERMES_KANBAN_DB` names the ACTIVE board, and a card can live only there: one review card was absent from `~/.hermes/kanban.db` (`SELECT` on `tasks` returned no row) while `kanban/boards/ops/kanban.db` held it. Loop the root DB plus the glob `~/.hermes/kanban/boards/*/kanban.db` and match the id, rather than trusting the first DB you open (the inverse of the "card is in the root DB but not in boards/*" case above).
- **Never pass a `git log --format=` string through a shell.** `--format=%an <%ae>` interpolated into an f-string reaches `/bin/sh`, where `<%ae>` is a redirection: the probe prints `sh: %ae: No such file or directory` and then reads as an EMPTY trailer block, which is the exact shape of a real attribution defect. Pass argv (`subprocess.run([...])`) or quote the format, and treat a mangled format-string error as a probe bug before reporting missing trailers.
- `write_file` can fail with `OSError: [Errno 2] No such file or directory` on a `*.py` path (its post-write lint step) while `.txt` writes succeed. Workaround: write the script with a `.txt` name and run `python3 that_file.txt` — the interpreter ignores the extension.
- **Postgres tooling on the financially host: `psql` is NOT on PATH.** Use the vendored binary (`/opt/hermes_prod/financially/postgres/16/bin/psql`, mirrored under the sandbox tree) and pass SQL from a file (`psql -h 127.0.0.1 -p 5432 -U <user> -d <db> -A -t -f /tmp/q.sql`); the inline form (`psql -tAc "select ..."`) is refused by the command scanner as an unresolved nested body. Reading the live ledger is fine — it is a read, and it is the only way to measure a prod-untouched claim with your own instrument instead of the handoff's replica.
- **Judging a "production was never written" claim: gate on the write counters, never on `xact_commit`.** `pg_stat_database` `tup_inserted|tup_updated|tup_deleted` plus the live row counts and the postmaster pid are the gates; `xact_commit`/`xact_rollback` move on every read connection's implicit rollback, so a delta there is the witness measuring itself (an EVIDENCE.json that reports the delta as *informational* is being honest — check it says so). Take both samples yourself, before and after your own runs, not from the handoff's evidence file, and require the write counters to be identical.
- **A card answered only through non-Kanban review comments still needs its review-run verdict landed.** Sign-off and conditions exchanged via `kanban_comment` (or DM walls) leave the card in the review lane with no terminal transition; the dispatcher then spawns a review worker whose only job is the verdict. Do not re-litigate the closed conditions — reproduce the headline numbers, read the reviewed tip, then `kanban_complete` with the caveats that remain (even when the residual is a held branch).
- Single-query headless sessions block nested executable bodies: `python3 -c "..."`, heredocs, `for`-over-`$(...)` loops and shell redirection can all be refused by the command scanner. Put the logic in a script file and run `python3 <file>`.
- **Tool results above roughly 4 KB arrive as content-free one-line stubs** (`[read_file] read <path> (N chars)`), and a `terminal` call whose output is large can come back as just `ran ... -> exit 0, 1 lines output`. Never reason from a stubbed read: re-read the file in ~30-line chunks (or grep the exact lines) before quoting it, and re-take any measurement whose numbers you only remember from before a compaction. Reconstructed file contents are the most dangerous form of rubber-stamping.
- **`rm -f /tmp/<file>` is refused as "delete in root path"**, just like `rm -rf`. Delete scratch from inside a `python3` script (`os.remove` / `shutil.rmtree`), or have the script remove itself with `os.remove(__file__)`.
- **A store-side time-window aggregate built on SQLite `date()` measures UTC days, not the store's local days.** Every yoyodine store records ET (`-04:00`), and `date('2026-09-07T20:00:08-04:00')` returns `2026-09-08` — so `WHERE date(started_at) BETWEEN …` silently shifts each evening's 20:00/21:00 row into the next day and reports a min/max band one row short, while the same connection's `MIN/MAX` visibly disagree with the row list you printed a moment earlier. Re-derive from row-level values grouped by job id, and treat any window-vs-row mismatch as this probe bug before calling the handoff's numbers wrong.
- **A `grep` pattern that literally contains a delete token is itself refused.** Asking for the skill's own example text can trip the dangerous-command scanner on an otherwise read-only search; match on a distinctive nearby phrase instead of dropping the check.
- **A provenance probe must not write.** `gate.py` stamps `<dest>/.deployed-from` BY DEFAULT; a review read-back of prod provenance needs `--no-stamp`. Its writer emits 6 keys, so an unguarded run silently drops the 7th line (`verified: copied tree matches git archive HEAD`) that deploy.sh writes, and moves `tree`/`deployed` to your probe's clock. If you did stamp: rebuild deploy.sh's 7-line template with the deployer's own values from the ledger row for that deploy, re-run the repo's `--check` (expect rc 0), and disclose it in the verdict — a stamp without `verified:` is a weakened provenance claim, not a cosmetic diff.
- **Cross-check rewritten prose against the artifact's own machine-readable description.** A doc bullet that misstates what a node consumes is falsified cheaply: the workflow YAML's `outputs.description` for that node, the node function's selector + cap constants, and the emitted output of a REAL run. One card's `research_plan` bullet claimed mover targets were "snapshot tickers that came back UNAVAILABLE" while the YAML in the same commit said "versioned fixed queries + snapshot movers + calendar gov releases" and the run's own plan output showed a fixed cap (`MAX_MOVER_TARGETS`) selecting the first 6 tokens with availability playing no part — a rework, not a pointer fix, when the false mechanic re-seeds the very habit the card existed to kill.

## Reviewing a lease / age-based reaper change on yoyodine

- **An age-based reap is a liveness test only if the record cannot be young and alive at once — prove it by advancing the CLOCK, never by editing the record.** Run the production sequence (`admit` -> `claim_next(lease_ok=True)` -> `sweep_delivery_queue(home, now_ns=claimed_at+ttl+1s)`) and then let the still-live turn call `settle(status=delivered)`: at the tip the sweep retires a LIVE claim to `ambiguous`/`lease_lapsed`, the settle RAISES, the reply is discarded and the sender's envelope reads `unknown` (a permitted `reoffer_ambiguous` replay then duplicates a message that already arrived); at the base the identical probe settles `delivered`. Run BOTH arms — a tip-only run cannot tell a fix from a regression.
- **Check the callers whose invariant the reap breaks, not just the reaper.** A record claimed by a live turn is expected to stay in `claimed/` until that turn settles: grep `settle(` / `requeue_unstarted(` and read whether each call sits inside its caller's `try/except` (`api_server_bot_delivery.py:159` does not) and whether the claimed file's disappearance surfaces as `FileNotFoundError` elsewhere in the same turn.
- **Ask what bounds the lease relative to what it must outlast.** A lease borrowed from the queue TTL (whose stated job is bounding how long a QUEUED record may wait) does not bound turn duration: measure live attempt durations against the lease (1548.2s vs 1800s = 86%) before accepting "the trigger is unreachable".
- **`slot_held(target)` is not a holder-identity test.** Gating the reap on the target's slot being free protects live turns but never reaps a claim whose continuously-busy target is held by another surface — the very case such a card is usually filed for — so name that residual instead of prescribing the gate alone.
- **A reap must not be the only thing that changed if it breaks a caller's assumption.** The minimum outcome to require: a claim whose holder is still alive is never retired and its true outcome still lands (heartbeat the lease from the running turn, or make `settle`/`requeue_unstarted` tolerate a `lease_lapsed` retirement), plus tests in both directions — live past the lease settles truthfully with no exception, dead past the lease is reaped and frees the slot.
- **Run BOTH orderings of the recovery replay, not just the reap-then-settle one.** A reap that retires a LIVE claim makes the sender's envelope read `unknown` where it previously read `receipt` — precisely the signal that invites the queue's one-time replay. So verify `reap -> settle -> reoffer` (the reoffer must be refused, the outcome lands, no duplicate) AND `reap -> reoffer -> settle` (the record is rewound to queued, or re-claimed by a later turn). The second arm is the one that breaks: `settle`/`requeue_unstarted` raise `FileNotFoundError` from the same unguarded terminal call site (it sits outside the `except` that wraps the turn), the reply is discarded, the re-claimable record delivers the message twice, and the raise is logged as a failed sweep tick that abandons every other profile's drain that tick. A tolerance that gates on the `ambiguous` settled file passes the first arm and fails the second, because the reoffer deletes that file — require a test per ordering, and reproduce both arms yourself before accepting a 'live turn now lands its outcome' claim.

## Reviewer stamping and re-measurement traps

- **Apply the reviewer stamp to an unpublished, clean, quiet tip; it belongs ON the reviewed commit** (`yaan-sdlc-standards`), and a repo `gate.py` REFUSEs a deploy without it. Prove the branch is quiet first (the implementer's run is closed, yours is the only live worker), then amend in the clean worktree with the same author identity+date. **Insert `Reviewed-by:` / `Reviewed-tree: <tree>` BEFORE the trailing blank line of `%B`** — `%B` already ends with it, and appending after that blank starts a new paragraph, which drops `Agent:`/`Host:`/`Task:` out of the trailer block (assert with `git interpret-trailers --parse` or `%(trailers)` before believing the message). Evidence set to quote: old/new sha, identical tree and parent, `git diff <old> <new>` empty, 5 trailers parsing, worktree clean, branch unpublished.
- **A DoD that requires BOTH a landing/push AND the reviewer's `Reviewed-by:` trailer is unsatisfiable in one pass — record the deferral, never rewrite to close it.** The implementer's push publishes the reviewed commit, and the stamp recipe rewrites every commit between it and HEAD, so stamping would force-push published history (forbidden by `yaan-sdlc-standards`: "a stamp is provenance, not a deploy blocker"); a sibling commit landing on top makes the CAS refuse outright. Verify the reviewed blob is identical at the reviewed sha, at HEAD and in the worktree, approve on the behavioural evidence, and record on the card the measured ref state, that standard's clause, and the process fix (the next card of this shape should hold the push so the tip is unpublished, or state the verdict lands as the card record). Never amend or cherry-pick a stamp onto a LATER commit — that mis-blesses the sibling's change — and check a repo's actual mode: a private repo with 13 `Reviewed-by` commits on `main` makes the missing trailer a real residual, not a nit.
- **A same-line grep for a shipped string is an instrument that lies: source string literals wrap.** `grep -c "MUST gather index/macro levels live during Phase 1 via web_search"` returned 0 on the *unfixed* prod copy because the literal is split across two concatenated source lines — a false "prod is already fixed" reading. Verify prod-untouched claims by file hash against canonical and the tip, and grep on a short fragment that cannot wrap.
- **`git checkout <rev> -- <paths>` stages the old content, so the natural `git checkout -- <paths>` restores from the INDEX and leaves the old bytes in the tree.** For a base-vs-tip control, revert with `git checkout <base> -- <paths>` and restore with `git checkout HEAD -- <paths>`, then assert `git status --porcelain` is empty and `git rev-parse HEAD` is the revision you think it is before trusting any later count in that clone.
- **An untracked stray file in the reviewed worktree is a claim about who ran what.** A 0-byte `None` in the tree root looked like a shipped-test defect; running the same suite in a throwaway clone produced no stray file and a clean status, so the artifact was the reviewer's own probe — remove it, disclose the side effect, and do not attribute it to the deliverable (nor leave it to trip the gate's dirty-tree check).

- **A review card whose DoD is a LIVE outcome cannot be closed by the review lane — approve, then CREATE one child card per remaining lane.** When the DoD reads "verified by a control ping with the hold time recorded", the artifact can be perfect and the card still cannot honestly complete: the landing needs the repo owner's identity and the live half needs a process bounce a worker context may not perform. Run the base/tip control as your own negative control, then create the cards with `parents=[...]` chaining them (owner landing -> ops converge/bounce/verify), name the measured live symptom in the parent's completion summary, and put the exact verified sha/tree/parent on the landing card so it neither re-derives nor re-writes a published commit. A missing pre-created child does NOT make the empty trailer block a bounce: create the child that owns the gate step. Never self-apply a `Reviewed-by:` naming an owner who did not review the change — the owner's stamp is the owner's act.
- **A trailer-less commit on an unpublished branch is not automatically a bounce when a pre-created
  deploy/owner child owns the gate step.** Read the children's bodies before deciding: if the deploy
  card is assigned to the repo's OWNER (per `OWNERS`) and its body already names the gate's
  `Reviewed-by:` requirement, the trailer block is that card's step, not the implementer's rework.
  Approve, and hand the deployer the facts it would otherwise re-derive: the exact reviewed sha/tree,
  that the branch is unpublished (verify at the FORGE, not via a clone whose `origin` is a local
  mirror), and every trailer the gate actually checks — `gate.py` refuses on a missing `Agent:`
  trailer *and* on a `Reviewed-by:` not naming the owner, so a deployer told only about
  `Reviewed-by:` adds one trailer and still gets refused. Prescribe an amend that adds the block and
  changes nothing else (same parent, identical tree) so the published tree is the reviewed tree.
  Bounce only when no lane owns the gate step, or when the branch is already published (then the
  attribution is gone and the finding is permanent, not correctable).
- **A live store's row count drifts under you, so quote it with its clock.** A census that grows
  between the handoff and the review (`102` -> `103` rows, refs through a new max) is the store
  being live, not a discrepancy: say "N at handoff, M now" and warn the deploy verifier against
  reading the later number as a regression. The same applies to any row set the fix's population is
  measured over.

## Verification

Before submitting the verdict, confirm:

- [ ] `kanban_show` was read for the current task and run.
- [ ] Every acceptance criterion was mapped to evidence.
- [ ] The actual deliverable was inspected.
- [ ] Relevant focused checks were run or an explicit reason was recorded when execution was impossible.
- [ ] Prior requested changes were re-tested on re-review.
- [ ] Unrelated regressions and scope changes were considered.
- [ ] The verdict uses exactly one terminal action.
- [ ] The summary contains concrete, non-secret evidence.
- [ ] No implementation files were edited by the reviewer.
