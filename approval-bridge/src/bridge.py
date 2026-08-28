"""Approval bridge: polls broker intents, posts GitHub approval comments,
watches for /approve or /flag replies, and calls broker approve/reject/flag."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_TIMEOUT_SECONDS = 15
_USER_AGENT = "safe-triage-approval-bridge"


class ApprovalBridge:
    """Polls the PTC/GAL broker for pending intents and posts GitHub comments
    requesting human approval.  Watches for /approve or /flag replies from
    org members and relays the decision back to the broker."""

    def __init__(
        self,
        broker_url: str,
        owner: str,
        repo: str,
        poll_interval: int = 30,
        *,
        app_id: str,
        private_key_path: str,
        installation_id: str,
    ) -> None:
        self.broker_url = broker_url.rstrip("/")
        self.owner = owner
        self.repo = repo
        self.poll_interval = poll_interval

        from .github_auth import GitHubAppAuth  # noqa: PLC0415
        self._auth = GitHubAppAuth(app_id, private_key_path, installation_id)

        # intent_id -> {comment_id, issue_number, posted_at (ISO)}
        self._tracked: dict[str, dict[str, Any]] = {}

        # Cache org membership lookups for the lifetime of the process.
        self._org_member_cache: dict[str, bool] = {}

    @property
    def github_token(self) -> str:
        return self._auth.get_installation_token()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Run the poll-post-watch loop indefinitely."""
        logger.info(
            "Approval bridge started: broker=%s  repo=%s/%s  interval=%ds",
            self.broker_url,
            self.owner,
            self.repo,
            self.poll_interval,
        )

        while True:
            try:
                self._poll_pending_intents()
            except Exception:
                logger.exception("Error polling pending intents")

            try:
                self._check_approval_responses()
            except Exception:
                logger.exception("Error checking approval responses")

            time.sleep(self.poll_interval)

    # ------------------------------------------------------------------
    # Polling broker for new intents
    # ------------------------------------------------------------------

    def _poll_pending_intents(self) -> None:
        """GET pending intents from the broker and post approval comments
        for any we haven't seen yet."""
        url = f"{self.broker_url}/intents?status=pending"
        try:
            intents = self._broker_get(url)
        except Exception:
            logger.exception("Failed to fetch pending intents from %s", url)
            return

        for intent in intents:
            intent_id = intent.get("intent_id") or intent.get("id")
            if not intent_id:
                logger.warning("Intent missing id field: %s", intent)
                continue

            if intent_id in self._tracked:
                continue  # already posted

            issue_number = self._extract_issue_number(intent)
            if issue_number is None:
                logger.warning(
                    "Cannot determine issue number from intent %s; skipping",
                    intent_id,
                )
                continue

            comment_id = self._post_approval_comment(intent, issue_number)
            if comment_id is not None:
                self._tracked[intent_id] = {
                    "comment_id": comment_id,
                    "issue_number": issue_number,
                    "posted_at": intent.get("created_at", ""),
                }
                logger.info(
                    "Posted approval comment for intent %s on issue #%s (comment %s)",
                    intent_id,
                    issue_number,
                    comment_id,
                )

    # ------------------------------------------------------------------
    # Posting approval comments
    # ------------------------------------------------------------------

    def _post_approval_comment(
        self, intent: dict[str, Any], issue_number: int
    ) -> int | None:
        """Post a formatted approval-request comment on the GitHub issue.
        Returns the comment ID on success, None on failure."""
        intent_id = intent.get("intent_id") or intent.get("id", "unknown")

        # Extract tool call details from the materialized request.
        mat = intent.get("materialized_request", intent)
        tool = mat.get("tool", "unknown")
        op = mat.get("op", "unknown")
        args = mat.get("args", {})
        args_summary = json.dumps(args, indent=2) if isinstance(args, dict) else str(args)

        body = (
            f"**Agent Action Pending Approval**\n\n"
            f"The triage agent wants to perform the following action:\n\n"
            f"| Field | Value |\n"
            f"|-------|-------|\n"
            f"| **Tool** | `{tool}` |\n"
            f"| **Operation** | `{op}` |\n"
            f"| **Arguments** | `{args_summary}` |\n"
            f"| **Intent ID** | `{intent_id}` |\n\n"
            f"To approve this action, reply with:\n"
            f"> /approve {intent_id}\n\n"
            f"To flag this action as incorrect, reply with:\n"
            f"> /flag {intent_id}\n\n"
            f"_This approval request was generated by the PTC/GAL broker._"
        )

        url = f"{_GITHUB_API}/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments"
        payload = json.dumps({"body": body}).encode()

        try:
            result = self._github_post(url, payload)
            return result.get("id")
        except Exception:
            logger.exception(
                "Failed to post approval comment for intent %s on issue #%s",
                intent_id,
                issue_number,
            )
            return None

    # ------------------------------------------------------------------
    # Watching for approval / flag responses
    # ------------------------------------------------------------------

    def _check_approval_responses(self) -> None:
        """For each tracked intent, look for /approve or /flag replies from
        org members and relay the decision to the broker."""
        resolved: list[str] = []

        for intent_id, tracking in self._tracked.items():
            issue_number = tracking["issue_number"]
            comment_id = tracking["comment_id"]

            comments = self._fetch_comments_since(issue_number, comment_id)
            for comment in comments:
                body = comment.get("body", "")
                username = comment.get("user", {}).get("login", "")

                if not username:
                    continue

                approved = f"/approve {intent_id}" in body
                flagged = f"/flag {intent_id}" in body

                if not approved and not flagged:
                    continue

                if not self._is_org_member(username):
                    logger.info(
                        "Ignoring command from non-org-member %s on intent %s",
                        username,
                        intent_id,
                    )
                    continue

                if approved:
                    self._approve_intent(intent_id, username)
                elif flagged:
                    self._flag_intent(intent_id, username)

                resolved.append(intent_id)
                break  # first valid response wins

        for intent_id in resolved:
            del self._tracked[intent_id]

    def _fetch_comments_since(
        self, issue_number: int, since_comment_id: int
    ) -> list[dict[str, Any]]:
        """Fetch issue comments and return only those posted after the
        approval-request comment (identified by comment ID ordering)."""
        url = (
            f"{_GITHUB_API}/repos/{self.owner}/{self.repo}"
            f"/issues/{issue_number}/comments?per_page=50"
        )
        try:
            all_comments = self._github_get(url)
        except Exception:
            logger.exception(
                "Failed to fetch comments on issue #%s", issue_number
            )
            return []

        # Return comments whose ID is greater than the approval comment's.
        return [c for c in all_comments if c.get("id", 0) > since_comment_id]

    # ------------------------------------------------------------------
    # Org membership verification
    # ------------------------------------------------------------------

    def _is_org_member(self, username: str) -> bool:
        """Check whether *username* is authorized to approve actions.

        Tries two checks in order:
        1. Org membership (GET /orgs/{org}/members/{user} -> 204)
        2. Repo collaborator (GET /repos/{owner}/{repo}/collaborators/{user} -> 204)

        The GitHub App token may lack org:members read permission, so the
        collaborator check is the reliable fallback.
        """
        if username in self._org_member_cache:
            return self._org_member_cache[username]

        is_authorized = False

        # Try org membership first
        url = f"{_GITHUB_API}/orgs/{self.owner}/members/{username}"
        try:
            req = urllib.request.Request(url, headers=self._github_headers())
            with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
                is_authorized = resp.status == 204
        except urllib.error.HTTPError:
            pass
        except Exception:
            pass

        # Fall back to repo permission check (works with GitHub App tokens)
        if not is_authorized:
            url = f"{_GITHUB_API}/repos/{self.owner}/{self.repo}/collaborators/{username}/permission"
            try:
                req = urllib.request.Request(url, headers=self._github_headers())
                with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
                    data = json.load(resp)
                    perm = data.get("permission", "")
                    is_authorized = perm in ("admin", "maintain", "write")
            except urllib.error.HTTPError:
                pass
            except Exception:
                pass

        if not is_authorized:
            logger.info("User %s is not an org member or repo collaborator", username)

        self._org_member_cache[username] = is_authorized
        return is_authorized

    # ------------------------------------------------------------------
    # Broker approve / reject / flag
    # ------------------------------------------------------------------

    def _approve_intent(self, intent_id: str, approved_by: str) -> None:
        url = f"{self.broker_url}/intents/{intent_id}/approve"
        payload = json.dumps({"approved_by": approved_by}).encode()
        try:
            self._broker_post(url, payload)
            logger.info(
                "Approved intent %s (by %s)", intent_id, approved_by
            )
        except Exception:
            logger.exception("Failed to approve intent %s", intent_id)

    def _reject_intent(self, intent_id: str, rejected_by: str) -> None:
        url = f"{self.broker_url}/intents/{intent_id}/reject"
        payload = json.dumps({"rejected_by": rejected_by}).encode()
        try:
            self._broker_post(url, payload)
            logger.info(
                "Rejected intent %s (by %s)", intent_id, rejected_by
            )
        except Exception:
            logger.exception("Failed to reject intent %s", intent_id)

    def _flag_intent(self, intent_id: str, flagged_by: str) -> None:
        url = f"{self.broker_url}/intents/{intent_id}/flag"
        payload = json.dumps({"flagged_by": flagged_by}).encode()
        try:
            self._broker_post(url, payload)
            logger.info(
                "Flagged intent %s (by %s)", intent_id, flagged_by
            )
        except Exception:
            logger.exception("Failed to flag intent %s", intent_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_issue_number(intent: dict[str, Any]) -> int | None:
        """Pull the target issue number from the intent's materialized args."""
        mat = intent.get("materialized_request", intent)
        args = mat.get("args", {})
        if isinstance(args, dict):
            number = args.get("number")
            if number is not None:
                try:
                    return int(number)
                except (TypeError, ValueError):
                    pass
        return None

    # -- GitHub HTTP helpers (stdlib only) ------------------------------

    def _github_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"token {self.github_token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": _USER_AGENT,
        }

    def _github_get(self, url: str) -> Any:
        req = urllib.request.Request(url, headers=self._github_headers())
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            raise ValueError(
                f"GitHub API GET {url} failed: HTTP {exc.code} -- "
                f"{exc.read().decode()}"
            ) from exc

    def _github_post(self, url: str, data: bytes) -> Any:
        headers = self._github_headers()
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            url, data=data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            raise ValueError(
                f"GitHub API POST {url} failed: HTTP {exc.code} -- "
                f"{exc.read().decode()}"
            ) from exc

    # -- Broker HTTP helpers -------------------------------------------

    def _broker_get(self, url: str) -> Any:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": _USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            raise ValueError(
                f"Broker GET {url} failed: HTTP {exc.code} -- "
                f"{exc.read().decode()}"
            ) from exc

    def _broker_post(self, url: str, data: bytes) -> Any:
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": _USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            raise ValueError(
                f"Broker POST {url} failed: HTTP {exc.code} -- "
                f"{exc.read().decode()}"
            ) from exc


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------

