from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import ws_tool.workspace as workspace_module
from ws_tool.errors import GitCommandError, WsError
from ws_tool.serialization import deserialize_workspace_state, serialize_workspace_state


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


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check)


def git_output(cwd: Path, *args: str, check: bool = True) -> str:
    return git(cwd, *args, check=check).stdout.strip()


def write_config(path: Path, source: Path, *, default_branch: str | None = None) -> Path:
    configured = "" if default_branch is None else f'\ndefault_branch = "{default_branch}"'
    path.write_text(
        '[project]\nworkspace_root = "../workspaces"\n\n'
        f'[repos."app"]\npath = {json.dumps(str(source))}{configured}\n',
        encoding="utf-8",
    )
    return path


def create_workspace(
    tmp_path: Path, source: Path, name: str, *, default_branch: str | None = None
) -> Path:
    config_dir = tmp_path / f"config-{name}"
    config_dir.mkdir()
    config = write_config(config_dir / "ws.toml", source, default_branch=default_branch)
    result = run_ws(config_dir, "create", name, "--config", str(config))
    assert result.returncode == 0, result.stderr
    return tmp_path / "workspaces" / name


def context_state(workspace: Path):
    return (
        deserialize_workspace_state((workspace / ".ws" / "state.toml").read_text(encoding="utf-8"))
        .repos["app"]
        .context
    )


def effect_pair(context, operation: str):
    effects = [effect for effect in context.completed_effects if effect.operation == operation]
    assert len(effects) >= 2
    assert effects[-2].step == "intent"
    assert effects[-1].step == "failure"
    return effects[-2], effects[-1]


def prepare_dirty_context(tmp_path: Path, git_repo, name: str):
    source = git_repo(name)
    workspace = create_workspace(tmp_path, source.path, name)
    worktree = workspace / "repos" / "app"
    assert run_ws(worktree, "claim", "app", "--target", f"feature/{name}").returncode == 0
    (worktree / "README.md").write_text("saved content\n", encoding="utf-8")
    git(worktree, "add", "README.md")
    (worktree / "saved-untracked.txt").write_text("saved untracked\n", encoding="utf-8")
    source.commit("context target", content="context target\n")
    return source, workspace, worktree


def test_clean_claimed_context_uses_locked_default_and_restores_branch(
    tmp_path: Path, git_repo
) -> None:
    source = git_repo("context-clean-claimed")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = write_config(config_dir / "ws.toml", source.path, default_branch="main")
    result = run_ws(config_dir, "create", "clean", "--config", str(config))
    assert result.returncode == 0, result.stderr
    workspace = tmp_path / "workspaces" / "clean"
    worktree = workspace / "repos" / "app"
    assert run_ws(worktree, "claim", "app", "--target", "feature/foo").returncode == 0
    saved_head = git_output(worktree, "rev-parse", "HEAD")
    target_head = source.commit("default update", content="default update\n")

    entered = run_ws(worktree, "context", "app", "default")

    assert entered.returncode == 0, entered.stderr
    assert git_output(worktree, "rev-parse", "HEAD") == target_head
    assert git_output(worktree, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) == ""
    state = deserialize_workspace_state((workspace / ".ws" / "state.toml").read_text())
    context = state.repos["app"].context
    assert context is not None
    assert state.phase == "active"
    assert state.repos["app"].mode == "context"
    assert context.return_branch == "feature/foo"
    assert context.return_saved_head == saved_head
    assert context.target_commit == target_head
    assert context.phase == "active"

    restored = run_ws(worktree, "context", "app", "--restore")

    assert restored.returncode == 0, restored.stderr
    assert git_output(worktree, "symbolic-ref", "--short", "HEAD") == "feature/foo"
    assert git_output(worktree, "rev-parse", "HEAD") == saved_head
    restored_state = deserialize_workspace_state((workspace / ".ws" / "state.toml").read_text())
    assert restored_state.phase == "idle"
    assert restored_state.repos["app"].context is None


