# Built with a multi-agent AI workflow

This document describes the *process* used to design and build this toolkit —
not a testimonial, not a benchmark. If you're evaluating whether this workflow
is worth adopting for your own project, this is the part that should transfer.

## Premise

Spec before code. Every feature in this repo existed as a written design
decision before a line of implementation was written against it. Three models
held three distinct roles in producing that spec and that code, and a human
directed the whole thing and was the only party allowed to accept a stage as
done.

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
  selection, ever, even as a "clean up after success" step. (The one place the
  toolkit *does* delete anything is a scratch staging directory it created
  itself for the multipart drag-drop fallback — not the user's original
  files — and that path shipped as a documented TODO, not live code, precisely
  because it's the one place this constraint gets subtle.)
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

- `selection.py` (download) — turns a sparse tree of checkbox toggles into an
  ordered list of rclone filter rules. Pure function in, data out. No rclone
  daemon, no HTTP.
- `plan.py` (upload) — turns a set of local file/folder picks into a list of
  copy jobs, handling dedup, destination-collision detection, and glob-escape.
  Pure logic, no `osascript`, no rclone.
- `hub_core.py` — install/status/launch logic for every registered tool. Every
  probe (TCP connect, `launchctl print`, path existence) is injected, so the
  full state machine is tested with zero real ports or processes.

Requiring these seams up front — rather than discovering after the fact that
the logic worth testing is tangled up with a Flask route or an `osascript`
call — is itself a spec decision, and it's why this repo's test suite runs in
milliseconds with no live account required.

## Takeaway

What the human actually did in this loop: assigned roles, wrote acceptance
criteria into the specs up front, and refused to accept a merge whose code
couldn't be traced back to a spec decision. That's the transferable part —
not the specific models, not how long anything took.
