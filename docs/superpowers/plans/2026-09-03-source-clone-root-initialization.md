# Source Clone Root Initialization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deepen Source clone root initialization without changing `ws init` behavior.

**Architecture:** `workspace.init_workspace` remains the caller-facing interface: it resolves the Project root, loads configuration, and delegates to a new internal source-initialization module. That module owns source-path validation, marker ownership, staging, publication, rollback, and cleanup, with narrow private seams for clone fault injection.

**Tech Stack:** Python 3.12, pytest, Git subprocesses.

**Spec:** `/tmp/architecture-review-20260903-120114.html` (candidate 1 and the agreed grilling decisions).

## Global Constraints

- Preserve every current error, ordering, marker rule, cleanup rule, and `BaseException` behavior.
- Keep `init_workspace` as the callers' only interface.
- Do not share this protocol with Feature workspace creation or add a general adapter interface.
- Preserve behavioral integration tests; move their clone fault seam to the new module.

---

### Task 1: Establish the Source clone root module seam

**Files:**
- Create: `src/ws_tool/source_initialization.py`
- Modify: `tests/integration/test_init.py:1-133`

**Interfaces:**
- Consumes: `Path` and `Iterable[RepoConfig]`.
- Produces: `initialize_source_clone_root(root: Path, repos: Iterable[RepoConfig]) -> Path`.

- [ ] **Step 1: Write the failing test**

```python
import importlib.util


def test_source_initialization_module_is_available() -> None:
    assert importlib.util.find_spec("ws_tool.source_initialization") is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_init.py::test_source_initialization_module_is_available -v`

Expected: FAIL because the module is absent.

- [ ] **Step 3: Write minimal implementation**

Create `source_initialization.py` with `initialize_source_clone_root`. Move the current source-path validation and transaction from `workspace.init_workspace` unchanged: create `repos/`, own `.ws-init.lock`, clone into a temporary directory, publish clones, reverse cleanup on failure, and remove the temporary directory in `finally`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_init.py::test_source_initialization_module_is_available -v`

Expected: PASS.

### Task 2: Preserve the external interface and behavioral fault test

**Files:**
- Modify: `src/ws_tool/workspace.py:1-145`
- Modify: `tests/integration/test_init.py:91-133`

**Interfaces:**
- Consumes: `initialize_source_clone_root(root, project.repos.values())`.
- Produces: unchanged `init_workspace(*, cwd: Path | None = None) -> Path`.

- [ ] **Step 1: Write the failing test**

Change the paused-clone integration test to patch the moved internal seam:

```python
import ws_tool.source_initialization as source_initialization_module

original_clone = source_initialization_module.clone_repository
monkeypatch.setattr(source_initialization_module, "clone_repository", paused_clone)
```

The existing assertion remains: a Feature workspace cannot observe a partially published Source clone root.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_init.py::test_create_cannot_observe_partially_cloned_sources_during_init -v`

Expected: FAIL because `init_workspace` still invokes the old implementation.

- [ ] **Step 3: Write minimal implementation**

Replace `init_workspace` with:

```python
def init_workspace(*, cwd: Path | None = None) -> Path:
    root = (cwd or Path.cwd()).resolve()
    project = load_config(root / "ws.toml")
    return initialize_source_clone_root(root, project.repos.values())
```

Remove imports made unused by moving the implementation and import `initialize_source_clone_root`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_init.py -v`

Expected: PASS, including rollback, marker visibility, credential redaction, and symlink safeguards.

### Task 3: Verify and commit the refactor

**Files:**
- Verify: `src/ws_tool/source_initialization.py`
- Verify: `src/ws_tool/workspace.py`
- Verify: `tests/integration/test_init.py`

**Interfaces:**
- Consumes: unchanged `ws init` behavior.
- Produces: evidence that the refactor preserves Source clone root initialization.

- [ ] **Step 1: Run focused behavioral tests**

Run: `uv run pytest tests/integration/test_init.py -v`

Expected: PASS.

- [ ] **Step 2: Run static checks**

Run: `uv run ruff check src tests && uv run mypy`

Expected: PASS.

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest`

Expected: PASS.

- [ ] **Step 4: Inspect and commit**

Run:

```bash
git diff --check
git status --short
git add src/ws_tool/source_initialization.py src/ws_tool/workspace.py tests/integration/test_init.py docs/superpowers/plans/2026-09-03-source-clone-root-initialization.md
git commit -m "refactor: deepen source initialization"
```

Expected: no whitespace errors; one commit containing only the agreed refactor, behavioral test change, and implementation plan.
