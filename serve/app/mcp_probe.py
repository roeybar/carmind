"""Probe the carmind MCP service: list its tools and call assess_warning_light once."""
import argparse
import asyncio
import json
import os
import sys
import time

from app.agent import McpTools


async def probe(url: str, customer_id: str, description: str) -> dict:
    async with McpTools(url).connect() as box:
        result = await box.call("assess_warning_light", {"customer_id": customer_id, "description": description})
        return {"tools": sorted(spec["name"] for spec in box.specs), "result": result}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("CARMIND_MCP_URL") or "http://mcp:7601/mcp")
    parser.add_argument("--customer", default="C-1001")
    parser.add_argument("--description", default="red brake warning light")
    parser.add_argument("--wait", type=float, default=30)
    args = parser.parse_args(argv)

    deadline = time.monotonic() + args.wait
    while True:
        try:
            found = asyncio.run(probe(args.url, args.customer, args.description))
            break
        except Exception as exc:
            if time.monotonic() >= deadline:
                print(f"FAIL mcp probe: {type(exc).__name__}")
                return 1
            time.sleep(1)
    print(f"PASS mcp list_tools: {', '.join(found['tools'])}")
    if "status" not in found["result"]:
        print("FAIL mcp call assess_warning_light: no status in " + json.dumps(found["result"]))
        return 1
    print("PASS mcp call assess_warning_light " + json.dumps({"customer_id": args.customer, "description": args.description}))
    print(json.dumps(found["result"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
