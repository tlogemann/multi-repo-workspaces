from __future__ import annotations

from pathlib import Path


class WsError(Exception):
    """Base class for expected workspace-tool failures."""


class ConfigError(WsError):
    """The project configuration is invalid or unavailable."""


class SerializationError(WsError):
    """Workspace metadata could not be serialized, parsed, or replaced safely."""


class GitCommandError(WsError):
    """A Git subprocess failed, with enough context for diagnosis."""

    def __init__(
        self,
        args: tuple[str, ...],
        cwd: Path | None,
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        self.args_list = args
        self.cwd = cwd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        command = " ".join(["git", *args])
        location = f" in {cwd}" if cwd is not None else ""
        detail = stderr.strip() or stdout.strip() or "no output"
        super().__init__(f"Git command failed{location}: {command} (exit {returncode}): {detail}")
