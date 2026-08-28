"""GitHubTriageConnector -- brokered connector for GitHub issue triage.

Implements the safe-agents Connector protocol: the Doer calls
``execute(tool, op, args, credential)`` with the broker-resolved credential
(a GitHub App installation token). The connector never sources its own
credential and is never exposed outside the Doer.

Supported operations:

    list_issues   GET  /repos/{owner}/{repo}/issues
    read_issue    GET  /repos/{owner}/{repo}/issues/{number} + comments
    add_label     POST /repos/{owner}/{repo}/issues/{number}/labels
    post_comment  POST /repos/{owner}/{repo}/issues/{number}/comments

stdlib urllib only -- no requests/httpx dependency, matching the RI pattern.
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from typing import Any

_API_BASE = "https://api.github.com"
_TIMEOUT_SECONDS = 15
_USER_AGENT = "safe-triage-broker"


class GitHubTriageConnector:
    """Brokered GitHub connector scoped to a single owner/repo.

    The owner and repo are set at construction so callers don't need to
    repeat them. The ``args`` dict passed to ``execute`` can override
    either if needed.
    """

    def __init__(self) -> None:
        import os
        self.owner = os.environ.get("GITHUB_OWNER", "lf-sa-demo")
        self.repo = os.environ.get("GITHUB_REPO", "safe-triage-demo")

    def execute(self, tool: str, op: str, args: Any, credential: Any) -> Any:
        """Dispatch a GitHub triage operation with the broker-injected token.

        Raises ValueError for unrecognized operations or HTTP errors.
        """
        dispatch = {
            "list_issues": self._list_issues,
            "read_issue": self._read_issue,
            "add_label": self._add_label,
            "post_comment": self._post_comment,
        }
        handler = dispatch.get(op)
        if handler is None:
            raise ValueError(
                f"GitHubTriageConnector does not support op {op!r}; "
                f"supported: {sorted(dispatch)}"
            )
        return handler(args or {}, credential)

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def _list_issues(self, args: dict[str, Any], token: str) -> list[dict[str, Any]]:
        owner = args.get("owner", self.owner)
        repo = args.get("repo", self.repo)
        state = args.get("state", "open")
        per_page = args.get("per_page", 30)

        url = f"{_API_BASE}/repos/{owner}/{repo}/issues?state={state}&per_page={per_page}"
        data = self._get(url, token)
        return [
            {
                "number": issue["number"],
                "title": issue["title"],
                "labels": [lb["name"] for lb in issue.get("labels", [])],
                "created_at": issue["created_at"],
                "user": issue.get("user", {}).get("login", ""),
            }
            for issue in data
        ]

    def _read_issue(self, args: dict[str, Any], token: str) -> dict[str, Any]:
        owner = args.get("owner", self.owner)
        repo = args.get("repo", self.repo)
        number = args["number"]

        issue_url = f"{_API_BASE}/repos/{owner}/{repo}/issues/{number}"
        issue = self._get(issue_url, token)

        comments_url = f"{_API_BASE}/repos/{owner}/{repo}/issues/{number}/comments"
        comments_raw = self._get(comments_url, token)

        return {
            "number": issue["number"],
            "title": issue["title"],
            "body": issue.get("body") or "",
            "labels": [lb["name"] for lb in issue.get("labels", [])],
            "user": issue.get("user", {}).get("login", ""),
            "comments": [
                {
                    "user": c.get("user", {}).get("login", ""),
                    "body": c.get("body", ""),
                }
                for c in comments_raw
            ],
        }

    def _add_label(self, args: dict[str, Any], token: str) -> dict[str, Any]:
        owner = args.get("owner", self.owner)
        repo = args.get("repo", self.repo)
        number = args["number"]
        labels = args["labels"]

        url = f"{_API_BASE}/repos/{owner}/{repo}/issues/{number}/labels"
        payload = json.dumps({"labels": labels}).encode()
        result = self._post(url, payload, token)
        return {"labels": [lb["name"] for lb in result]}

    def _post_comment(self, args: dict[str, Any], token: str) -> dict[str, Any]:
        owner = args.get("owner", self.owner)
        repo = args.get("repo", self.repo)
        number = args["number"]
        body = args["body"]

        url = f"{_API_BASE}/repos/{owner}/{repo}/issues/{number}/comments"
        payload = json.dumps({"body": body}).encode()
        result = self._post(url, payload, token)
        return {"id": result["id"], "html_url": result["html_url"]}

    # ------------------------------------------------------------------
    # HTTP helpers (stdlib only)
    # ------------------------------------------------------------------

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": _USER_AGENT,
        }

    def _get(self, url: str, token: str) -> Any:
        request = urllib.request.Request(url, headers=self._headers(token))  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            raise ValueError(
                f"GitHub API GET {url} failed: HTTP {exc.code} -- {exc.read().decode()}"
            ) from exc

    def _post(self, url: str, data: bytes, token: str) -> Any:
        headers = self._headers(token)
        headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            raise ValueError(
                f"GitHub API POST {url} failed: HTTP {exc.code} -- {exc.read().decode()}"
            ) from exc
