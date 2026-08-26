from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import ws_tool.workspace as workspace_module
from ws_tool.errors import GitCommandError, WsError
from ws_tool.git import worktree_admin_path
from ws_tool.models import ContextState, RemovalRepoState, RemovalState, RepoState, WorkspaceState
from ws_tool.serialization import deserialize_workspace_lock, write_workspace_state
from ws_tool.workspace import create_workspace


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
        f"[project]\nworkspace_root = {workspace_root}\n\n{repos}",
        encoding="utf-8",
    )
    return path


def repo_table(name: str, source: Path, default_branch: str | None = None) -> str:
    default = "" if default_branch is None else f'\ndefault_branch = "{default_branch}"'
    return f'[repos."{name}"]\npath = "{source}"{default}\n'


def test_create_multi_repo_and_status_from_nested_directory(tmp_path: Path, git_repo) -> None:
    app = git_repo("app")
    library = git_repo("library")
    library.branch("develop")
    library.run("switch", "develop")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = write_config(
        config_dir / "ws.toml",
        '"../workspaces"',
        repo_table("app", app.path) + repo_table("library", library.path),
    )

    created = run_ws(config_dir, "create", "demo", "--config", str(config))

    assert created.returncode == 0, created.stderr
    workspace = tmp_path / "workspaces" / "demo"
    assert (workspace / "workspace.lock.toml").is_file()
    assert (workspace / ".ws" / "state.toml").is_file()
    lock = deserialize_workspace_lock(
        (workspace / "workspace.lock.toml").read_text(encoding="utf-8")
    )
    assert set(lock.repos) == {"app", "library"}
    assert lock.repos["app"].default_selector == "main"
    assert lock.repos["library"].default_selector == "develop"
    for name, source in (("app", app), ("library", library)):
        worktree = workspace / "repos" / name
        assert worktree.is_dir()
        assert run_ws(worktree, "status").returncode == 0
        assert source.run("rev-parse", "HEAD").stdout.strip() == lock.repos[name].base_commit
        assert run_git_output(worktree, "symbolic-ref", "--quiet", "--short", "HEAD") == ""

    nested = workspace / "repos" / "app" / "nested"
    nested.mkdir()
    (workspace / "repos" / "app" / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    human = run_ws(nested, "status")
    assert human.returncode == 0
    assert "WORKSPACE demo" in human.stdout
    assert "app" in human.stdout
    assert "detached" in human.stdout
    status = run_ws(nested, "status", "--json")

    assert status.returncode == 0, status.stderr
    payload = json.loads(status.stdout)
    assert payload["workspace"] == "demo"
    assert payload["repos"]["app"]["locked_base_commit"] == lock.repos["app"].base_commit
    assert payload["repos"]["app"]["head"] == lock.repos["app"].base_commit
    assert payload["repos"]["app"]["detached"] is True
    assert payload["repos"]["app"]["dirty"] is True
    assert payload["repos"]["app"]["context"] is None


def run_git_output(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def test_create_source_override_branch_tag_and_sha(tmp_path: Path, git_repo) -> None:
    branch_source = git_repo("branch-source")
    tag_source = git_repo("tag-source")
    sha_source = git_repo("sha-source")
    base = branch_source.run("rev-parse", "HEAD").stdout.strip()
    branch_source.branch("feature/special")
    tag_source.commit("tagged", content="tagged\n")
    tag_source.run("tag", "v1")
    sha_source.commit("second", content="second\n")
    second = sha_source.run("rev-parse", "HEAD").stdout.strip()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = write_config(
        config_dir / "ws.toml",
        '"../workspaces"',
        repo_table("branch", branch_source.path)
        + repo_table("tag", tag_source.path)
        + repo_table("sha", sha_source.path),
    )

    result = run_ws(
        config_dir,
        "create",
        "demo",
        "--config",
        str(config),
        "--source",
        "branch=feature/special",
        "--source",
        "tag=v1",
        "--source",
        f"sha={second}",
    )

    assert result.returncode == 0, result.stderr
    lock = deserialize_workspace_lock(
        (tmp_path / "workspaces" / "demo" / "workspace.lock.toml").read_text(encoding="utf-8")
    )
    assert lock.repos["branch"].base_commit == base
    assert lock.repos["tag"].base_commit == tag_source.run("rev-parse", "v1").stdout.strip()
    assert lock.repos["sha"].base_commit == second


def test_status_error_is_stdout_free(tmp_path: Path) -> None:
    result = run_ws(tmp_path, "status", "--json")

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr.strip()


def test_status_rejects_renamed_workspace_with_authoritative_lock_name(
    tmp_path: Path, git_repo
) -> None:
    source = git_repo("rename-source")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = write_config(config_dir / "ws.toml", '"../workspaces"', repo_table("app", source.path))
    assert run_ws(config_dir, "create", "original", "--config", str(config)).returncode == 0
    original = tmp_path / "workspaces" / "original"
    renamed = tmp_path / "workspaces" / "renamed"
    original.rename(renamed)

    result = run_ws(renamed, "status", "--json")

    assert result.returncode != 0
    assert result.stdout == ""
    assert "workspace name" in result.stderr


def test_status_rejects_copied_metadata_without_registered_worktrees(
    tmp_path: Path, git_repo
) -> None:
    source = git_repo("copied-source")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = write_config(config_dir / "ws.toml", '"../workspaces"', repo_table("app", source.path))
    assert run_ws(config_dir, "create", "source", "--config", str(config)).returncode == 0
    original = tmp_path / "workspaces" / "source"
    copied = tmp_path / "other" / "source"
    copied.mkdir(parents=True)
    shutil.copy2(original / "workspace.lock.toml", copied / "workspace.lock.toml")
    (copied / ".ws").mkdir()
    shutil.copy2(original / ".ws" / "state.toml", copied / ".ws" / "state.toml")

    result = run_ws(copied, "status", "--json")

    assert result.returncode != 0
    assert result.stdout == ""
    assert "not registered" in result.stderr


def test_create_bare_source_with_explicit_override_records_null_default(
    tmp_path: Path, git_repo, bare_git_repo
) -> None:
    seed = git_repo("seed")
    bare = bare_git_repo("bare.git")
    seed.run("remote", "add", "origin", str(bare))
    seed.run("push", "origin", "main")
    seed.run("--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = write_config(
        config_dir / "ws.toml",
        '"../workspaces"',
        repo_table("bare", bare),
    )

    result = run_ws(
        config_dir,
        "create",
        "bare-demo",
        "--config",
        str(config),
        "--source",
        "bare=main",
    )

    assert result.returncode == 0, result.stderr
    lock = deserialize_workspace_lock(
        (tmp_path / "workspaces" / "bare-demo" / "workspace.lock.toml").read_text(encoding="utf-8")
    )
    assert lock.repos["bare"].default_selector is None
    assert (tmp_path / "workspaces" / "bare-demo" / "repos" / "bare").is_dir()


def test_explicit_source_allows_missing_default_selector(tmp_path: Path, git_repo) -> None:
    source = git_repo("missing-default")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = write_config(
        config_dir / "ws.toml",
        '"../workspaces"',
        repo_table("app", source.path, "missing/default"),
    )

    result = run_ws(
        config_dir,
        "create",
        "missing-default-demo",
        "--config",
        str(config),
        "--source",
        "app=HEAD",
    )

    assert result.returncode == 0, result.stderr
    lock = deserialize_workspace_lock(
        (tmp_path / "workspaces" / "missing-default-demo" / "workspace.lock.toml").read_text(
            encoding="utf-8"
        )
    )
    assert lock.repos["app"].default_selector is None


def test_explicit_source_allows_ambiguous_remote_defaults_in_bare_source(
    tmp_path: Path, git_repo, bare_git_repo
) -> None:
    seed = git_repo("ambiguous-seed")
    seed.branch("develop")
    seed.commit("main divergence", content="main divergence\n")
    bare = bare_git_repo("ambiguous.git")
    seed.run("remote", "add", "origin", str(bare))
    seed.run("push", "origin", "main")
    seed.run("push", "origin", "develop")
    seed.run(
        "--git-dir",
        str(bare),
        "symbolic-ref",
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/main",
    )
    seed.run(
        "--git-dir",
        str(bare),
        "symbolic-ref",
        "refs/remotes/upstream/HEAD",
        "refs/remotes/origin/develop",
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = write_config(config_dir / "ws.toml", '"../workspaces"', repo_table("app", bare))

    result = run_ws(
        config_dir,
        "create",
        "ambiguous-demo",
        "--config",
        str(config),
        "--source",
        "app=main",
    )

    assert result.returncode == 0, result.stderr
    lock = deserialize_workspace_lock(
        (tmp_path / "workspaces" / "ambiguous-demo" / "workspace.lock.toml").read_text(
            encoding="utf-8"
        )
    )
    assert lock.repos["app"].default_selector is None


def test_create_rejects_same_named_remote_heads_at_different_commits(
    tmp_path: Path, git_repo, bare_git_repo
) -> None:
    source = git_repo("disagreeing-remotes")
    origin = bare_git_repo("origin.git")
    upstream = bare_git_repo("upstream.git")
    source.add_remote(origin, "origin")
    source.commit("second", content="second\n")
    source.add_remote(upstream, "upstream")
    source.run(
        "symbolic-ref",
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/main",
    )
    source.run(
        "symbolic-ref",
        "refs/remotes/upstream/HEAD",
        "refs/remotes/upstream/main",
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = write_config(config_dir / "ws.toml", '"../workspaces"', repo_table("app", source.path))

    result = run_ws(config_dir, "create", "disagreeing", "--config", str(config))

    assert result.returncode != 0
    assert "remote symbolic HEADs disagree" in result.stderr


def test_create_accepts_differently_named_remote_heads_at_same_commit(
    tmp_path: Path, git_repo, bare_git_repo
) -> None:
    source = git_repo("agreeing-remotes")
    origin = bare_git_repo("origin.git")
    upstream = bare_git_repo("upstream.git")
    source.add_remote(origin, "origin")
    source.add_remote(upstream, "upstream")
    origin_main = source.run("rev-parse", "refs/remotes/origin/main").stdout.strip()
    source.run("update-ref", "refs/remotes/upstream/develop", origin_main)
    source.run(
        "symbolic-ref",
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/main",
    )
    source.run(
        "symbolic-ref",
        "refs/remotes/upstream/HEAD",
        "refs/remotes/upstream/develop",
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = write_config(config_dir / "ws.toml", '"../workspaces"', repo_table("app", source.path))

    result = run_ws(config_dir, "create", "agreeing", "--config", str(config))

    assert result.returncode == 0, result.stderr
    lock = deserialize_workspace_lock(
        (tmp_path / "workspaces" / "agreeing" / "workspace.lock.toml").read_text(
            encoding="utf-8"
        )
    )
    assert lock.repos["app"].default_selector == "refs/remotes/origin/main"


def test_create_rejects_unresolvable_remote_symbolic_head(
    tmp_path: Path, git_repo, bare_git_repo
) -> None:
    source = git_repo("invalid-remote-head")
    remote = bare_git_repo("origin.git")
    source.add_remote(remote)
    tree_oid = source.run("rev-parse", "HEAD^{tree}").stdout.strip()
    source.run("update-ref", "refs/remotes/origin/not-a-commit", tree_oid)
    source.run(
        "symbolic-ref",
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/not-a-commit",
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = write_config(config_dir / "ws.toml", '"../workspaces"', repo_table("app", source.path))

    result = run_ws(config_dir, "create", "invalid", "--config", str(config))

    assert result.returncode != 0
    assert "does not resolve to a commit" in result.stderr


def test_create_rejects_existing_target_and_lifecycle_lock_without_cleanup(
    tmp_path: Path, git_repo
) -> None:
    source = git_repo("source")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = write_config(
        config_dir / "ws.toml",
        '"../workspaces"',
        repo_table("app", source.path),
    )
    target = tmp_path / "workspaces" / "demo"
    target.mkdir(parents=True)
    marker = target / "keep.txt"
    marker.write_text("keep\n", encoding="utf-8")

    result = run_ws(config_dir, "create", "demo", "--config", str(config))

    assert result.returncode != 0
    assert result.stdout == ""
    assert marker.read_text(encoding="utf-8") == "keep\n"


def test_create_uses_only_canonical_cwd_config_by_default(tmp_path: Path, git_repo) -> None:
    source = git_repo("source")
    write_config(
        tmp_path / "ws.toml",
        '"workspaces"',
        repo_table("app", source.path),
    )

    created = run_ws(tmp_path, "create", "default-config")

    assert created.returncode == 0, created.stderr
    assert (tmp_path / "workspaces" / "default-config").is_dir()

    nested = tmp_path / "nested"
    nested.mkdir()
    no_search = run_ws(nested, "create", "must-fail")
    assert no_search.returncode != 0
    assert "configuration file" in no_search.stderr


def test_create_rolls_back_real_worktrees_after_injected_second_add_failure(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    first = git_repo("first")
    second = git_repo("second")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = write_config(
        config_dir / "ws.toml",
        '"../workspaces"',
        repo_table("first", first.path) + repo_table("second", second.path),
    )
    original_run_git = workspace_module.run_git
    worktree_adds = 0

    def fail_second_worktree_add(args, *, cwd=None, check=True):
        nonlocal worktree_adds
        command = tuple(str(arg) for arg in args)
        if command[:2] == ("worktree", "add"):
            worktree_adds += 1
            if worktree_adds == 2:
                raise GitCommandError(command, cwd, 1, "", "injected worktree failure")
        return original_run_git(args, cwd=cwd, check=check)

    monkeypatch.setattr(workspace_module, "run_git", fail_second_worktree_add)

    with pytest.raises(GitCommandError, match="injected worktree failure"):
        create_workspace("rollback", config_path=config)

    workspace = tmp_path / "workspaces" / "rollback"
    assert not workspace.exists()
    for source in (first, second):
        registered = source.run("worktree", "list", "--porcelain").stdout
        assert str(workspace) not in registered


def test_create_retains_lifecycle_lock_on_unexpected_identity_cleanup_failure(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    first = git_repo("unexpected-first")
    second = git_repo("unexpected-second")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = write_config(
        config_dir / "ws.toml",
        '"../workspaces"',
        repo_table("first", first.path) + repo_table("second", second.path),
    )
    original_run_git = workspace_module.run_git
    original_validate = workspace_module.validate_worktree_registration
    worktree_adds = 0

    def fail_second_worktree_add(args, *, cwd=None, check=True):
        nonlocal worktree_adds
        command = tuple(str(arg) for arg in args)
        if command[:2] == ("worktree", "add"):
            worktree_adds += 1
            if worktree_adds == 2:
                raise GitCommandError(command, cwd, 1, "", "injected worktree failure")
        return original_run_git(args, cwd=cwd, check=check)

    def fail_identity_verification(source: Path, expected_path: Path):
        original_validate(source, expected_path)
        raise RuntimeError("injected identity verification failure")

    monkeypatch.setattr(workspace_module, "run_git", fail_second_worktree_add)
    monkeypatch.setattr(
        workspace_module, "validate_worktree_registration", fail_identity_verification
    )

    with pytest.raises(WsError, match="cleanup failed unexpectedly|Retain"):
        create_workspace("unexpected-rollback", config_path=config)

    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / "unexpected-rollback"
    assert (workspace_root / ".unexpected-rollback.lifecycle.lock").is_dir()
    assert (workspace / "repos" / "first").is_dir()
    assert (workspace / ".ws" / "operation.lock").is_dir()


def test_sigint_during_creation_reraises_after_verified_rollback(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    first = git_repo("sigint-first")
    second = git_repo("sigint-second")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = write_config(
        config_dir / "ws.toml",
        '"../workspaces"',
        repo_table("first", first.path) + repo_table("second", second.path),
    )
    original_run_git = workspace_module.run_git
    worktree_adds = 0
    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / "sigint-creation"

    def interrupt_second_worktree_add(args, *, cwd=None, check=True):
        nonlocal worktree_adds
        command = tuple(str(arg) for arg in args)
        if command[:2] == ("worktree", "add"):
            worktree_adds += 1
            if worktree_adds == 2:
                assert (workspace_root / ".sigint-creation.lifecycle.lock").is_dir()
                assert (workspace / ".ws" / "operation.lock").is_dir()
                raise KeyboardInterrupt
        return original_run_git(args, cwd=cwd, check=check)

    monkeypatch.setattr(workspace_module, "run_git", interrupt_second_worktree_add)

    with pytest.raises(KeyboardInterrupt):
        create_workspace("sigint-creation", config_path=config)

    assert not workspace.exists()
    assert not (workspace_root / ".sigint-creation.lifecycle.lock").exists()
    for source in (first, second):
        assert str(workspace) not in source.run("worktree", "list", "--porcelain").stdout


def test_sigint_with_ordinary_cleanup_failure_reraises_original_interrupt(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    first = git_repo("sigint-original-first")
    second = git_repo("sigint-original-second")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = write_config(
        config_dir / "ws.toml",
        '"../workspaces"',
        repo_table("first", first.path) + repo_table("second", second.path),
    )
    original_run_git = workspace_module.run_git
    worktree_adds = 0
    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / "sigint-original"

    def interrupt_second_worktree_add(args, *, cwd=None, check=True):
        nonlocal worktree_adds
        command = tuple(str(arg) for arg in args)
        if command[:2] == ("worktree", "add"):
            worktree_adds += 1
            if worktree_adds == 2:
                raise KeyboardInterrupt("injected creation interrupt")
        return original_run_git(args, cwd=cwd, check=check)

    def fail_cleanup_identity_verification(_source: Path, _expected_path: Path):
        raise RuntimeError("injected ordinary cleanup failure")

    monkeypatch.setattr(workspace_module, "run_git", interrupt_second_worktree_add)
    monkeypatch.setattr(
        workspace_module,
        "validate_worktree_registration",
        fail_cleanup_identity_verification,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        create_workspace("sigint-original", config_path=config)

    assert "injected ordinary cleanup failure" in "\n".join(caught.value.__notes__)
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert (workspace_root / ".sigint-original.lifecycle.lock").is_dir()
    assert (workspace / ".ws" / "operation.lock").is_dir()


def test_sigint_during_cleanup_retains_both_creation_locks(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    first = git_repo("sigint-cleanup-first")
    second = git_repo("sigint-cleanup-second")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = write_config(
        config_dir / "ws.toml",
        '"../workspaces"',
        repo_table("first", first.path) + repo_table("second", second.path),
    )
    original_run_git = workspace_module.run_git
    worktree_adds = 0
    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / "sigint-cleanup"

    def fail_second_worktree_add(args, *, cwd=None, check=True):
        nonlocal worktree_adds
        command = tuple(str(arg) for arg in args)
        if command[:2] == ("worktree", "add"):
            worktree_adds += 1
            if worktree_adds == 2:
                raise GitCommandError(command, cwd, 1, "", "injected worktree failure")
        return original_run_git(args, cwd=cwd, check=check)

    def interrupt_identity_verification(_source: Path, _expected_path: Path):
        raise KeyboardInterrupt

    monkeypatch.setattr(workspace_module, "run_git", fail_second_worktree_add)
    monkeypatch.setattr(
        workspace_module, "validate_worktree_registration", interrupt_identity_verification
    )

    with pytest.raises(KeyboardInterrupt):
        create_workspace("sigint-cleanup", config_path=config)

    assert (workspace_root / ".sigint-cleanup.lifecycle.lock").is_dir()
    assert (workspace / ".ws" / "operation.lock").is_dir()
    assert (workspace / "repos" / "first").is_dir()


def test_create_holds_both_locks_until_successful_completion(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    source = git_repo("lock-source")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = write_config(config_dir / "ws.toml", '"../workspaces"', repo_table("app", source.path))
    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / "locks"
    original_release = workspace_module._release_operation_lock
    observed: list[tuple[bool, bool]] = []

    def observe_release(path: Path) -> None:
        observed.append((path.is_dir(), (workspace_root / ".locks.lifecycle.lock").is_dir()))
        original_release(path)

    monkeypatch.setattr(workspace_module, "_release_operation_lock", observe_release)

    create_workspace("locks", config_path=config)

    assert observed == [(True, True)]
    assert not (workspace / ".ws" / "operation.lock").exists()
    assert not (workspace_root / ".locks.lifecycle.lock").exists()


def test_create_rejects_dangling_removal_tombstone_symlink(tmp_path: Path, git_repo) -> None:
    source = git_repo("dangling-tombstone-source")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = write_config(config_dir / "ws.toml", '"../workspaces"', repo_table("app", source.path))
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    tombstone = workspace_root / ".dangling.removing"
    tombstone.symlink_to(tmp_path / "does-not-exist")

    result = run_ws(config_dir, "create", "dangling", "--config", str(config))

    assert result.returncode != 0
    assert "tombstone" in result.stderr
    assert tombstone.is_symlink()
    assert not (workspace_root / ".dangling.lifecycle.lock").exists()


def test_failed_cleanup_retains_dirty_worktree_and_lifecycle_lock(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    first = git_repo("dirty-first")
    second = git_repo("dirty-second")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = write_config(
        config_dir / "ws.toml",
        '"../workspaces"',
        repo_table("first", first.path) + repo_table("second", second.path),
    )
    original_run_git = workspace_module.run_git
    worktree_adds = 0
    worktree = tmp_path / "workspaces" / "dirty-rollback" / "repos" / "first"

    def fail_after_dirtying_first(args, *, cwd=None, check=True):
        nonlocal worktree_adds
        command = tuple(str(arg) for arg in args)
        if command[:2] == ("worktree", "add"):
            worktree_adds += 1
            if worktree_adds == 2:
                (worktree / "concurrent.txt").write_text("preserve\n", encoding="utf-8")
                raise GitCommandError(command, cwd, 1, "", "injected worktree failure")
        return original_run_git(args, cwd=cwd, check=check)

    monkeypatch.setattr(workspace_module, "run_git", fail_after_dirtying_first)

    with pytest.raises(WsError, match="manual cleanup|recovery"):
        create_workspace("dirty-rollback", config_path=config)

    assert (worktree / "concurrent.txt").read_text(encoding="utf-8") == "preserve\n"
    assert (tmp_path / "workspaces" / ".dirty-rollback.lifecycle.lock").is_dir()
    assert str(worktree) in first.run("worktree", "list", "--porcelain").stdout


def test_status_reports_durable_context_mode_and_phase(tmp_path: Path, git_repo) -> None:
    source = git_repo("context-status")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = write_config(config_dir / "ws.toml", '"../workspaces"', repo_table("app", source.path))
    assert run_ws(config_dir, "create", "context-status", "--config", str(config)).returncode == 0
    workspace = tmp_path / "workspaces" / "context-status"
    state_path = workspace / ".ws" / "state.toml"
    commit = source.run("rev-parse", "HEAD").stdout.strip()
    state = WorkspaceState(
        workspace_name="context-status",
        phase="idle",
        repos={
            "app": RepoState(
                name="app",
                mode="context",
                head=commit,
                detached=True,
                context=ContextState(
                    target_ref="origin/main",
                    target_commit=commit,
                    phase="entering",
                    return_mode="detached",
                    stash_token="status-token",
                    return_saved_head=commit,
                ),
            )
        },
    )
    write_workspace_state(state_path, state)

    result = run_ws(workspace, "status", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["repos"]["app"]["mode"] == "context"
    assert payload["repos"]["app"]["context"]["phase"] == "entering"
    assert payload["repos"]["app"]["context"]["target_ref"] == "origin/main"


def test_status_reports_partial_removal_after_completed_worktree_is_removed(
    tmp_path: Path, git_repo
) -> None:
    first = git_repo("removal-status-first")
    second = git_repo("removal-status-second")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = write_config(
        config_dir / "ws.toml",
        '"../workspaces"',
        repo_table("first", first.path) + repo_table("second", second.path),
    )
    assert run_ws(config_dir, "create", "removal-status", "--config", str(config)).returncode == 0
    workspace = tmp_path / "workspaces" / "removal-status"
    first_worktree = workspace / "repos" / "first"
    second_worktree = workspace / "repos" / "second"
    first_admin = worktree_admin_path(first_worktree)
    second_admin = worktree_admin_path(second_worktree)
    first_head = first.run("rev-parse", "HEAD").stdout.strip()
    second_head = second.run("rev-parse", "HEAD").stdout.strip()
    state = WorkspaceState(
        workspace_name="removal-status",
        phase="removing",
        repos={
            "first": RepoState(name="first", mode="detached", head=first_head, detached=True),
            "second": RepoState(name="second", mode="detached", head=second_head, detached=True),
        },
        removal=RemovalState(
            phase="removing",
            workspace_path=workspace,
            tombstone_path=workspace.parent / ".removal-status.removing",
            repos={
                "first": RemovalRepoState(
                    name="first",
                    worktree_path=first_worktree,
                    git_admin_path=first_admin,
                    complete=True,
                ),
                "second": RemovalRepoState(
                    name="second",
                    worktree_path=second_worktree,
                    git_admin_path=second_admin,
                    complete=False,
                ),
            },
        ),
    )
    write_workspace_state(workspace / ".ws" / "state.toml", state)
    first.run("worktree", "remove", str(first_worktree))

    result = run_ws(workspace, "status", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["removal"] == {"phase": "removing", "completed": ["first"]}
    assert payload["repos"]["first"]["head"] == first_head
    assert payload["repos"]["second"]["head"] == second_head
