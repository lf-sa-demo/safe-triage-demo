"""Local tools that call the broker's HTTP API for GitHub triage operations.

Each tool wraps a POST /call to the broker, which mediates all GitHub API
access through the PTC/GAL controls layer. The broker enforces taint
propagation, grant-level gating, and audit on every call.
"""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.error

from fipsagents.baseagent import tool

_BROKER_URL = os.environ.get("BROKER_URL", "http://broker:8080")


def _broker_call(tool_name: str, op: str, args: dict | None = None) -> str:
    payload = json.dumps({"tool": tool_name, "op": op, "args": args or {}}).encode()
    req = urllib.request.Request(
        f"{_BROKER_URL}/call",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        return json.dumps({"error": f"Broker returned HTTP {exc.code}: {body}"})

    if data.get("decision_kind") == "allow":
        return json.dumps(data.get("result", {}), indent=2)
    elif data.get("decision_kind") == "require_approval":
        intent_id = data.get("intent_id", "unknown")
        return json.dumps({
            "status": "pending_approval",
            "intent_id": intent_id,
            "reason": data.get("reason", ""),
            "message": f"This action requires human approval. Intent ID: {intent_id}. "
                       f"A comment will be posted on the issue for a committee member to approve.",
        })
    else:
        return json.dumps({
            "status": data.get("decision_kind", "denied"),
            "reason": data.get("reason", "Unknown"),
        })


@tool(description="List open GitHub issues on the demo repository", visibility="llm_only")
async def github_list_issues(state: str = "open", per_page: int = 30) -> str:
    """List open issues from the safe-triage-demo repository.

    Args:
        state: Filter by issue state (open, closed, all). Defaults to open.
        per_page: Number of issues to return. Defaults to 30.
    """
    return _broker_call("github", "list_issues", {"state": state, "per_page": per_page})


@tool(description="Read a specific GitHub issue with its comments", visibility="llm_only")
async def github_read_issue(number: int) -> str:
    """Read the full content of a GitHub issue including comments.

    Args:
        number: The issue number to read.
    """
    return _broker_call("github", "read_issue", {"number": number})


@tool(description="Add labels to a GitHub issue", visibility="llm_only")
async def github_add_label(number: int, labels: list[str]) -> str:
    """Add classification and priority labels to an issue.

    Args:
        number: The issue number to label.
        labels: List of label names to add (e.g. ["bug", "priority/high"]).
    """
    return _broker_call("github", "add_label", {"number": number, "labels": labels})


@tool(description="Post a comment on a GitHub issue", visibility="llm_only")
async def github_post_comment(number: int, body: str) -> str:
    """Post a triage response comment on an issue.

    Args:
        number: The issue number to comment on.
        body: The comment text to post.
    """
    return _broker_call("github", "post_comment", {"number": number, "body": body})
