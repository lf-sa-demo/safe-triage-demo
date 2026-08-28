"""FastMCP server wrapping the PTC/GAL broker's HTTP API as MCP tools.

Goose discovers these tools at startup via stdio transport. Each tool
calls POST /call on the broker, which mediates all GitHub API access
through the PTC/GAL controls layer.
"""

import json
import os
import urllib.request
import urllib.error

from fastmcp import FastMCP

mcp = FastMCP("broker-tools", instructions="""
These tools connect to the PTC/GAL broker, which mediates all GitHub API access.
The broker enforces provenance tracking (PTC) and autonomy lifecycle (GAL) controls.
If a tool returns 'pending_approval', the action requires human approval before executing.
""")

BROKER_URL = os.environ.get("BROKER_URL", "http://broker:8080")


def _broker_call(tool: str, op: str, args: dict) -> str:
    payload = json.dumps({"tool": tool, "op": op, "args": args}).encode()
    req = urllib.request.Request(
        f"{BROKER_URL}/call",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        return json.dumps({"error": f"Broker returned HTTP {exc.code}: {exc.read().decode()}"})

    if data.get("decision_kind") == "allow":
        return json.dumps(data.get("result", {}), indent=2)
    elif data.get("decision_kind") == "require_approval":
        intent_id = data.get("intent_id", "unknown")
        return json.dumps({
            "status": "pending_approval",
            "intent_id": intent_id,
            "reason": data.get("reason", ""),
            "message": f"This action requires human approval. Intent ID: {intent_id}. "
                       "An approval comment will be posted on the issue.",
        })
    else:
        return json.dumps({
            "status": data.get("decision_kind", "denied"),
            "reason": data.get("reason", "Unknown"),
        })


@mcp.tool()
def github_list_issues(state: str = "open", per_page: int = 30) -> str:
    """List open GitHub issues on the safe-triage-demo repository.

    Args:
        state: Filter by issue state (open, closed, all). Defaults to open.
        per_page: Number of issues to return. Defaults to 30.
    """
    return _broker_call("github", "list_issues", {"state": state, "per_page": per_page})


@mcp.tool()
def github_read_issue(number: int) -> str:
    """Read a specific GitHub issue with its full body and comments.

    Args:
        number: The issue number to read.
    """
    return _broker_call("github", "read_issue", {"number": number})


@mcp.tool()
def github_add_label(number: int, labels: list[str]) -> str:
    """Add classification and priority labels to a GitHub issue.

    Args:
        number: The issue number to label.
        labels: List of label names to add (e.g. ["bug", "priority/high"]).
    """
    return _broker_call("github", "add_label", {"number": number, "labels": labels})


@mcp.tool()
def github_post_comment(number: int, body: str) -> str:
    """Post a triage response comment on a GitHub issue.

    Args:
        number: The issue number to comment on.
        body: The comment text to post.
    """
    return _broker_call("github", "post_comment", {"number": number, "body": body})
