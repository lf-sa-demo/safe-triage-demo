"""Simplified PTC airlock for the Goose agent.

Implements the 9-gate dispatch from the safe-agents RI with in-memory
stores (no DynamoDB/S3). Validates inbound requests, trust-maps senders,
stamps provenance, and forwards accepted messages to Goose's ACP endpoint.
"""

import hashlib
import hmac
import json
import logging
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from typing import Any

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

app = FastAPI(title="PTC Airlock", version="0.1.0")

GOOSE_ACP_URL = os.environ.get("GOOSE_ACP_URL", "http://localhost:3284")
AIRLOCK_TOKEN = os.environ.get("AIRLOCK_TOKEN", "")
AIRLOCK_ZONE = "airlock-demo"

# In-memory stores (no DynamoDB/S3 needed for the demo)
_dedupe_store: set[str] = set()
_drop_log: list[dict[str, Any]] = []


class ChatRequest(BaseModel):
    message: str


class DropRecord(BaseModel):
    gate: str
    reason: str
    sender_identity: str | None = None
    ts: str


# -- The nine gates --

def gate_1_verify_token(headers: dict[str, str]) -> str | None:
    """Gate 1: Transport authenticity. Constant-time token comparison."""
    token = headers.get("x-airlock-token", "")
    if not AIRLOCK_TOKEN:
        return "airlock_token_not_configured"
    if not hmac.compare_digest(token, AIRLOCK_TOKEN):
        return "authenticity_failed"
    return None


def gate_2_extract_identity(headers: dict[str, str]) -> str | None:
    """Gate 2: Extract sender identity from headers."""
    # For the demo, use the X-Sender-Identity header (set by committee members)
    # In production, this would come from OAuth/mTLS
    identity = headers.get("x-sender-identity", "").strip().lower()
    if not identity:
        return None  # will be caught as "malformed"
    return identity


def gate_3_validate_message(body: dict[str, Any]) -> str | None:
    """Gate 3: Schema check -- is the message well-formed?"""
    if "message" not in body or not isinstance(body["message"], str):
        return "malformed"
    if not body["message"].strip():
        return "malformed"
    return None


def gate_4_check_expiry() -> str | None:
    """Gate 4: Expiry. For HTTP requests, always fresh."""
    return None


# Trust map: maps sender identities to principals and sender classes
# In production, this comes from the ChannelsManifest YAML
TRUST_MAP: dict[str, dict[str, str]] = {}


def _load_trust_map():
    """Load trust map from environment or defaults."""
    global TRUST_MAP
    # Default: trust any identity as a "peer-agent" sender class
    # For the demo, committee members are mapped as external senders
    # (their messages taint the turn, showing the PTC controls)
    raw = os.environ.get("AIRLOCK_TRUST_MAP", "")
    if raw:
        try:
            TRUST_MAP = json.loads(raw)
        except json.JSONDecodeError:
            pass

    # Always trust "demo-operator" as owner (for scripted demo flows)
    TRUST_MAP.setdefault("demo-operator", {
        "principal": "goose",
        "sender_class": "owner",
    })

    # Default entry: any authenticated sender is an external user
    # (their content taints the turn -- this is the demo point)
    TRUST_MAP.setdefault("*", {
        "principal": "goose",
        "sender_class": "external",
    })


def gate_5_trust_map(identity: str) -> dict[str, str] | None:
    """Gate 5: Trust map resolution. Returns the mapped entry or None."""
    entry = TRUST_MAP.get(identity)
    if entry is None:
        entry = TRUST_MAP.get("*")  # wildcard fallback for demo
    return entry


def gate_6_dedupe(identity: str, message: str) -> bool:
    """Gate 6: Deduplication. Returns True if duplicate."""
    key = f"{identity}:{hashlib.sha256(message.encode()).hexdigest()}"
    if key in _dedupe_store:
        return True
    _dedupe_store.add(key)
    return False


def gate_7_screen() -> str | None:
    """Gate 7: Injection screen. Ships OFF for the demo."""
    return None


def gate_8_stamp(identity: str, sender_class: str) -> dict[str, Any]:
    """Gate 8: Taint stamp. Returns provenance entry."""
    label = "trusted" if sender_class in ("owner", "peer-agent") else "untrusted"
    return {
        "zone": AIRLOCK_ZONE,
        "source": f"channel:{identity}",
        "label": label,
        "ts": datetime.now(timezone.utc).isoformat(),
        "evidence": [],
    }


