"""TriageAgent -- GitHub issue triage via broker MCP gateway.

Reads open issues, classifies them (bug/feature/question/docs), assigns
priority (high/medium/low), proposes labels, and drafts an acknowledging
comment.  All tool calls go through the broker's MCP gateway; there are
no local tools.
"""

from __future__ import annotations

import logging

from fipsagents.baseagent import BaseAgent, StepResult

log = logging.getLogger(__name__)


class TriageAgent(BaseAgent):
    """Triages GitHub issues filed by Linux Foundation committee members."""

    async def setup(self) -> None:
        await super().setup()
        self._processed_issues: set[int] = set()
        self._pending_issues: list[int] = []
        self._fetched_list = False

    async def step(self) -> StepResult:
        # Phase 1: fetch the list of open issues (once).
        if not self._fetched_list:
            self._fetched_list = True
            self.add_message(
                "user",
                (
                    "List all open issues using the github_list_issues tool. "
                    "Return the issue numbers so we can triage each one."
                ),
            )
            response = await self.call_model()
            response = await self.run_tool_calls(response)

            # Parse issue numbers from the model's response for tracking.
            # The model may return them in various formats; we extract
            # what we can and fall back to letting the model drive.
            self._extract_issue_numbers(response.content or "")

            if not self._pending_issues:
                # The model may have found no issues, or we couldn't parse
                # the numbers.  Either way, let the model summarize.
                return StepResult.done(
                    result=response.content or "No open issues found."
                )
            return StepResult.continue_()

        # Phase 2: triage the next unprocessed issue.
        if self._pending_issues:
            issue_num = self._pending_issues.pop(0)
            if issue_num in self._processed_issues:
                return StepResult.continue_()
            self._processed_issues.add(issue_num)

            self.add_message(
                "user",
                (
                    f"Triage issue #{issue_num}.\n\n"
                    "1. Call github_read_issue to read the full issue.\n"
                    "2. Classify it as: bug, feature, question, or docs.\n"
                    "3. Assign priority: high, medium, or low.\n"
                    "4. Call github_add_label with the category and "
                    "priority labels (e.g. 'bug', 'priority/high').\n"
                    "5. Draft a brief, professional response and call "
                    "github_post_comment to post it.\n"
                    "6. Summarize what you did."
                ),
            )
            response = await self.call_model()
            response = await self.run_tool_calls(response)

            log.info("Triaged issue #%d: %s", issue_num, response.content)
            return StepResult.continue_()

        # Phase 3: all issues processed -- summarize and finish.
        self.add_message(
            "user",
            "All issues have been triaged. Provide a brief summary of "
            "what was done.",
        )
        response = await self.call_model()
        return StepResult.done(result=response.content)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_issue_numbers(self, text: str) -> None:
        """Best-effort extraction of issue numbers from model output."""
        import re

        numbers = [int(n) for n in re.findall(r"#(\d+)", text)]
        if not numbers:
            # Try bare integers on their own lines or in lists.
            numbers = [int(n) for n in re.findall(r"\b(\d+)\b", text) if int(n) < 100_000]
        self._pending_issues = [n for n in numbers if n not in self._processed_issues]
        log.info("Extracted issue numbers: %s", self._pending_issues)


# ---------------------------------------------------------------------------
# HTTP server (default)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from fipsagents.baseagent import load_config
    from fipsagents.server import OpenAIChatServer

    config = load_config("agent.yaml")
    server = OpenAIChatServer(
        agent_class=TriageAgent,
        config_path="agent.yaml",
        title=config.agent.name,
        version=config.agent.version,
    )
    server.run(host=config.server.host, port=config.server.port)
