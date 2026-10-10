"""Start an MCP server command on stdio and check that it answers initialize and tools/list.

    python scripts/mcp_stdio_smoke.py [--min-tools N] -- <command> [args...]

Used by CI for the Claude Desktop bundle: the command is started the way a client starts it,
with placeholder credentials (no Intervals.icu request is made for these two calls). Exits 1
when the server does not answer within the timeout or lists fewer tools than expected.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from typing import IO, Any

TIMEOUT_S = 180  # the first start of a bundle installs its dependencies


def _pump(stream: IO[str], sink: queue.Queue[str]) -> None:
    """Move the server's stdout lines into a queue so that reads can time out."""
    for line in stream:
        sink.put(line)


def _handshake(proc: subprocess.Popen[str]) -> tuple[dict[str, Any], list[Any]]:
    """Send initialize and tools/list; return the server info and the tools."""
    assert proc.stdin is not None and proc.stdout is not None
    stdin = proc.stdin
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=_pump, args=(proc.stdout, lines), daemon=True).start()

    def send(message: dict[str, Any]) -> None:
        try:
            stdin.write(json.dumps(message) + "\n")
            stdin.flush()
        except BrokenPipeError:
            raise SystemExit(f"error: the server exited with status {proc.wait()} before reading its input") from None

    def answer(request_id: int) -> dict[str, Any]:
        deadline = time.monotonic() + TIMEOUT_S
        while True:
            try:
                message = json.loads(lines.get(timeout=1))
            except queue.Empty:
                if proc.poll() is not None and lines.empty():
                    raise SystemExit(f"error: the server exited with status {proc.returncode} before answering") from None
                if time.monotonic() > deadline:
                    raise SystemExit(f"error: no answer to request {request_id} within {TIMEOUT_S} s") from None
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    raise SystemExit(f"error: request {request_id} failed: {message['error']}")
                result: dict[str, Any] = message["result"]
                return result

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "ci-smoke", "version": "0"}}})
    info = answer(1).get("serverInfo", {})
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    return info, answer(2).get("tools", [])


def main(argv: list[str] | None = None) -> int:
    """Command line entry point; exit status 1 when the server does not answer as expected."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--min-tools", type=int, default=1, help="fail with fewer tools than this")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="server command, after --")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no server command given")

    env = dict(os.environ, API_KEY="test", ATHLETE_ID="i1", MCP_PERMISSIONS="read", MCP_TRANSPORT="stdio")
    # The command comes from the CI workflow.
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, text=True) as proc:
        try:
            info, tools = _handshake(proc)
        finally:
            assert proc.stdin is not None
            proc.stdin.close()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
    print(f"server {info.get('name')!r} answered initialize and listed {len(tools)} tools")
    if len(tools) < args.min_tools:
        print(f"error: expected at least {args.min_tools} tools")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