def test_dirty_claimed_context_preserves_changes_and_unrelated_newer_stash(
    tmp_path: Path, git_repo
) -> None:
    source = git_repo("context-dirty-claimed")
    workspace = create_workspace(tmp_path, source.path, "dirty", default_branch="main")
    worktree = workspace / "repos" / "app"
    assert run_ws(worktree, "claim", "app", "--target", "feature/dirty").returncode == 0
    (worktree / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(worktree, "add", "tracked.txt")
    git(worktree, "commit", "-m", "add tracked file")
    (worktree / "README.md").write_text("original dirty\n", encoding="utf-8")
    git(worktree, "add", "README.md")
    (worktree / "tracked.txt").write_text("unstaged change\n", encoding="utf-8")
    (worktree / "untracked.txt").write_text("keep me\n", encoding="utf-8")
    source.commit("new default", content="new default\n")

    entered = run_ws(worktree, "context", "app", "default")

    assert entered.returncode == 0, entered.stderr
    state = deserialize_workspace_state((workspace / ".ws" / "state.toml").read_text())
    context = state.repos["app"].context
    assert context is not None
    assert context.stash_oid is not None
    assert context.private_ref is not None
    tool_oid = context.stash_oid
    private_ref = context.private_ref
    assert git_output(worktree, "status", "--short") == ""
    (worktree / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
    git(worktree, "add", "unrelated.txt")
    git(worktree, "stash", "push", "--include-untracked", "-m", "unrelated-user-stash")

    restored = run_ws(worktree, "context", "app", "--restore")

    assert restored.returncode == 0, restored.stderr
    assert tool_oid in restored.stdout
    assert (worktree / "README.md").read_text(encoding="utf-8") == "original dirty\n"
    assert (worktree / "tracked.txt").read_text(encoding="utf-8") == "unstaged change\n"
    assert (worktree / "untracked.txt").read_text(encoding="utf-8") == "keep me\n"
    assert "README.md" in git_output(worktree, "diff", "--cached", "--name-only")
    assert "tracked.txt" in git_output(worktree, "diff", "--name-only")
    assert git_output(worktree, "show-ref", "--verify", private_ref, check=False) == ""
    assert "unrelated-user-stash" in git_output(worktree, "stash", "list")
    assert context.stash_token in git_output(worktree, "stash", "list")


def test_detached_context_restores_detached_head_and_explicit_ref(tmp_path: Path, git_repo) -> None:
    source = git_repo("context-detached")
    source.branch("feature/inspect")
    config = tmp_path / "config" / "ws.toml"
    config.parent.mkdir()
    write_config(config, source.path)
    result = run_ws(config.parent, "create", "detached", "--config", str(config))
    assert result.returncode == 0, result.stderr
    workspace = tmp_path / "workspaces" / "detached"
    worktree = workspace / "repos" / "app"
    original = git_output(worktree, "rev-parse", "HEAD")
    target = git_output(source.path, "rev-parse", "feature/inspect")

    entered = run_ws(worktree, "context", "app", "feature/inspect")
    assert entered.returncode == 0, entered.stderr
    assert git_output(worktree, "rev-parse", "HEAD") == target
    assert git_output(worktree, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) == ""

    restored = run_ws(worktree, "context", "app", "--restore")
    assert restored.returncode == 0, restored.stderr
    assert git_output(worktree, "rev-parse", "HEAD") == original
    assert git_output(worktree, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) == ""


def test_context_stash_failure_persists_intent_and_failure_with_token(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    _, workspace, worktree = prepare_dirty_context(tmp_path, git_repo, "failure-stash")
    original_run_git = workspace_module.run_git

    def fail_stash(args, *, cwd=None, check=True):
        if tuple(str(arg) for arg in args)[:2] == ("stash", "push"):
            raise GitCommandError(tuple(str(arg) for arg in args), cwd, 1, "", "injected stash")
        return original_run_git(args, cwd=cwd, check=check)

    monkeypatch.setattr(workspace_module, "run_git", fail_stash)
    monkeypatch.chdir(worktree)

    with pytest.raises(WsError, match="stash creation failed"):
        workspace_module.enter_context("app", "default")

    context = context_state(workspace)
    assert context is not None
    intent, failure = effect_pair(context, "stash")
    assert context.phase == "entering"
    assert context.stash_oid is None
    assert context.stash_token
    assert intent.expected_refs["HEAD"] == context.return_saved_head
    assert failure.known_oids == {}


def test_context_private_ref_failure_persists_pinned_stash_oid(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    _, workspace, worktree = prepare_dirty_context(tmp_path, git_repo, "failure-private-ref")

    def fail_private_ref(ref, oid, *, cwd=None):
        raise GitCommandError(("update-ref", ref, oid), cwd, 1, "", "injected private ref")

    monkeypatch.setattr(workspace_module, "create_private_ref", fail_private_ref)
    monkeypatch.chdir(worktree)

    with pytest.raises(WsError, match="private snapshot pinning failed"):
        workspace_module.enter_context("app", "default")

    context = context_state(workspace)
    assert context is not None
    intent, failure = effect_pair(context, "private_ref")
    assert context.phase == "entering"
    assert context.stash_oid is not None
    assert intent.known_oids["stash"] == context.stash_oid
    assert failure.known_oids["stash"] == context.stash_oid
    assert context.private_ref is not None
    assert git_output(worktree, "show-ref", "--verify", context.private_ref, check=False) == ""


def test_context_checkout_failure_persists_target_commit_and_no_recovery(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    source = git_repo("failure-checkout")
    workspace = create_workspace(tmp_path, source.path, "failure-checkout")
    worktree = workspace / "repos" / "app"
    assert run_ws(worktree, "claim", "app", "--target", "feature/failure-checkout").returncode == 0
    target_head = source.commit("context target", content="context target\n")
    original_run_git = workspace_module.run_git

    def fail_context_checkout(args, *, cwd=None, check=True):
        command = tuple(str(arg) for arg in args)
        if command[:2] == ("switch", "--detach") and command[2] == target_head:
            raise GitCommandError(command, cwd, 1, "", "injected checkout")
        return original_run_git(args, cwd=cwd, check=check)

    monkeypatch.setattr(workspace_module, "run_git", fail_context_checkout)
    monkeypatch.chdir(worktree)

    with pytest.raises(WsError, match="context checkout failed"):
        workspace_module.enter_context("app", "default")

    context = context_state(workspace)
    assert context is not None
    intent, failure = effect_pair(context, "checkout")
    assert context.phase == "entering"
    assert intent.expected_refs["HEAD"] == target_head
    assert failure.known_oids["target"] == target_head


def test_context_return_checkout_failure_persists_return_head_without_recovery(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    source = git_repo("failure-return-checkout")
    workspace = create_workspace(tmp_path, source.path, "failure-return-checkout")
    worktree = workspace / "repos" / "app"
    assert run_ws(worktree, "claim", "app", "--target", "feature/failure-return").returncode == 0
    saved_head = git_output(worktree, "rev-parse", "HEAD")
    target_head = source.commit("context target", content="context target\n")
    assert run_ws(worktree, "context", "app", target_head).returncode == 0
    original_run_git = workspace_module.run_git

    def fail_return_checkout(args, *, cwd=None, check=True):
        command = tuple(str(arg) for arg in args)
        if command == ("switch", "feature/failure-return"):
            raise GitCommandError(command, cwd, 1, "", "injected return checkout")
        return original_run_git(args, cwd=cwd, check=check)

    monkeypatch.setattr(workspace_module, "run_git", fail_return_checkout)
    monkeypatch.chdir(worktree)

    with pytest.raises(WsError, match="return checkout failed"):
        workspace_module.restore_context("app")

    context = context_state(workspace)
    assert context is not None
    intent, failure = effect_pair(context, "return_checkout")
    assert context.phase == "restoring"
    assert context.return_saved_head == saved_head
    assert intent.expected_refs["HEAD"] == saved_head
    assert intent.known_oids["return"] == saved_head
    assert failure.known_oids["return"] == saved_head


def test_context_stash_apply_failure_records_pinned_oid_and_partial_dirty_state(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    _, workspace, worktree = prepare_dirty_context(tmp_path, git_repo, "failure-apply")
    assert run_ws(worktree, "context", "app", "default").returncode == 0
    original_run_git = workspace_module.run_git
    fail_apply = True

    def fail_partial_apply(args, *, cwd=None, check=True):
        nonlocal fail_apply
        command = tuple(str(arg) for arg in args)
        if fail_apply and command[:2] == ("stash", "apply"):
            fail_apply = False
            (worktree / "README.md").write_text("partial tracked\n", encoding="utf-8")
            git(worktree, "add", "README.md")
            (worktree / "README.md").write_text("partial unstaged\n", encoding="utf-8")
            (worktree / "partial-untracked.txt").write_text("partial\n", encoding="utf-8")
            raise GitCommandError(command, cwd, 1, "", "injected partial apply")
        return original_run_git(args, cwd=cwd, check=check)

    monkeypatch.setattr(workspace_module, "run_git", fail_partial_apply)
    monkeypatch.chdir(worktree)

    with pytest.raises(WsError, match="stash apply failed"):
        workspace_module.restore_context("app")

    failed_context = context_state(workspace)
    assert failed_context is not None
    intent, failure = effect_pair(failed_context, "stash_apply")
    assert failed_context.phase == "restore_failed"
    assert failed_context.stash_oid is not None
    assert intent.known_oids["stash"] == failed_context.stash_oid
    assert failure.known_oids["stash"] == failed_context.stash_oid
    assert git_output(worktree, "diff", "--cached", "--name-only") == "README.md"
    assert git_output(worktree, "diff", "--name-only") == "README.md"
    assert (worktree / "partial-untracked.txt").is_file()

    dirty_retry = run_ws(worktree, "context", "app", "--restore")
    assert dirty_retry.returncode != 0
    assert "clean return baseline" in dirty_retry.stderr

    saved_head = failed_context.return_saved_head
    assert saved_head is not None
    git(worktree, "reset", "--hard", saved_head)
    git(worktree, "clean", "-fd")
    (worktree / "README.md").write_text("moved return\n", encoding="utf-8")
    git(worktree, "add", "README.md")
    git(worktree, "commit", "-m", "move return branch")
    moved_retry = run_ws(worktree, "context", "app", "--restore")
    assert moved_retry.returncode != 0
    assert "saved return" in moved_retry.stderr

    git(worktree, "reset", "--hard", saved_head)
    restored = run_ws(worktree, "context", "app", "--restore")
    assert restored.returncode == 0, restored.stderr
    assert (worktree / "README.md").read_text(encoding="utf-8") == "saved content\n"


def test_context_private_ref_delete_failure_records_oid_and_retains_snapshot(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    _, workspace, worktree = prepare_dirty_context(tmp_path, git_repo, "failure-delete")
    assert run_ws(worktree, "context", "app", "default").returncode == 0
    active_context = context_state(workspace)
    assert active_context is not None
    private_ref = active_context.private_ref
    stash_oid = active_context.stash_oid
    assert private_ref is not None
    assert stash_oid is not None

    def fail_private_ref_delete(ref, oid, *, cwd=None):
        raise GitCommandError(("update-ref", "-d", ref, oid), cwd, 1, "", "injected delete")

    monkeypatch.setattr(workspace_module, "delete_private_ref", fail_private_ref_delete)
    monkeypatch.chdir(worktree)

    with pytest.raises(WsError, match="snapshot was not deleted"):
        workspace_module.restore_context("app")

    failed_context = context_state(workspace)
    assert failed_context is not None
    intent, failure = effect_pair(failed_context, "private_ref_delete")
    assert failed_context.phase == "restoring"
    assert intent.known_oids["stash"] == stash_oid
    assert failure.known_oids["stash"] == stash_oid
    assert git_output(worktree, "show-ref", "--verify", private_ref) != ""


def test_context_blocks_nested_context_claim_and_dirty_temporary_restore(
    tmp_path: Path, git_repo
) -> None:
    source = git_repo("context-guards")
    workspace = create_workspace(tmp_path, source.path, "guards")
    worktree = workspace / "repos" / "app"
    assert run_ws(worktree, "context", "app", "HEAD").returncode == 0

    nested = run_ws(worktree, "context", "app", "HEAD")
    claim = run_ws(worktree, "claim", "app")
    (worktree / "temporary.txt").write_text("do not discard\n", encoding="utf-8")
    dirty_restore = run_ws(worktree, "context", "app", "--restore")

    assert nested.returncode != 0
    assert "already has an active" in nested.stderr
    assert claim.returncode != 0
    assert "context" in claim.stderr
    assert dirty_restore.returncode != 0
    assert "uncommitted" in dirty_restore.stderr
    assert (worktree / "temporary.txt").is_file()


@pytest.mark.parametrize("return_branch_change", ["moved", "deleted", "other_worktree"])
def test_restore_refuses_changed_claimed_return_branch(
    tmp_path: Path, git_repo, return_branch_change: str
) -> None:
    source = git_repo(f"context-return-{return_branch_change}")
    workspace = create_workspace(tmp_path, source.path, "return-check")
    worktree = workspace / "repos" / "app"
    assert run_ws(worktree, "claim", "app", "--target", "feature/return").returncode == 0
    saved_head = git_output(worktree, "rev-parse", "HEAD")
    assert run_ws(worktree, "context", "app", "HEAD").returncode == 0

    if return_branch_change == "moved":
        replacement = source.commit("moved return", content="moved return\n")
        source.run("branch", "-f", "feature/return", replacement)
    elif return_branch_change == "deleted":
        source.run("branch", "-D", "feature/return")
    else:
        other_worktree = tmp_path / "other-worktree"
        source.run("worktree", "add", str(other_worktree), "feature/return")

    restored = run_ws(worktree, "context", "app", "--restore")

    assert restored.returncode != 0
    assert "saved return" in restored.stderr or "checked out by another" in restored.stderr
    state = deserialize_workspace_state((workspace / ".ws" / "state.toml").read_text())
    context = state.repos["app"].context
    assert context is not None
    assert context.phase == "active"
    assert context.return_saved_head == saved_head


def test_conflicted_restore_requires_finalize_and_retains_private_snapshot(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    source = git_repo("context-conflict")
    source.commit("add resolution notes", filename="notes.txt", content="base notes\n")
    common_head = git_output(source.path, "rev-parse", "HEAD")
    source.commit("context target", content="context target\n")
    alternate_worktree = tmp_path / "alternate-conflict"
    source.run("worktree", "add", str(alternate_worktree), common_head)
    (alternate_worktree / "README.md").write_text("alternate target\n", encoding="utf-8")
    git(alternate_worktree, "add", "README.md")
    git(alternate_worktree, "commit", "-m", "alternate target")
    alternate_head = git_output(alternate_worktree, "rev-parse", "HEAD")
    source.run("worktree", "remove", str(alternate_worktree))
    source.run("branch", "other", alternate_head)
    workspace = create_workspace(tmp_path, source.path, "conflict")
    worktree = workspace / "repos" / "app"
    assert run_ws(worktree, "claim", "app", "--target", "feature/conflict").returncode == 0
    (worktree / "README.md").write_text("saved change\n", encoding="utf-8")
    git(worktree, "add", "README.md")
    assert run_ws(worktree, "context", "app", "other").returncode == 0
    original_run_git = workspace_module.run_git

    def inject_return_conflict(args, *, cwd=None, check=True):
        command = tuple(str(arg) for arg in args)
        if command[:2] == ("stash", "apply"):
            assert cwd == worktree
            original_run_git(["merge", "--no-commit", alternate_head], cwd=worktree, check=False)
            assert original_run_git(["ls-files", "--unmerged"], cwd=worktree).stdout
            raise GitCommandError(command, cwd, 1, "", "injected stash conflict")
        result = original_run_git(args, cwd=cwd, check=check)
        return result

    monkeypatch.setattr(workspace_module, "run_git", inject_return_conflict)
    monkeypatch.chdir(worktree)

    try:
        workspace_module.restore_context("app")
    except WsError as exc:
        assert "conflicted" in str(exc)
    else:
        raise AssertionError("restore unexpectedly succeeded")

    state = deserialize_workspace_state((workspace / ".ws" / "state.toml").read_text())
    context = state.repos["app"].context
    assert context is not None
    assert context.phase == "restore_conflicted"
    assert any(
        effect.operation == "stash_apply" and effect.step == "failure"
        for effect in context.completed_effects
    )
    assert context.private_ref is not None
    private_ref = context.private_ref
    stash_oid = context.stash_oid
    assert stash_oid is not None
    blocked_finalize = run_ws(worktree, "context", "app", "--finalize-restore")
    assert blocked_finalize.returncode != 0
    assert "unmerged" in blocked_finalize.stderr
    (worktree / "README.md").write_text("resolved staged\n", encoding="utf-8")
    git(worktree, "add", "README.md")
    (worktree / "notes.txt").write_text("resolved unstaged\n", encoding="utf-8")
    finalized = run_ws(worktree, "context", "app", "--finalize-restore")

    assert finalized.returncode == 0, finalized.stderr
    assert git_output(worktree, "rev-parse", "HEAD") == context.return_saved_head
    assert git_output(worktree, "diff", "--cached", "--name-only") == "README.md"
    assert git_output(worktree, "diff", "--name-only") == "notes.txt"
    assert (worktree / "README.md").read_text(encoding="utf-8") == "resolved staged\n"
    assert (worktree / "notes.txt").read_text(encoding="utf-8") == "resolved unstaged\n"
    assert git_output(worktree, "show-ref", "--verify", private_ref, check=False) == ""
    assert stash_oid in git_output(worktree, "stash", "list") or context.stash_token in git_output(
        worktree, "stash", "list"
    )
    final_state = deserialize_workspace_state((workspace / ".ws" / "state.toml").read_text())
    assert final_state.repos["app"].context is None


def test_restore_failed_requires_clean_baseline_then_retries_pinned_snapshot(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    source = git_repo("context-retry")
    workspace = create_workspace(tmp_path, source.path, "retry")
    worktree = workspace / "repos" / "app"
    assert run_ws(worktree, "claim", "app", "--target", "feature/retry").returncode == 0
    (worktree / "README.md").write_text("saved retry\n", encoding="utf-8")
    git(worktree, "add", "README.md")
    source.commit("retry target", content="retry target\n")
    assert run_ws(worktree, "context", "app", "default").returncode == 0
    original_run_git = workspace_module.run_git
    fail_apply = True

    def fail_stash_apply(args, *, cwd=None, check=True):
        nonlocal fail_apply
        command = tuple(str(arg) for arg in args)
        if fail_apply and command[:2] == ("stash", "apply"):
            fail_apply = False
            raise GitCommandError(command, cwd, 1, "", "injected apply failure")
        return original_run_git(args, cwd=cwd, check=check)

    monkeypatch.setattr(workspace_module, "run_git", fail_stash_apply)
    monkeypatch.chdir(worktree)

    with pytest.raises(WsError, match="stash apply failed"):
        workspace_module.restore_context("app")

    failed_state = deserialize_workspace_state((workspace / ".ws" / "state.toml").read_text())
    assert failed_state.phase == "restore_failed"
    assert failed_state.repos["app"].context is not None
    retried = workspace_module.restore_context("app")

    assert retried is not None
    assert (worktree / "README.md").read_text(encoding="utf-8") == "saved retry\n"
    final_state = deserialize_workspace_state((workspace / ".ws" / "state.toml").read_text())
    assert final_state.phase == "idle"
    assert final_state.repos["app"].context is None


@pytest.mark.parametrize("phase", ["entering", "restoring"])
def test_interrupted_context_transition_blocks_mutation(
    tmp_path: Path, git_repo, phase: str
) -> None:
    source = git_repo(f"context-interrupted-{phase}")
    workspace = create_workspace(tmp_path, source.path, f"interrupted-{phase}")
    worktree = workspace / "repos" / "app"
    assert run_ws(worktree, "context", "app", "HEAD").returncode == 0

    state_path = workspace / ".ws" / "state.toml"
    state = deserialize_workspace_state(state_path.read_text(encoding="utf-8"))
    repo = state.repos["app"]
    context = repo.context
    assert context is not None
    interrupted_context = replace(context, phase=phase)
    interrupted = replace(
        state,
        phase=phase,
        repos={"app": replace(repo, context=interrupted_context)},
    )
    state_path.write_text(serialize_workspace_state(interrupted), encoding="utf-8")

    attempted_enter = run_ws(worktree, "context", "app", "HEAD")
    attempted_restore = run_ws(worktree, "context", "app", "--restore")

    assert attempted_enter.returncode != 0
    assert "interrupted" in attempted_enter.stderr
    assert attempted_restore.returncode != 0
    assert "interrupted" in attempted_restore.stderr
