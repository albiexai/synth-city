#!/usr/bin/env python3
"""Write or check the deployed Mail tool schemas the backend compares session catalogs with (#1146).

The backend cannot import the MCP shim at runtime (importing it registers tools), so it reads this committed snapshot of
the public tools' input schemas. A drift test fails whenever a tool or a parameter changes without regenerating it.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "backend/app/services/tool_catalog_schemas.json"


def render() -> str:
    spec = importlib.util.spec_from_file_location("tool_catalog_shim", ROOT / "backend/mcp_shim/agent_mail_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tools = asyncio.run(module.mcp.list_tools())
    schemas = {tool.name: tool.inputSchema for tool in tools if tool.name.startswith("synth_city_")}
    return json.dumps(schemas, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="Rewrite the snapshot from the shim.")
    mode.add_argument("--check", action="store_true", help="Fail when the snapshot differs from the shim.")
    args = parser.parse_args()
    expected = render()
    if args.check:
        if not SNAPSHOT.exists() or SNAPSHOT.read_text() != expected:
            print("The tool schema snapshot differs from the shim. Run scripts/tool-catalog-schemas.py --write.")
            return 1
        print("The tool schema snapshot matches the shim.")
        return 0
    SNAPSHOT.write_text(expected)
    print("Wrote the tool schema snapshot.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
