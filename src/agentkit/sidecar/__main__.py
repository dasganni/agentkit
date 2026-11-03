"""`python -m agentkit.sidecar`: serve the sidecar on its unix socket, or sign in in a terminal."""

import argparse
import sys

from agentkit.sidecar import login, runner, server


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agentkit.sidecar",
        description="Run Claude Code and Codex headless behind a unix-socket HTTP server.",
        epilog=server.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", nargs="?", choices=["serve", "login"], default="serve")
    parser.add_argument(
        "target",
        nargs="*",
        help="login: [user-key] [claude|codex]; the backend alone signs in the single user",
    )
    args = parser.parse_args(argv)
    if args.command == "serve":
        return server.serve()
    target: list[str] = args.target
    if len(target) == 1 and target[0] in runner.BACKENDS:
        target = ["", target[0]]
    user, backend = (target + ["", "claude"][len(target) :])[:2]
    return login.terminal_login(runner.Config.from_env(), user, backend)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
