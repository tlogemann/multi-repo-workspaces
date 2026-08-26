from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ws_tool.git import worktree_admin_path
from ws_tool.models import ContextState, RemovalRepoState, RemovalState, RepoState, WorkspaceState
from ws_tool.serialization import write_workspace_state


def run_ws(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from ws_tool.cli import main; raise SystemExit(main())",
            *args,
        ],
        cwd=cwd,
        text=True,
        capture_output=True,
        env=os.environ.copy(),
    )


def write_config(path: Path, workspace_root: str, repos: str) -> Path:
    path.write_text(
        f"[project]\nworkspace_root = {json.dumps(workspace_root)}\n\n{repos}",
        encoding="utf-8",
    )
    return path


def repo_table(name: str, source: Path, default_ref: str | None = None) -> str:
    default = "" if default_ref is None else f'\ndefault_ref = "{default_ref}"'
    return f'[repos."{name}"]\npath = {json.dumps(str(source))}{default}\n'


def git_output(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check)
    return result.stdout.strip()


def create_config(tmp_path: Path, source: Path, *, default_ref: str | None = None) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    return write_config(
        config_dir / "ws.toml",
        "../workspaces",
        repo_table("app", source, default_ref),
    )


def create_workspace(tmp_path: Path, source: Path, name: str, config: Path) -> Path:
    result = run_ws(config.parent, "create", name, "--config", str(config))
    assert result.returncode == 0, result.stderr
    return tmp_path / "workspaces" / name


def test_claim_uses_locked_base_but_source_default_uses_current_default(
    tmp_path: Path, git_repo
) -> None:
    source = git_repo("claim-defaults")
    initial = source.run("rev-parse", "HEAD").stdout.strip()
    config = create_config(tmp_path, source.path, default_ref="main")
    locked = create_workspace(tmp_path, source.path, "locked", config)
    current = create_workspace(tmp_path, source.path, "current", config)
    latest = source.commit("advanced", content="advanced\n")

    locked_result = run_ws(locked / "repos" / "app", "claim", "app")
    current_result = run_ws(
        current / "repos" / "app",
        "claim",
        "app",
        "--source",
        "default",
        "--target",
        "feature/current-default",
    )

    assert locked_result.returncode == 0, locked_result.stderr
    assert current_result.returncode == 0, current_result.stderr
    assert git_output(locked / "repos" / "app", "rev-parse", "HEAD") == initial
    assert git_output(current / "repos" / "app", "rev-parse", "HEAD") == latest
    assert (
        git_output(locked / "repos" / "app", "symbolic-ref", "--short", "HEAD") == "ws/locked/app"
    )
    assert (
        git_output(current / "repos" / "app", "symbolic-ref", "--short", "HEAD")
        == "feature/current-default"
    )


def test_claim_explicit_source_and_target(tmp_path: Path, git_repo) -> None:
    source = git_repo("claim-explicit")
    source.branch("feature/base")
    base = source.run("rev-parse", "feature/base").stdout.strip()
    source.commit("main advance", content="main advance\n")
    config = create_config(tmp_path, source.path)
    workspace = create_workspace(tmp_path, source.path, "explicit", config)

    result = run_ws(
        workspace / "repos" / "app",
        "claim",
        "app",
        "--source",
        "feature/base",
        "--target",
        "feature/claimed",
    )

    assert result.returncode == 0, result.stderr
    assert git_output(workspace / "repos" / "app", "rev-parse", "HEAD") == base
    assert (
        git_output(workspace / "repos" / "app", "symbolic-ref", "--short", "HEAD")
        == "feature/claimed"
    )


