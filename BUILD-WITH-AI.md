# Built with a multi-agent AI workflow

This document describes the *process* used to design and build this suite —
not a testimonial, not a benchmark. If you're evaluating whether this workflow
is worth adopting for your own project, this is the part that should transfer.

## Premise

Spec before code. Every feature across this suite's four components existed
as a written design decision before a line of implementation was written
against it. Three models held three distinct roles in producing that spec
and that code, and a human directed the whole thing and was the only party
allowed to accept a stage as done.

The point of separating roles isn't ceremony — it's that a model reviewing its
own output tends to rationalize it. Splitting "write the spec," "write the
code," and "verify the code matches the spec" across different sessions (and
different models) means the verifier has no stake in the code having been
written a particular way.

## Roles

| Role | Model | Responsibility |
|---|---|---|
| Designer | Fable (Opus-class, design mode) | Writes and revises specs: architecture decisions, data schemas, edge cases, what's in scope for v1 vs. later. Produces the design document, not code. |
| Orchestrator | Opus | Reads specs, breaks them into buildable units, dispatches implementation work, and — critically — verifies the result against the spec before accepting it. Runs anything destructive (cutovers, migrations) itself rather than delegating it. |
| Executor | Sonnet | Implements one spec section at a time. Writes code, writes the tests the spec mandates, reports back. Does not decide what to build — only how, within what's written. |

Separation of powers, concretely: the executor that writes a piece of code is
never the one that judges whether it's correct. The orchestrator that accepts
a merge didn't write the code it's accepting. The designer that decided
"upload must be copy-only" doesn't get a vote on whether the implementation
actually enforces that — that's the orchestrator's job to check, and this
document's job to show one example of.

## Spec-driven flow, traced

Here's one real decision, from spec line to code, to show what "spec-driven"
means in practice rather than in the abstract.

**Spec excerpt** (drive-upload design, "Decision 2"):

> Mirror-image fork of drive-downloader; copy semantics only (never move — a
> mis-click must not destroy local data).

**What that constrains, concretely:**

- The rclone operation on the wire has to be `sync/copy`, never `sync/move` or
  `sync/sync` — those two can delete source files under the wrong flags.
- Nothing in the upload path may call `rmtree`/`unlink` on a user's *source*
  selection, ever, even as a "clean up after success" step.
- The async job kickoff (`RcloneRC.upload_async`) is named and typed for copy
  specifically, so a future contributor extending it can't accidentally widen
  it to move by changing a string constant somewhere else.

That's the trace: one sentence in a design doc constrains a wire-level API
choice, a rule about what the codebase is never allowed to call, and a naming
convention meant to make the constraint hard to violate by accident later.
Nothing in this codebase's upload path issues a move or delete against a
user-selected source — that invariant is the spec's, not an implementation
afterthought.

## Verification-first

Before any upload code shipped, the plan it produces was validated by running
the real transfer engine in `rclone --dry-run` — confirming **zero writes**
against the destination before ever running it for real. That's checking the
plan does what the spec says *before* trusting it with a live account, not
trusting the tests alone.

Specs themselves were verified against the live account before implementation
started, not discovered as build failures partway through:

- The original design assumed a Shared Drive remote could be addressed
  generically. Testing against the real account surfaced that the configured
  remote had a specific team drive **baked into its rclone config** — a bare
  reference to the remote only ever resolves to that one drive. That fact
  reshaped how every other drive gets addressed (a connection-string override
  per request) *before* the addressing code was written, not as a patch after
  it broke.
- Firing copy jobs for multiple drives at once looked fine in isolation but
  tripped Google's rate limiting under real load. The fix — one job per drive,
  fired sequentially rather than concurrently — is baked into the job-dispatch
  design, not bolted on as a retry-after-failure hack.

Both of these became architecture decisions in the spec, verified against a
live account, before the corresponding code was written — not bugs found
after the fact.

## Testable seams, mandated by spec

The specs didn't just describe behavior — they specified *where the pure logic
had to live* so it could be unit-tested without touching a network or a
filesystem:

- `selection.py` (toolkit download) — turns a sparse tree of checkbox toggles
  into an ordered list of rclone filter rules. Pure function in, data out. No
  rclone daemon, no HTTP.
- `plan.py` (toolkit upload) — turns a set of local file/folder picks into a
  list of copy jobs, handling dedup, destination-collision detection, and
  glob-escape. Pure logic, no `osascript`, no rclone.
- `hub_core.py` (toolkit hub) — install/status/launch logic for every
  registered tool. Every probe (TCP connect, `launchctl print`, path
  existence) is injected, so the full state machine is tested with zero real
  ports or processes.
