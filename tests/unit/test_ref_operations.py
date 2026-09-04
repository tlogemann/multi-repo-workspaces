from __future__ import annotations

from pathlib import Path

import pytest

import ws_tool.ref_operations as ref_operations
from ws_tool.errors import GitCommandError, WsError
from ws_tool.git import GitResult
from ws_tool.ref_operations import RefOperationTarget, execute_ref_operation

APP_HEAD = "a" * 40
API_HEAD = "b" * 40
APP_TARGET = "c" * 40
API_TARGET = "d" * 40


def targets(tmp_path: Path) -> tuple[RefOperationTarget, RefOperationTarget]:
    return (
        RefOperationTarget("app", tmp_path / "app"),
        RefOperationTarget("api", tmp_path / "api"),
    )


def mutations(calls: list[tuple[str, tuple[str, ...]]]) -> list[tuple[str, tuple[str, ...]]]:
    return [call for call in calls if call[1][0] in {"switch", "merge"}]


def rollback_calls(calls: list[tuple[str, tuple[str, ...]]]) -> list[tuple[str, tuple[str, ...]]]:
    return [
        call
        for call in calls
        if call[1][0] in {"switch", "reset"} or call[1][:2] == ("merge", "--abort")
    ]


class FakeGit:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.fail_on: tuple[str, tuple[str, ...]] | None = None
        self.failures: set[tuple[str, tuple[str, ...]]] = set()
        self.heads = {"app": APP_HEAD, "api": API_HEAD}
        self.branches: dict[str, str | None] = {"app": "feature/app", "api": "feature/api"}
        self.ref_heads = {"app": APP_TARGET, "api": API_TARGET}

    def __call__(self, args, *, cwd=None, check=True) -> GitResult:
        command = tuple(str(arg) for arg in args)
        if cwd is None:
            raise AssertionError("fake Git requires a cwd")
        call = (cwd.name, command)
        self.calls.append(call)
        if call == self.fail_on or call in self.failures:
            raise GitCommandError(command, cwd, 1, "", f"injected failure: {' '.join(command)}")

        name = cwd.name
        if command == ("status", "--porcelain", "--untracked-files=all"):
            return GitResult(command, cwd, 0, "", "")
        if command == ("ls-files", "--unmerged"):
            return GitResult(command, cwd, 0, "", "")
        if command == ("rev-parse", "HEAD"):
            return GitResult(command, cwd, 0, f"{self.heads[name]}\n", "")
        if command == ("symbolic-ref", "--quiet", "--short", "HEAD"):
            branch = self.branches[name]
            return GitResult(command, cwd, 0 if branch else 1, f"{branch}\n" if branch else "", "")
        if command == ("rev-parse", "--verify", "release^{commit}"):
            return GitResult(command, cwd, 0, f"{self.ref_heads[name]}\n", "")
        if command[0:2] == ("rev-parse", "--git-path"):
            return GitResult(command, cwd, 0, f"{cwd / command[2]}\n", "")
        if command[0] == "switch":
            if command[1] == "--detach":
                self.branches[name] = None
                self.heads[name] = command[2]
            else:
                self.branches[name] = command[1]
            return GitResult(command, cwd, 0, "", "")
        if command[0] == "merge" and command[1] != "--abort":
            self.heads[name] = self.ref_heads[name]
            return GitResult(command, cwd, 0, "", "")
        if command == ("merge", "--abort"):
            return GitResult(command, cwd, 0, "", "")
        if command == ("reset", "--hard", self.heads[name]):
            return GitResult(command, cwd, 0, "", "")
        if command[0] == "reset" and command[1:2] == ("--hard",):
            self.heads[name] = command[2]
            return GitResult(command, cwd, 0, "", "")
        raise AssertionError(f"unexpected Git command: {call}")


def install_fake(monkeypatch, tmp_path: Path) -> FakeGit:
    fake = FakeGit()
    monkeypatch.setattr(ref_operations, "run_git", fake)
    return fake


def test_preflight_completes_for_all_targets_before_mutation(tmp_path: Path, monkeypatch) -> None:
    fake = install_fake(monkeypatch, tmp_path)

    execute_ref_operation("switch", "release", targets(tmp_path))

    first_mutation = next(index for index, call in enumerate(fake.calls) if call[1][0] == "switch")
    assert all(
        call[1][0:2] in {
            ("status", "--porcelain"),
            ("ls-files", "--unmerged"),
            ("rev-parse", "HEAD"),
            ("symbolic-ref", "--quiet"),
            ("rev-parse", "--verify"),
            ("rev-parse", "--git-path"),
        }
        for call in fake.calls[:first_mutation]
    )