def test_claim_preserves_dirty_detached_worktree_changes(tmp_path: Path, git_repo) -> None:
    source = git_repo("claim-dirty")
    config = create_config(tmp_path, source.path)
    workspace = create_workspace(tmp_path, source.path, "dirty", config)
    worktree = workspace / "repos" / "app"
    (worktree / "README.md").write_text("staged change\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=worktree, check=True)
    (worktree / "untracked.txt").write_text("untracked\n", encoding="utf-8")

    result = run_ws(worktree, "claim", "app")

    assert result.returncode == 0, result.stderr
    assert (worktree / "README.md").read_text(encoding="utf-8") == "staged change\n"
    assert (worktree / "untracked.txt").read_text(encoding="utf-8") == "untracked\n"
    assert "README.md" in git_output(worktree, "diff", "--cached", "--name-only")
    assert "?? untracked.txt" in git_output(worktree, "status", "--short")


def test_claim_exact_idempotency_does_not_resolve_stale_source(tmp_path: Path, git_repo) -> None:
    source = git_repo("claim-idempotent")
    config = create_config(tmp_path, source.path)
    workspace = create_workspace(tmp_path, source.path, "idempotent", config)
    worktree = workspace / "repos" / "app"
    first = run_ws(worktree, "claim", "app")

    second = run_ws(
        worktree,
        "claim",
        "app",
        "--source",
        "does-not-exist",
        "--target",
        "ws/idempotent/app",
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert git_output(worktree, "symbolic-ref", "--short", "HEAD") == "ws/idempotent/app"


def test_claim_rejects_existing_target_branch_without_mutating_detached_worktree(
    tmp_path: Path, git_repo
) -> None:
    source = git_repo("claim-collision")
    source.branch("feature/existing")
    config = create_config(tmp_path, source.path)
    workspace = create_workspace(tmp_path, source.path, "collision", config)
    worktree = workspace / "repos" / "app"
    before = git_output(worktree, "rev-parse", "HEAD")

    result = run_ws(worktree, "claim", "app", "--target", "feature/existing")

    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert git_output(worktree, "rev-parse", "HEAD") == before
    assert git_output(worktree, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) == ""


def test_claim_rejects_target_checked_out_by_another_workspace_worktree(
    tmp_path: Path, git_repo
) -> None:
    source = git_repo("claim-worktree-collision")
    config = create_config(tmp_path, source.path)
    first_workspace = create_workspace(tmp_path, source.path, "first", config)
    second_workspace = create_workspace(tmp_path, source.path, "second", config)
    first_worktree = first_workspace / "repos" / "app"
    second_worktree = second_workspace / "repos" / "app"
    first_result = run_ws(first_worktree, "claim", "app", "--target", "feature/shared")
    before = git_output(second_worktree, "rev-parse", "HEAD")

    result = run_ws(second_worktree, "claim", "app", "--target", "feature/shared")

    assert first_result.returncode == 0, first_result.stderr
    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert git_output(first_worktree, "symbolic-ref", "--short", "HEAD") == "feature/shared"
    assert git_output(second_worktree, "rev-parse", "HEAD") == before
    assert (
        git_output(second_worktree, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) == ""
    )


def test_claim_rejects_already_claimed_repository_on_different_branch(
    tmp_path: Path, git_repo
) -> None:
    source = git_repo("claim-different-branch")
    config = create_config(tmp_path, source.path)
    workspace = create_workspace(tmp_path, source.path, "different", config)
    worktree = workspace / "repos" / "app"
    first = run_ws(worktree, "claim", "app", "--target", "feature/first")
    before = git_output(worktree, "rev-parse", "HEAD")

    result = run_ws(worktree, "claim", "app", "--target", "feature/second")

    assert first.returncode == 0, first.stderr
    assert result.returncode != 0
    assert "already claimed" in result.stderr
    assert git_output(worktree, "symbolic-ref", "--short", "HEAD") == "feature/first"
    assert git_output(worktree, "rev-parse", "HEAD") == before


def test_claim_default_uses_locked_selector_after_config_and_tag_source_are_removed(
    tmp_path: Path, git_repo
) -> None:
    source = git_repo("claim-locked-selector")
    source.run("tag", "v1")
    config = create_config(tmp_path, source.path, default_ref="main")
    workspace = tmp_path / "workspaces" / "tagged"
    created = run_ws(
        config.parent,
        "create",
        "tagged",
        "--config",
        str(config),
        "--source",
        "app=v1",
    )
    assert created.returncode == 0, created.stderr
    initial = git_output(workspace / "repos" / "app", "rev-parse", "HEAD")
    config.unlink()
    latest = source.commit("default advanced", content="default advanced\n")

    result = run_ws(
        workspace / "repos" / "app",
        "claim",
        "app",
        "--source",
        "default",
        "--target",
        "feature/locked-default",
    )

    assert result.returncode == 0, result.stderr
    assert latest != initial
    assert git_output(workspace / "repos" / "app", "rev-parse", "HEAD") == latest
    assert (
        git_output(workspace / "repos" / "app", "symbolic-ref", "--short", "HEAD")
        == "feature/locked-default"
    )


def test_claim_default_uses_explicit_locked_selector_for_bare_source_after_config_removal(
    tmp_path: Path, git_repo, bare_git_repo
) -> None:
    seed = git_repo("claim-bare-default-seed")
    bare = bare_git_repo("claim-bare-default.git")
    seed.run("remote", "add", "origin", str(bare))
    seed.run("push", "origin", "main")
    seed.run("--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main")
    config = create_config(tmp_path, bare, default_ref="main")
    workspace = create_workspace(tmp_path, bare, "bare-default", config)
    initial = git_output(workspace / "repos" / "app", "rev-parse", "HEAD")
    config.unlink()
    latest = seed.commit("bare default advanced", content="bare default advanced\n")
    seed.run("push", "origin", "main")

    result = run_ws(
        workspace / "repos" / "app",
        "claim",
        "app",
        "--source",
        "default",
        "--target",
        "feature/bare-default",
    )

    assert result.returncode == 0, result.stderr
    assert latest != initial
    assert git_output(workspace / "repos" / "app", "rev-parse", "HEAD") == latest
    assert (
        git_output(workspace / "repos" / "app", "symbolic-ref", "--short", "HEAD")
        == "feature/bare-default"
    )


def test_claim_rejects_null_locked_default_selector(
    tmp_path: Path, git_repo, bare_git_repo
) -> None:
    seed = git_repo("claim-bare-seed")
    bare = bare_git_repo("claim-bare.git")
    seed.run("remote", "add", "origin", str(bare))
    seed.run("push", "origin", "main")
    seed.run("--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main")
    config = create_config(tmp_path, bare)
    workspace = tmp_path / "workspaces" / "bare"
    config_result = run_ws(
        config.parent,
        "create",
        "bare",
        "--config",
        str(config),
        "--source",
        "app=main",
    )
    assert config_result.returncode == 0, config_result.stderr

    result = run_ws(workspace / "repos" / "app", "claim", "app", "--source", "default")

    assert result.returncode != 0
    assert "no locked default selector" in result.stderr
    assert (
        git_output(
            workspace / "repos" / "app", "symbolic-ref", "--quiet", "--short", "HEAD", check=False
        )
        == ""
    )


def test_claim_refuses_context_and_removal_states(tmp_path: Path, git_repo) -> None:
    source = git_repo("claim-state-guards")
    commit = source.run("rev-parse", "HEAD").stdout.strip()
    config = create_config(tmp_path, source.path)
    context_workspace = create_workspace(tmp_path, source.path, "context", config)
    context_state = WorkspaceState(
        workspace_name="context",
        phase="idle",
        repos={
            "app": RepoState(
                name="app",
                mode="context",
                head=commit,
                detached=True,
                context=ContextState(
                    target_ref="main",
                    target_commit=commit,
                    phase="active",
                    return_mode="detached",
                    stash_token="claim-guard",
                    return_saved_head=commit,
                ),
            )
        },
    )
    write_workspace_state(context_workspace / ".ws" / "state.toml", context_state)

    context_result = run_ws(context_workspace / "repos" / "app", "claim", "app")

    removal_workspace = create_workspace(tmp_path, source.path, "removing", config)
    removal_worktree = removal_workspace / "repos" / "app"
    removal_state = WorkspaceState(
        workspace_name="removing",
        phase="removing",
        repos={"app": RepoState(name="app", mode="detached", head=commit, detached=True)},
        removal=RemovalState(
            phase="removing",
            workspace_path=removal_workspace,
            tombstone_path=removal_workspace.parent / ".removing.removing",
            repos={
                "app": RemovalRepoState(
                    name="app",
                    worktree_path=removal_worktree,
                    git_admin_path=worktree_admin_path(removal_worktree),
                    complete=False,
                )
            },
        ),
    )
    write_workspace_state(removal_workspace / ".ws" / "state.toml", removal_state)

    removal_result = run_ws(removal_worktree, "claim", "app")

    assert context_result.returncode != 0
    assert "context" in context_result.stderr
    assert removal_result.returncode != 0
    assert "removal" in removal_result.stderr