- `renamer.py` / `staged_complete_path` (drive-offload) — canonical naming and
  "is this file actually done" classification, both pure functions over names
  and marker siblings, not size or timing.

Requiring these seams up front — rather than discovering after the fact that
the logic worth testing is tangled up with a Flask route or an `osascript`
call — is itself a spec decision, and it's why this suite's test suites run
in milliseconds with no live account required.

## What the workflow missed — a field report

The sections above describe the process working. This one is the counterweight,
from a single session (2026-08-01) that shipped two features across the server
and the Fire TV client. It ran the roles above exactly — Fable planning, Opus
orchestrating and verifying, Sonnet executing — and the failures below are the
transferable part, more than the successes are.

**Green tests and a clean build are not evidence the feature works.** The
sort/group work landed with 20 new unit tests passing and `assembleRelease`
clean. A D-pad navigation bug on the same screen was invisible to every one of
them, because the thing that was broken — which node takes focus after a
keypress — has no seam a JVM test can reach. When an adversarial reviewer was
finally pointed at it, its verdict on the primary scenario was *"unclear"*:
static reasoning over Compose's focus internals could not settle it either.

What settled it was six keypresses against the real device:

```
adb shell input keyevent DPAD_DOWN   # then: uiautomator dump, read focused node
```

Driving the physical stick and dumping the focused node after every press turns
"does focus go where it should" from an opinion into a table you can diff. That
loop found the bug, proved the fix, and cost minutes. **If a behavior has no
testable seam, build the crude external harness instead of arguing about it.**

**Attribute before you fix.** The nav bug surfaced immediately after shipping a
feature that rewrote the very row it involved, so the obvious reading was "we
broke it." Instead: check out the commit *before* the feature into a worktree,
build it, install it, and run the byte-identical keypress walk. Same failure,
press for press — the defect predated the feature by weeks. That cost about
four minutes and prevented "fixing" a non-regression by reverting good work.

**A test that cannot fail is worse than no test**, because it reads as
coverage. A regression test written for this session's scoped-refresh fix
asserted the right value while being structurally incapable of failing: the two
sets it compared overlapped, so the bug it guarded could be reintroduced in one
direction with the suite still green. It was caught only because a reviewer was
told to *mutate the production code and prove the test fails*. That instruction
is now the standard for any test guarding a specific bug — write it, break the
fix, watch it go red, restore.

**Never let a model document a claim it has not executed.** A comment was added
asserting that the order of the selected-drive list "isn't meaningful
downstream." It was false. A probe against the real function showed that
reordering two drives that share a show flips the merged record's owning drive,
year, and poster source. A confident comment is worse than no comment; it
survives review and gets trusted. The finding became [D-013](docs/DECISIONS.md).

**Check the environment before you diagnose the code.** Twice in one session
the system lied convincingly:

- "The fix doesn't work — it still scans every drive." The running server had
  been up for five days and held the pre-fix modules in memory. Nothing on disk
  mattered until it was restarted.
- Five consecutive D-pad presses moved nothing, matching a dead-press failure a
  reviewer had predicted in that exact code. The device was asleep
  (`mWakefulness=Asleep`).

Both would have produced a confident, wrong fix to real code. Cheap habit:
before believing a diagnosis, confirm the thing under test is actually running
the code under test.

**Fix rounds regress, so each one needs the same adversarial pass as the
original.** This session took four rounds. Round two's fix — folding a counter
into a `SaveableStateProvider` key — compiled, passed its tests, and introduced
two new defects that only a fresh review caught, including one that cleared
focus off the control the user had just pressed. It became
[D-014](docs/DECISIONS.md). A fix is not smaller than a feature just because
its diff is.

**On-device probing mutates real state.** Driving a live device with synthetic
input is the best verification available here, and it is not free: a stray
`DPAD_CENTER` during a focus probe started playback and overwrote a real resume
position (recovered afterwards via the progress API). Treat someone's running
device like production, because it is — snapshot what you are about to disturb.

## Case studies

Two components have their own full case-study writeups — how they were
designed, built, hardened, and shipped through this process:

- [docs/case-studies/drivecast.md](docs/case-studies/drivecast.md) — the streaming server
- [docs/case-studies/drive-offload.md](docs/case-studies/drive-offload.md) — the ingest daemon

## Takeaway

What the human actually did in this loop: assigned roles, wrote acceptance
criteria into the specs up front, and refused to accept a merge whose code
couldn't be traced back to a spec decision. That's the transferable part —
not the specific models, not how long anything took.

The other half of the transferable part is the field report above: role
separation catches a great deal, and it still cannot tell you whether the
thing works. Only running it can. Where a behavior has no seam a test can
reach — focus, scroll, anything a person perceives — build the external
harness and let the artifact answer.
