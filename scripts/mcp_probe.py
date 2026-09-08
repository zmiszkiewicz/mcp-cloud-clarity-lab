#!/usr/bin/env python3
"""
Report what the installed `mcp` package actually offers, and whether the
Infoblox MCP Server answers.

Exists because two separate guesses about this went wrong on live runs: which
transport the hosted server speaks, and what the client library calls the
factory for it. Both are cheap to establish and expensive to guess, so
01/setup-shell runs this and prints the answer into the track log.

Non-fatal by design. It is a diagnostic, not a gate — a failure here should
tell you what is wrong, not stop the lab from starting.

Usage:
    python3 mcp_probe.py            report, and try to connect
    python3 mcp_probe.py --no-connect   report the package only
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))

import lab_config as cfg  # noqa: E402


def report_package():
    """Version, and the real contents of each transport module."""
    try:
        import importlib.metadata as meta
        print(f"mcp package version: {meta.version('mcp')}")
    except Exception as exc:                            # noqa: BLE001
        print(f"mcp package version: unknown ({exc})")

    for module_name in ("mcp.client.streamable_http", "mcp.client.sse",
                        "mcp.client.stdio"):
        try:
            module = __import__(module_name, fromlist=["*"])
        except ImportError as exc:
            print(f"  {module_name}: NOT IMPORTABLE ({exc})")
            continue
        public = [n for n in dir(module) if not n.startswith("_")
                  and callable(getattr(module, n, None))]
        print(f"  {module_name}: {', '.join(public) or '(no public callables)'}")


def report_candidates():
    """What McpFleet will actually try, in order."""
    from mcp_client import _transport_candidates

    candidates = _transport_candidates()
    if not candidates:
        print("\n❌ No usable transport factory found in the installed mcp "
              "package. The agent cannot connect to anything.")
        return False

    print(f"\nTransport candidates, in order:")
    for label, _ in candidates:
        print(f"  {label}")
    return True


async def try_connect():
    """Actually open a session and list tools. The only real answer."""
    from mcp_client import McpFleet, McpUnavailable

    print(f"\nConnecting to {cfg.MCP_SERVER_URL} ...")
    try:
        async with McpFleet(with_aws=False) as fleet:
            tools = await fleet.list_tools()
            print(f"✅ connected over {fleet.transport}")
            print(f"   {len(tools)} tool(s) exposed by the Infoblox MCP Server")
            for tool in tools[:15]:
                print(f"     {tool['name']}")
            if len(tools) > 15:
                print(f"     ... and {len(tools) - 15} more")
            print(f"\n   Pin this for future runs:  MCP_TRANSPORT="
                  f"{fleet.transport.split(' ')[0]}")
            return True
    except McpUnavailable as exc:
        print(f"❌ {exc}")
        return False
    except Exception as exc:                            # noqa: BLE001
        print(f"❌ unexpected error: {type(exc).__name__}: {exc}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Report the installed mcp package and probe the server."
    )
    parser.add_argument("--no-connect", action="store_true",
                        help="report the package only, do not connect")
    args = parser.parse_args()

    print("=" * 62)
    print("MCP probe")
    print("=" * 62)
    report_package()
    usable = report_candidates()

    if args.no_connect or not usable:
        return 0

    asyncio.run(try_connect())
    return 0


if __name__ == "__main__":
    sys.exit(main())
