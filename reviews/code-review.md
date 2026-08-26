# Code Review: `HEAD~6...HEAD`

## Scope and evidence

- **Reviewed range:** `git diff HEAD~6...HEAD`
- **Commits:** `bd33767` through `8008e1d` (six commits)
- **Spec:** [`doc/workspace-plan.md`](../doc/workspace-plan.md)
- **Standards sources:** none found. `AGENTS.md` only links engineering-skill configuration and does not define coding standards.
- **Review evidence:** the prior review reported 141 tests passing, plus Ruff and mypy passing. Those commands were not re-run when creating this artifact.

## Confirmed issues: spec compliance

Each item should be technically verified against the code and tests before changing it.

### P1 — Tombstone recovery is not durable across a real crash

- **Location:** `src/ws_tool/workspace.py:702-735`
- **Finding:** `shutil.rmtree()` combined with exception-time reconstruction cannot recover if SIGKILL or power loss occurs during deletion; the tombstone metadata can be partly deleted and unreadable.
- **Spec requirement:** “A crash after rename resumes only from that exact metadata-only tombstone.” (`doc/workspace-plan.md:723-727`)

### P1 — Context state is workspace-global, not per repository

- **Locations:** `src/ws_tool/workspace.py:267-274`, `1157-1173`
- **Finding:** global `state.phase` blocks claims across all repositories; restoring one context sets the whole workspace idle, which can misrepresent another repository’s active context.
- **Spec requirement:** “For this initial implementation, support only one temporary context level **per repository**.” (`doc/workspace-plan.md:950-954`)

### P1 — Remote default-branch disagreement is under-detected

- **Location:** `src/ws_tool/workspace.py:1627-1634`
- **Finding:** `_remote_symbolic_selector()` compares only branch-name suffixes. `origin/main` and `upstream/main` can therefore be treated as agreeing even when they resolve to different commits, and one is selected arbitrarily.
- **Spec requirement:** use “a unique remote symbolic HEAD” and “fail if remotes disagree.” (`doc/workspace-plan.md:179-185`)

### P1 — Required concurrency and crash integration coverage is missing

- **Locations:** integration-test suite; no real concurrent-process test was found. The tombstone test uses a catchable `KeyboardInterrupt`, not a process crash.
- **Finding:** required real-concurrency and crash-durability coverage is incomplete.
- **Spec requirements:** “Run real concurrent subprocesses for the same workspace” (`doc/workspace-plan.md:1813-1823`); test a “crash during partial tombstone metadata deletion” (`doc/workspace-plan.md:1884-1903`).

### P2 — Human-readable status omits required fields

- **Location:** `src/ws_tool/workspace.py:1530-1545` (`render_status_human()`)
- **Finding:** the human status omits the locked default-branch selector, context transition phase, and durable removal phase/progress. JSON includes them.
- **Spec requirement:** status must show at least the “locked default-branch selector,” “context transition phase,” and “durable removal phase/progress.” (`doc/workspace-plan.md:1013-1028`)

### P2 — Lifecycle locking exceeds the specified scope

- **Locations:** `src/ws_tool/workspace.py:247-263`, `1264-1274`
- **Finding:** claim, context, and finalize operations acquire the external lifecycle lock. This broadens blocking behavior beyond the lifecycle operations specified for that lock.
- **Spec requirements:** “Create and remove share this external… lifecycle lock.” (`doc/workspace-plan.md:1180-1194`); “Mutators are serialized by `.ws/operation.lock`.” (`doc/workspace-plan.md:2381-2384`)

### P3 — Required documentation is absent

- **Location:** repository root; no `README.md` exists.
- **Finding:** the requested mental-model documentation and examples are not present.
- **Spec requirement:** “Document the mental model thoroughly but concisely. Include examples.” (`doc/workspace-plan.md:1989-1993`)

## Judgement calls and design concerns

These are Fowler-smell heuristics, not documented-standard violations. The repository has no documented coding standards that override them.

### P2 — Divergent Change: `workspace.py` has too many responsibilities

- **Location:** `src/ws_tool/workspace.py:62-1766`
- **Concern:** one 1,766-line module owns creation, claiming, removal recovery, temporary contexts, status, locking, and rollback—independent reasons to change.
- **Suggested direction:** split by workflow (creation, claim, context, removal, status) and retain shared metadata/locking utilities.

### P3 — Duplicated creation rollback/recovery handling

- **Location:** `src/ws_tool/workspace.py:149-226`
- **Concern:** the `except KeyboardInterrupt` and `except Exception` paths repeat `_rollback_creation(...)`, lifecycle-lock retention, cleanup exception handling, and recovery messaging.
- **Suggested direction:** extract shared cleanup/recovery handling while preserving distinct exception propagation semantics.

### P3 — Primitive Obsession / Repeated Switches for domain states

- **Locations:** `src/ws_tool/models.py:47-76`; `src/ws_tool/workspace.py:796-805`, `987-1000`; `src/ws_tool/serialization.py:427-480`
- **Concern:** `phase`, `mode`, `operation`, and `step` are unrestricted strings, with recurring phase/mode cascades across mutation and validation.
- **Suggested direction:** introduce small enums or constrained domain types and centralize valid transitions.

### P3 — Middle Man in context effect helpers

- **Location:** `src/ws_tool/workspace.py:1330-1367`
- **Concern:** `_context_effect_intent` and `_context_effect_outcome` merely delegate to `_persist_context_effect`; callers already pass `"intent"` or `"outcome"` explicitly.
- **Suggested direction:** call the underlying function directly, or have wrappers supply the step themselves.

### P3 — Duplicated integration-test helpers

- **Locations:** `tests/integration/test_claim.py:14`, `tests/integration/test_context.py:17`, `tests/integration/test_phase2.py:20`, `tests/integration/test_remove.py:16`
- **Concern:** near-identical `run_ws(...)` and Git helpers recur across the integration test files.
- **Suggested direction:** consolidate shared helpers in `tests/conftest.py` or a test-support module.

## Standards conclusion

No confirmed documented-standard violations were found because the repository does not document code-writing standards. The five items above are design concerns only and should not be treated as mandatory refactors without verification.
