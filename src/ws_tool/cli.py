from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ws",
        description="Manage isolated multi-repository Git workspaces.",
        epilog="Phase 1 syntax: ws create <workspace> [--config PATH]",
    )
    commands = parser.add_subparsers(dest="command")

    create = commands.add_parser("create", help="create a workspace (future phase)")
    create.add_argument("workspace")
    create.add_argument("--config", type=str)
    create.add_argument("--source", action="append", default=[])

    status = commands.add_parser("status", help="show workspace state (future phase)")
    status.add_argument("--json", action="store_true")

    claim = commands.add_parser("claim", help="claim a repository (future phase)")
    claim.add_argument("repo")
    claim.add_argument("--source")
    claim.add_argument("--target")

    context = commands.add_parser("context", help="temporarily inspect a ref (future phase)")
    context.add_argument("repo")
    context.add_argument("ref", nargs="?")
    context_actions = context.add_mutually_exclusive_group()
    context_actions.add_argument("--restore", action="store_true")
    context_actions.add_argument("--finalize-restore", action="store_true")

    remove = commands.add_parser("remove", help="remove a workspace (future phase)")
    remove.add_argument("workspace")
    remove.add_argument("--config", type=str)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code is None:
            return 0
        return exc.code if isinstance(exc.code, int) else 2

    if args.command is None:
        parser.print_help()
        return 0
    print(f"ws {args.command} is not implemented in Phase 1", file=sys.stderr)
    return 2
