from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from .errors import WsError
from .workspace import claim_workspace, create_workspace, render_status_human, status_workspace


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ws",
        description="Manage isolated multi-repository Git workspaces.",
        epilog="Phase 2A syntax: ws create <workspace> [--config PATH]",
    )
    commands = parser.add_subparsers(dest="command")

    create = commands.add_parser("create", help="create a detached workspace")
    create.add_argument("workspace")
    create.add_argument("--config", type=str)
    create.add_argument("--source", action="append", default=[])

    status = commands.add_parser("status", help="show workspace state")
    status.add_argument("--json", action="store_true")

    claim = commands.add_parser("claim", help="claim a repository")
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

    try:
        if args.command is None:
            parser.print_help()
            return 0
        if args.command == "create":
            paths = create_workspace(
                args.workspace,
                config_path=args.config,
                source_overrides=args.source,
            )
            print(f"Created workspace {paths.workspace}")
            return 0
        if args.command == "status":
            payload = status_workspace()
            if args.json:
                print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
            else:
                print(render_status_human(payload))
            return 0
        if args.command == "claim":
            claim_workspace(args.repo, source=args.source, target=args.target)
            print(f"Claimed repository {args.repo}")
            return 0
        print(f"ws {args.command} is not implemented in Phase 2A", file=sys.stderr)
        return 2
    except (WsError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
