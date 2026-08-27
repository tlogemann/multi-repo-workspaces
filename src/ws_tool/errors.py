from __future__ import annotations

import re
from pathlib import Path

_STANDARD_URL_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s?#@]+@")
_SCP_URL_USERINFO = re.compile(r"(?<![\w/@])[^/\s:@]+@([^/\s:]+):")
_URL_QUERY_VALUE = re.compile(r"([?&][^=\s&#]+)=([^&#\s]*)")


def redact_sensitive_url(value: str) -> str:
    """Remove URL credentials and query values before exposing diagnostics."""

    redacted = _STANDARD_URL_USERINFO.sub(r"\1", value)
    redacted = _SCP_URL_USERINFO.sub(r"[REDACTED]@\1:", redacted)
    return _URL_QUERY_VALUE.sub(r"\1[REDACTED]", redacted)


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
        self.args_list = tuple(redact_sensitive_url(arg) for arg in args)
        self.cwd = cwd
        self.returncode = returncode
        self.stdout = redact_sensitive_url(stdout)
        self.stderr = redact_sensitive_url(stderr)
        command = " ".join(["git", *self.args_list])
        location = f" in {cwd}" if cwd is not None else ""
        detail = self.stderr.strip() or self.stdout.strip() or "no output"
        super().__init__(f"Git command failed{location}: {command} (exit {returncode}): {detail}")


class GitWorktreeError(WsError):
    """Git worktree registration or identity did not match expectations."""