def main() -> None:
    """Parse args and run the approval bridge."""
    import argparse  # noqa: PLC0415

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [approval-bridge] %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(
        description="Approval bridge for the PTC/GAL broker"
    )
    parser.add_argument(
        "--broker-url",
        default=os.environ.get("BROKER_URL", "http://broker:8080"),
    )
    parser.add_argument(
        "--owner",
        default=os.environ.get("GITHUB_OWNER", "lf-sa-demo"),
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPO", "safe-triage-demo"),
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=int(os.environ.get("POLL_INTERVAL", "30")),
    )
    parser.add_argument(
        "--app-id",
        default=os.environ.get("GITHUB_APP_ID", ""),
    )
    parser.add_argument(
        "--key-path",
        default=os.environ.get("GITHUB_APP_KEY_PATH", "/run/github-app/private-key.pem"),
    )
    parser.add_argument(
        "--installation-id",
        default=os.environ.get("GITHUB_INSTALLATION_ID", ""),
    )

    args = parser.parse_args()

    if not args.app_id or not args.installation_id:
        logger.error("GITHUB_APP_ID and GITHUB_INSTALLATION_ID are required")
        sys.exit(1)

    bridge = ApprovalBridge(
        broker_url=args.broker_url,
        owner=args.owner,
        repo=args.repo,
        poll_interval=args.poll_interval,
        app_id=args.app_id,
        private_key_path=args.key_path,
        installation_id=args.installation_id,
    )
    bridge.run()


if __name__ == "__main__":
    main()