def _drop(gate: str, reason: str, identity: str | None = None):
    """Record a drop and raise HTTP 403."""
    record = DropRecord(
        gate=gate, reason=reason,
        sender_identity=identity,
        ts=datetime.now(timezone.utc).isoformat(),
    )
    _drop_log.append(record.model_dump())
    logger.warning("DROPPED at gate %s: %s (sender=%s)", gate, reason, identity)
    raise HTTPException(
        status_code=403,
        detail={"error": "dropped", "gate": gate, "reason": reason},
    )


@app.post("/chat")
async def chat(request: Request):
    """Inbound chat endpoint. Runs the 9-gate airlock dispatch."""
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        body = await request.json()
    except Exception:
        _drop("schema", "malformed", None)

    # Gate 1: Transport authenticity
    failure = gate_1_verify_token(headers)
    if failure:
        _drop("transport_auth", failure)

    # Gate 2: Identity extraction
    identity = gate_2_extract_identity(headers)
    if identity is None:
        _drop("identity", "malformed")

    # Gate 3: Schema check
    failure = gate_3_validate_message(body)
    if failure:
        _drop("schema", failure, identity)

    # Gate 4: Expiry check
    failure = gate_4_check_expiry()
    if failure:
        _drop("expiry", failure, identity)

    # Gate 5: Trust map
    trust_entry = gate_5_trust_map(identity)
    if trust_entry is None:
        _drop("trust_map", "unmapped", identity)

    # Gate 6: Deduplication
    if gate_6_dedupe(identity, body["message"]):
        return JSONResponse({"status": "duplicate", "message": "Request already processed"})

    # Gate 7: Injection screen (OFF)
    failure = gate_7_screen()
    if failure:
        _drop("screen", failure, identity)

    # Gate 8: Stamp provenance
    provenance = gate_8_stamp(identity, trust_entry["sender_class"])

    # Gate 9: Emit -- forward to Goose
    logger.info(
        "ACCEPTED: sender=%s class=%s label=%s",
        identity, trust_entry["sender_class"], provenance["label"],
    )

    # Forward to Goose via CLI subprocess (goose run -t "message")
    import asyncio  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    env = {
        **os.environ,
        "GOOSE_PROVIDER": os.environ.get("GOOSE_PROVIDER", "openrouter"),
        "GOOSE_MODEL": os.environ.get("GOOSE_MODEL", "z-ai/glm-5.3-flash"),
        "GOOSE_MODE": "auto",
    }

    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            [
                "goose", "run",
                "--with-extension", "stdio:python3 /app/broker_tools.py",
                "--no-session",
                "-t", body["message"],
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        goose_output = proc.stdout.strip() if proc.stdout else ""
        if proc.returncode != 0 and proc.stderr:
            logger.warning("Goose stderr: %s", proc.stderr[-500:])

        return {
            "response": goose_output or "(no response)",
            "airlock": {
                "status": "accepted",
                "sender": identity,
                "sender_class": trust_entry["sender_class"],
                "provenance_label": provenance["label"],
                "gates_passed": 9,
            },
        }
    except subprocess.TimeoutExpired:
        return JSONResponse(
            status_code=504,
            content={"error": "goose_timeout", "detail": "Goose did not respond within 120s"},
        )
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={"error": "goose_error", "detail": str(exc)},
        )


@app.get("/")
async def root():
    return {
        "service": "PTC Airlock → Goose Agent",
        "usage": "POST /chat with JSON body {\"message\": \"...\"} and headers X-Airlock-Token + X-Sender-Identity",
        "endpoints": {
            "POST /chat": "Send a message through the airlock to Goose",
            "GET /healthz": "Health check",
            "GET /stats": "Airlock gate statistics",
            "GET /drops": "Drop records",
        },
    }

@app.get("/chat")
async def chat_usage():
    return {
        "error": "Use POST, not GET",
        "usage": "curl -X POST /chat -H 'Content-Type: application/json' -H 'X-Airlock-Token: <token>' -H 'X-Sender-Identity: <name>' -d '{\"message\": \"your message\"}'",
    }

@app.get("/healthz")
async def healthz():
    return {"status": "ok", "component": "airlock"}


@app.get("/drops")
async def list_drops():
    """Debug endpoint: list all drop records."""
    return {"drops": _drop_log, "total": len(_drop_log)}


@app.get("/stats")
async def stats():
    """Airlock statistics."""
    gate_counts: dict[str, int] = {}
    for d in _drop_log:
        gate_counts[d["gate"]] = gate_counts.get(d["gate"], 0) + 1
    return {
        "total_drops": len(_drop_log),
        "dedupe_entries": len(_dedupe_store),
        "drops_by_gate": gate_counts,
    }


def main():
    import uvicorn
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [airlock] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    _load_trust_map()
    port = int(os.environ.get("AIRLOCK_PORT", "8082"))
    logger.info("Airlock starting on :%d with %d trust map entries", port, len(TRUST_MAP))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
