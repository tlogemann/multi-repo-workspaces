from __future__ import annotations

from types import SimpleNamespace

import ws_tool.cli as cli


def test_help_dispatch_is_available(capsys) -> None:
    assert cli.main(["--help"]) == 0
    help_text = capsys.readouterr().out
    assert "ws init" in help_text
    assert "ws create" in help_text
    assert "ws switch" in help_text
    assert "ws merge" in help_text


def test_init_rejects_workspace_argument() -> None:
    assert cli.main(["init", "feature"]) == 2


def test_create_source_override_remains_accepted(monkeypatch, tmp_path) -> None:
    seen: dict[str, object] = {}

    def fake_create(workspace_name, *, config_path=None, source_overrides=()):
        seen.update(
            workspace_name=workspace_name,
            config_path=config_path,
            source_overrides=source_overrides,
        )
        return SimpleNamespace(workspace=tmp_path / workspace_name)

    monkeypatch.setattr(cli, "create_workspace", fake_create)

    assert cli.main(["create", "feature", "--source", "api=main"]) == 0
    assert seen == {
        "workspace_name": "feature",
        "config_path": None,
        "source_overrides": ["api=main"],
    }


def test_switch_dispatches_ref_and_explicit_repositories(monkeypatch, capsys) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        cli,
        "switch_workspace",
        lambda ref, repository_names=(): seen.update(ref=ref, repositories=repository_names),
    )

    assert cli.main(["switch", "release", "api", "web"]) == 0
    assert seen == {"ref": "release", "repositories": ["api", "web"]}
    assert "Switched" in capsys.readouterr().out


def test_merge_without_names_dispatches_all_repositories(monkeypatch, capsys) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        cli,
        "merge_workspace",
        lambda ref, repository_names=(): seen.update(ref=ref, repositories=repository_names),
    )

    assert cli.main(["merge", "origin/release"]) == 0
    assert seen == {"ref": "origin/release", "repositories": []}
    assert "Merged" in capsys.readouterr().out


def test_switch_requires_ref() -> None:
    assert cli.main(["switch"]) == 2


def test_merge_requires_ref() -> None:
    assert cli.main(["merge"]) == 2
