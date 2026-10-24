"""`homestead` command line.

homestead serve                   # stdio, for Claude Desktop / Claude Code
homestead serve --transport http  # streamable HTTP (Cloud Run); OAuth if HOMESTEAD_PUBLIC_URL is set
homestead approvals               # list pending requests
homestead approve <id>            # a human approves and executes a request
homestead deny <id> [--reason]
homestead audit [-n 20]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from homestead.approvals import ApprovalError
from homestead.auth import OAuthConfig
from homestead.config import load_config
from homestead.server import build_app, state_dir_from_env


def _app(oauth: OAuthConfig | None = None):
    cfg_path = os.environ.get("HOMESTEAD_CONFIG", "config/household.yaml")
    return build_app(load_config(cfg_path), state_dir_from_env(), oauth=oauth)


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="homestead")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run the MCP server")
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))

    sub.add_parser("approvals", help="list pending approval requests")
    approve = sub.add_parser("approve", help="approve and execute a request")
    approve.add_argument("request_id")
    deny = sub.add_parser("deny", help="deny a request")
    deny.add_argument("request_id")
    deny.add_argument("--reason", default="")
    audit = sub.add_parser("audit", help="show recent audit entries")
    audit.add_argument("-n", type=int, default=20)

    args = parser.parse_args(argv)

    if args.cmd == "serve":
        if args.transport == "stdio":
            _app().server.run("stdio")
            return 0
        oauth = OAuthConfig.from_env(os.environ)
        if oauth is None:
            print(
                "homestead: HOMESTEAD_PUBLIC_URL is not set, so there is no OAuth. "
                "Only run this way behind something that authenticates (Cloud Run IAM).",
                file=sys.stderr,
            )
        _app(oauth).server.run(
            "streamable-http", host=args.host, port=args.port, stateless_http=True
        )
        return 0

    app = _app()

    if args.cmd == "approvals":
        _print([vars(r) for r in app.approvals.list("pending")])
        return 0

    if args.cmd == "audit":
        _print(app.audit.tail(args.n))
        return 0

    try:
        if args.cmd == "approve":
            pending = app.approvals.get(args.request_id)
            tier = (
                app.registry.tools[pending.tool].tier.name
                if pending.tool in app.registry.tools
                else "?"
            )
            req = app.approvals.approve_and_execute(args.request_id, app.registry.execute_approved)
            app.audit.record(
                tool=req.tool,
                tier=tier,
                actor="human-cli",
                outcome=req.status,
                params=req.params,
                request_id=req.id,
                detail=req.error,
            )
            _print(vars(req))
            return 0 if req.status == "executed" else 1
        if args.cmd == "deny":
            req = app.approvals.deny(args.request_id, args.reason)
            app.audit.record(
                tool=req.tool,
                tier="APPROVAL",
                actor="human-cli",
                outcome="denied",
                params=req.params,
                request_id=req.id,
                detail=args.reason or None,
            )
            _print(vars(req))
            return 0
    except ApprovalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
