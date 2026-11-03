"""`python -m agentkit.sidecar`: serve the sidecar on its unix socket."""

import argparse
import sys

from agentkit.sidecar import server


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agentkit.sidecar",
        description="Run Claude Code and Codex headless behind a unix-socket HTTP server.",
        epilog=server.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", nargs="?", choices=["serve"], default="serve")
    parser.parse_args(argv)
    return server.serve()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