@pytest.mark.parametrize("target_ref", ["", "   "])
def test_blank_target_ref_is_rejected_before_git(
    tmp_path: Path, monkeypatch, target_ref: str
) -> None:
    fake = install_fake(monkeypatch, tmp_path)

    with pytest.raises(WsError, match="preflight"):
        execute_ref_operation("switch", target_ref, targets(tmp_path))

    assert fake.calls == []


def test_duplicate_target_names_are_rejected_before_git(tmp_path: Path, monkeypatch) -> None:
    fake = install_fake(monkeypatch, tmp_path)
    duplicate_targets = (targets(tmp_path)[0], targets(tmp_path)[0])

    with pytest.raises(WsError, match="duplicate"):
        execute_ref_operation("switch", "release", duplicate_targets)

    assert fake.calls == []


@pytest.mark.parametrize(
    "kind",
    ["dirty", "unmerged", "operation"],
)
def test_preflight_rejects_ineligible_target_without_mutation(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    fake = install_fake(monkeypatch, tmp_path)
    if kind == "dirty":
        original = fake

        def dirty(args, *, cwd=None, check=True):
            result = original(args, cwd=cwd, check=check)
            if tuple(str(arg) for arg in args) == (
                "status",
                "--porcelain",
                "--untracked-files=all",
            ):
                return GitResult(result.args, result.cwd, 0, " M file\n", "")
            return result

        monkeypatch.setattr(ref_operations, "run_git", dirty)
    elif kind == "unmerged":
        original = fake

        def unmerged(args, *, cwd=None, check=True):
            result = original(args, cwd=cwd, check=check)
            if tuple(str(arg) for arg in args) == ("ls-files", "--unmerged"):
                return GitResult(result.args, result.cwd, 0, "100644 abc\tfile\n", "")
            return result

        monkeypatch.setattr(ref_operations, "run_git", unmerged)
    else:
        marker = tmp_path / "app" / "rebase-merge"
        marker.mkdir(parents=True)

    with pytest.raises(WsError, match=r"preflight app:"):
        execute_ref_operation("switch", "release", targets(tmp_path))

    assert mutations(fake.calls) == []


def test_missing_ref_is_rejected_without_mutation(tmp_path: Path, monkeypatch) -> None:
    fake = install_fake(monkeypatch, tmp_path)
    original = fake

    def missing(args, *, cwd=None, check=True):
        result = original(args, cwd=cwd, check=check)
        if cwd is not None and cwd.name == "api" and tuple(str(arg) for arg in args)[0:2] == (
            "rev-parse",
            "--verify",
        ):
            raise GitCommandError(tuple(str(arg) for arg in args), cwd, 128, "", "missing ref")
        return result

    monkeypatch.setattr(ref_operations, "run_git", missing)
    with pytest.raises(WsError, match=r"preflight api:"):
        execute_ref_operation("switch", "release", targets(tmp_path))
    assert mutations(fake.calls) == []


def test_merge_requires_attached_branch_without_mutation(tmp_path: Path, monkeypatch) -> None:
    fake = install_fake(monkeypatch, tmp_path)
    fake.branches["api"] = None

    with pytest.raises(WsError, match=r"preflight api:.*attached"):
        execute_ref_operation("merge", "release", targets(tmp_path))

    assert mutations(fake.calls) == []


def test_successful_switch_returns_detached_results(tmp_path: Path, monkeypatch) -> None:
    install_fake(monkeypatch, tmp_path)

    result = execute_ref_operation("switch", "release", targets(tmp_path))

    assert result == (
        ref_operations.RefOperationResult("app", APP_TARGET, None),
        ref_operations.RefOperationResult("api", API_TARGET, None),
    )


def test_failed_second_merge_rolls_back_in_reverse_order(tmp_path: Path, monkeypatch) -> None:
    fake = install_fake(monkeypatch, tmp_path)
    fake.fail_on = ("api", ("merge", API_TARGET))

    with pytest.raises(WsError) as caught:
        execute_ref_operation("merge", "release", targets(tmp_path))

    assert "merge" in str(caught.value)
    assert rollback_calls(fake.calls) == [
        ("api", ("switch", "feature/api")),
        ("api", ("reset", "--hard", API_HEAD)),
        ("app", ("switch", "feature/app")),
        ("app", ("reset", "--hard", APP_HEAD)),
    ]


def test_rollback_failure_is_reported_with_primary_failure(tmp_path: Path, monkeypatch) -> None:
    fake = install_fake(monkeypatch, tmp_path)
    fake.fail_on = ("api", ("merge", API_TARGET))
    fake.failures.add(("app", ("reset", "--hard", APP_HEAD)))

    with pytest.raises(WsError) as caught:
        execute_ref_operation("merge", "release", targets(tmp_path))

    message = str(caught.value)
    assert "merge" in message
    assert "rollback" in message
    assert "app" in message


def test_recovered_rollback_command_failure_is_not_reported_as_diagnostic(
    tmp_path: Path, monkeypatch
) -> None:
    fake = install_fake(monkeypatch, tmp_path)
    fake.fail_on = ("api", ("merge", API_TARGET))
    fake.failures.add(("api", ("reset", "--hard", API_HEAD)))

    with pytest.raises(WsError) as caught:
        execute_ref_operation("merge", "release", targets(tmp_path))

    assert "rollback diagnostics" not in str(caught.value)
    assert "rollback completed" in str(caught.value)


def test_detached_snapshot_rolls_back_with_detached_switch(tmp_path: Path, monkeypatch) -> None:
    fake = install_fake(monkeypatch, tmp_path)
    fake.branches["app"] = None
    fake.fail_on = ("api", ("switch", "--detach", API_TARGET))

    with pytest.raises(WsError):
        execute_ref_operation("switch", "release", targets(tmp_path))

    assert ("app", ("switch", "--detach", APP_HEAD)) in fake.calls


def test_post_mutation_callback_failure_rolls_back_targets(
    tmp_path: Path, monkeypatch
) -> None:
    fake = install_fake(monkeypatch, tmp_path)

    def fail_persist() -> None:
        raise WsError("state persistence failed")

    with pytest.raises(WsError, match="state persistence failed") as caught:
        execute_ref_operation(
            "switch", "release", targets(tmp_path), post_mutation=fail_persist
        )

    assert "workspace-state persistence failed" in str(caught.value)
    assert "switch api" not in str(caught.value)
    assert rollback_calls(fake.calls)[-4:] == [
        ("api", ("switch", "feature/api")),
        ("api", ("reset", "--hard", API_HEAD)),
        ("app", ("switch", "feature/app")),
        ("app", ("reset", "--hard", APP_HEAD)),
    ]


def test_post_mutation_result_failure_names_actual_target(tmp_path: Path, monkeypatch) -> None:
    fake = install_fake(monkeypatch, tmp_path)
    original = ref_operations._read_result

    def fail_api_result(target: RefOperationTarget) -> ref_operations.RefOperationResult:
        if target.name == "api":
            raise ValueError("result read failed")
        return original(target)

    monkeypatch.setattr(ref_operations, "_read_result", fail_api_result)

    with pytest.raises(WsError, match=r"switch api:.*result read failed"):
        execute_ref_operation("switch", "release", targets(tmp_path))

    assert fake.heads == {"app": APP_HEAD, "api": API_HEAD}


def test_merge_abort_only_runs_for_an_active_merge(tmp_path: Path, monkeypatch) -> None:
    fake = install_fake(monkeypatch, tmp_path)
    original = fake

    def fail_with_active_merge(args, *, cwd=None, check=True):
        command = tuple(str(arg) for arg in args)
        if cwd is not None and cwd.name == "api" and command == ("merge", API_TARGET):
            cwd.mkdir(parents=True, exist_ok=True)
            (cwd / "MERGE_HEAD").touch()
            raise GitCommandError(command, cwd, 1, "", "active merge")
        return original(args, cwd=cwd, check=check)

    monkeypatch.setattr(ref_operations, "run_git", fail_with_active_merge)
    with pytest.raises(WsError) as caught:
        execute_ref_operation("merge", "release", targets(tmp_path))

    assert "rollback diagnostics" not in str(caught.value)
    assert ("api", ("merge", "--abort")) in fake.calls
    assert ("app", ("merge", "--abort")) not in fake.calls
