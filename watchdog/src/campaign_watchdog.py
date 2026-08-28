"""Campaign watchdog: correlates broker audit records into detection reports.

Reads the broker's audit JSONL file and looks for patterns that suggest
adversarial input (prompt injection campaigns). Follows the safe-agents
meta-alarm standard: heartbeat (I ran), content alarm (I found something),
meta-alarm (I'm broken).

Designed to run as a CronJob on OpenShift, reading the audit file from
the broker's PVC.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TAINT_REASONS = frozenset({
    "tainted external write",
    "tainted_external_write",
})

ESCALATION_DECISIONS = frozenset({
    "require_approval",
    "deny",
})


def _parse_ts(ts_str: str) -> float:
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


def read_audit_records(
    audit_path: str, window_seconds: int = 3600
) -> list[dict[str, Any]]:
    path = Path(audit_path)
    if not path.exists():
        return []

    cutoff = time.time() - window_seconds
    records = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _parse_ts(record.get("ts", ""))
        if ts >= cutoff:
            records.append(record)
    return records


def analyze(
    records: list[dict[str, Any]],
    min_taint_denials: int = 3,
    min_failed_ops: int = 5,
) -> list[dict[str, Any]]:
    """Correlate audit records into campaign reports.

    Returns a list of findings, each describing a suspected pattern.
    """
    findings: list[dict[str, Any]] = []

    taint_events = [
        r for r in records
        if r.get("reason") in TAINT_REASONS
        and r.get("decision") in ESCALATION_DECISIONS
    ]
    if len(taint_events) >= min_taint_denials:
        by_op = defaultdict(list)
        for e in taint_events:
            by_op[f"{e.get('tool')}.{e.get('op')}"].append(e)

        findings.append({
            "type": "taint_escalation_burst",
            "severity": "high",
            "count": len(taint_events),
            "operations": {op: len(events) for op, events in by_op.items()},
            "description": (
                f"{len(taint_events)} taint-triggered escalations in the analysis window. "
                "This may indicate prompt injection attempts in issue content that "
                "the agent read before attempting writes."
            ),
            "first_seen": taint_events[0].get("ts"),
            "last_seen": taint_events[-1].get("ts"),
        })

    failed_ops = [
        r for r in records
        if r.get("outcome") == "failed"
    ]
    if len(failed_ops) >= min_failed_ops:
        by_op = defaultdict(int)
        for e in failed_ops:
            by_op[f"{e.get('tool')}.{e.get('op')}"] += 1

        findings.append({
            "type": "connector_failure_burst",
            "severity": "medium",
            "count": len(failed_ops),
            "operations": dict(by_op),
            "description": (
                f"{len(failed_ops)} connector execution failures in the analysis window. "
                "Sustained failures may indicate credential issues, API rate limiting, "
                "or a misconfigured connector."
            ),
            "first_seen": failed_ops[0].get("ts"),
            "last_seen": failed_ops[-1].get("ts"),
        })

    denied = [r for r in records if r.get("decision") == "deny"]
    if len(denied) >= min_taint_denials:
        reasons = defaultdict(int)
        for e in denied:
            reasons[e.get("reason", "unknown")] += 1

        findings.append({
            "type": "denial_cluster",
            "severity": "medium",
            "count": len(denied),
            "reasons": dict(reasons),
            "description": (
                f"{len(denied)} hard denials in the analysis window. "
                "Review the reasons to determine if this is expected behavior "
                "or an indicator of policy misconfiguration."
            ),
        })

    return findings


def emit_heartbeat() -> None:
    print(json.dumps({
        "event": "heartbeat",
        "component": "campaign-watchdog",
        "ts": datetime.now(timezone.utc).isoformat(),
    }), flush=True)


def emit_content_alarm(findings: list[dict[str, Any]]) -> None:
    for finding in findings:
        print(json.dumps({
            "event": "content_alarm",
            "component": "campaign-watchdog",
            "ts": datetime.now(timezone.utc).isoformat(),
            "finding": finding,
        }), flush=True)


def emit_meta_alarm(message: str) -> None:
    print(json.dumps({
        "event": "meta_alarm",
        "component": "campaign-watchdog",
        "ts": datetime.now(timezone.utc).isoformat(),
        "message": message,
    }), file=sys.stderr, flush=True)
    sys.exit(1)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [watchdog] %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    audit_path = os.environ.get("AUDIT_PATH", "/var/lib/broker/audit.jsonl")
    window = int(os.environ.get("WINDOW_SECONDS", "3600"))
    min_taint = int(os.environ.get("MIN_TAINT_DENIALS", "3"))
    min_failures = int(os.environ.get("MIN_FAILED_OPS", "5"))

    emit_heartbeat()

    try:
        records = read_audit_records(audit_path, window)
    except Exception as exc:
        emit_meta_alarm(f"Failed to read audit trail: {exc}")

    logger.info("Read %d audit records from the last %d seconds", len(records), window)

    if not records:
        logger.info("No records in window -- nothing to correlate")
        return

    try:
        findings = analyze(records, min_taint, min_failures)
    except Exception as exc:
        emit_meta_alarm(f"Analysis failed: {exc}")

    if findings:
        logger.warning("Found %d campaign indicator(s)", len(findings))
        emit_content_alarm(findings)
    else:
        logger.info("No campaign indicators found")

    summary = {
        "total_records": len(records),
        "decisions": defaultdict(int),
        "outcomes": defaultdict(int),
    }
    for r in records:
        summary["decisions"][r.get("decision", "unknown")] += 1
        summary["outcomes"][r.get("outcome", "unknown")] += 1

    print(json.dumps({
        "event": "summary",
        "component": "campaign-watchdog",
        "ts": datetime.now(timezone.utc).isoformat(),
        "window_seconds": window,
        "summary": {
            "total_records": summary["total_records"],
            "decisions": dict(summary["decisions"]),
            "outcomes": dict(summary["outcomes"]),
            "findings_count": len(findings),
        },
    }), flush=True)


if __name__ == "__main__":
    main()
