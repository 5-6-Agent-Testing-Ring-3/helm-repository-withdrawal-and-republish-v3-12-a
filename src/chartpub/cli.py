from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from chartpub.config import load_contract
from chartpub.errors import ChartpubError, RemoteConflict
from chartpub.git import Git
from chartpub.github import GitHubClient
from chartpub.lifecycle import Lifecycle
from chartpub.security import read_env_file, redact, require_token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chartpub")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "publish", "withdraw", "audit", "repair"):
        command = subcommands.add_parser(name)
        command.add_argument("--contract", type=Path, default=Path("publication-contract.json"))
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--credentials", type=Path)
        command.add_argument("--state-dir", type=Path, default=Path(".chartpub"))
        if name == "repair":
            command.add_argument("--rollback", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    secrets: dict[str, str] = {}
    try:
        contract = load_contract(args.contract)
        result: dict[str, Any] = {
            "command": args.command,
            "repository": contract.repository,
            "local": [
                "package reproducibly",
                "lint, render fixtures, install archive in isolated namespace",
                "persist non-secret recovery journal",
            ],
            "github": {
                "quarantine_release_tag": contract.bad_tag,
                "delete_tag": contract.bad_tag,
                "expected_tag": contract.expected_bad_tag_target,
                "create_release_tag": contract.replacement_tag,
                "assets": [
                    contract.asset_name(contract.replacement_version),
                    contract.asset_name(contract.replacement_version) + ".sha256",
                ],
            },
            "pages": {
                "branch": contract.pages_branch,
                "expected_tip": contract.expected_pages_tip,
                "remove_version": contract.bad_version,
                "add_version": contract.replacement_version,
                "force_with_lease_required": True,
            },
        }
        if args.credentials:
            secrets = read_env_file(args.credentials.expanduser())
            token = require_token(secrets)
            git = Git(args.contract.resolve().parent, contract.repository, token)
            lifecycle = Lifecycle(
                contract, git, GitHubClient(contract.repository, token), args.state_dir
            )
            if args.command == "plan" or args.dry_run:
                result["remote"] = lifecycle.audit()
                result["plan"] = lifecycle.plan(args.command)
                result["pages"]["force_with_lease_required"] = result["plan"][
                    "force_with_lease_required"
                ]
                result["writes"] = []
            elif args.command == "audit":
                result = lifecycle.audit()
            elif args.command == "withdraw":
                result = lifecycle.withdraw()
            elif args.command == "publish":
                result = lifecycle.publish()
            elif args.rollback:
                result = lifecycle.rollback()
            else:
                lifecycle.withdraw()
                result = lifecycle.publish()
        elif args.command != "plan" and not args.dry_run:
            raise ChartpubError("--credentials is required for authenticated operations")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1 if args.command == "audit" and result.get("problems") else 0
    except (ChartpubError, OSError, ValueError) as exc:
        print("chartpub: " + redact(str(exc), secrets), file=sys.stderr)
        return 3 if isinstance(exc, RemoteConflict) else 2
