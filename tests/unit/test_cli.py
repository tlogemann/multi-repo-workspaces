from __future__ import annotations

from ws_tool.cli import main


def test_help_dispatch_is_available(capsys) -> None:
    assert main(["--help"]) == 0
    assert "ws create" in capsys.readouterr().out
