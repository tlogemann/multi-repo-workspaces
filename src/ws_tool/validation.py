from __future__ import annotations

import re

_LOGICAL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class InvalidLogicalName(ValueError):
    """A logical workspace or repository identifier is unsafe."""


def validate_logical_name(name: str, *, kind: str) -> str:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
        or not _LOGICAL_NAME.fullmatch(name)
    ):
        raise InvalidLogicalName(
            f"invalid {kind} identifier {name!r}; expected [A-Za-z0-9][A-Za-z0-9._-]*"
        )
    return name
