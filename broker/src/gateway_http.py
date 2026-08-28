"""gateway_http.py -- serve the broker's MCP gateway over streamable-HTTP.

The safe-agents ``GatewaySurface`` is transport-agnostic. The RI's
``gateway/__main__.py`` serves it over stdio for a wrapped harness; this
module serves it over HTTP so the fips-agents triage agent can connect to
``http://broker-gateway:8081/mcp`` via FastMCP's StreamableHTTPTransport.

Two integration strategies, attempted in order:

1. **Low-level MCP Server over streamable-HTTP** -- uses the ``mcp`` SDK's
   ``Server`` (the same one ``gateway/server.py`` wires to stdio) and
   ``StreamableHTTPServer`` to serve the standard MCP JSON-RPC protocol.

2. **FastAPI REST fallback** -- if the MCP SDK's HTTP server support is
   unavailable or incompatible, exposes simple REST endpoints at
   ``/mcp/tools`` and ``/mcp/call`` that the agent can call directly.

Configuration is read from the environment, matching the broker:

    BROKER_MANIFEST       path to manifest.yaml
    BROKER_STORE          store arm (memory/dynamo/sqlite)
    BROKER_HMAC_KEY       grant-store HMAC key
    BROKER_SECRETS        secrets arm
    BROKER_SECRETS_DIR    secrets mount directory
    GATEWAY_PORT          port to listen on (default 8081)
"""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 8081


def _build_surface():
    """Construct the GatewaySurface from the broker runtime.

    All diagnostic output goes to stderr so it doesn't interfere with
    a potential stdio transport.
    """
    import contextlib  # noqa: PLC0415

    from safe_agents.broker.api import build_runtime  # noqa: PLC0415
    from safe_agents.broker.gateway.surface import GatewaySurface  # noqa: PLC0415
    from safe_agents.broker.prototype.boot_config import load_named_manifest  # noqa: PLC0415

    manifest = load_named_manifest()

    with contextlib.redirect_stdout(sys.stderr):
        runtime, _sink = build_runtime(manifest)
        surface = GatewaySurface(runtime, server_name="safe-triage-broker")
        tool_count = len(surface.tools())
        agent_id = manifest.principal.agentId if manifest.principal else "?"
        logger.info(
            "MCP gateway ready: %d tool(s) for %s", tool_count, agent_id
        )

    return surface, runtime


def _try_mcp_streamable_http(surface, port: int) -> bool:
    """Attempt to serve via the mcp SDK's streamable-HTTP transport.

    Returns True if the server was started (blocking), False if the SDK
    doesn't support this transport shape.
    """
    try:
        from safe_agents.broker.gateway.server import build_server  # noqa: PLC0415
    except ImportError:
        return False

    server = build_server(surface)

    # The mcp SDK (>=1.20) bundles a streamable-HTTP ASGI app via
    # ``mcp.server.streamable_http``. If available, wrap it with uvicorn.
    try:
        from mcp.server.streamable_http import StreamableHTTPServer  # noqa: PLC0415
        import uvicorn  # noqa: PLC0415

        http_server = StreamableHTTPServer(server)
        app = http_server.app

        logger.info("Starting MCP streamable-HTTP gateway on :%d/mcp", port)
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")  # noqa: S104
        return True
    except (ImportError, AttributeError):
        logger.debug(
            "StreamableHTTPServer not available in this mcp SDK version; "
            "falling back to FastAPI REST gateway"
        )
        return False


def _run_fastapi_gateway(surface, port: int, runtime=None) -> None:
    """Serve the gateway as a FastAPI app with REST endpoints.

    This is the fallback when the MCP SDK's streamable-HTTP server is
    not available. It exposes:

        GET  /mcp/tools          -- the tool list
        POST /mcp/call           -- execute a tool call
        GET  /intents             -- list pending intents
        POST /intents/{id}/approve -- approve a held intent
        POST /intents/{id}/reject  -- reject a held intent
        POST /intents/{id}/flag    -- flag a held intent
        GET  /healthz             -- health check
    """
    from fastapi import FastAPI, HTTPException  # noqa: PLC0415
    from pydantic import BaseModel  # noqa: PLC0415
    import uvicorn  # noqa: PLC0415

    app = FastAPI(title="safe-triage-broker gateway", version="0.1.0")

    class ToolCallRequest(BaseModel):
        name: str
        arguments: dict | None = None

    class ToolCallResponse(BaseModel):
        ok: bool
        text: str
        decision_kind: str
        structured: object | None = None
        reason: str | None = None
        intent_id: str | None = None

    class ToolInfo(BaseModel):
        name: str
        description: str
        input_schema: dict

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/mcp/tools", response_model=list[ToolInfo])
    async def list_tools():
        tools = surface.tools()
        return [
            ToolInfo(
                name=t.wire_name,
                description=t.description,
                input_schema=t.input_schema,
            )
            for t in tools
        ]

    @app.post("/mcp/call", response_model=ToolCallResponse)
    async def call_tool(req: ToolCallRequest):
        result = surface.call(req.name, req.arguments)
        return ToolCallResponse(
            ok=result.ok,
            text=result.text,
            decision_kind=result.decision_kind,
            structured=result.structured,
            reason=result.reason,
            intent_id=result.intent_id,
        )

    # -- Intent management endpoints (for the approval bridge) --

    if runtime is not None:
        class ApproveRequest(BaseModel):
            approved_by: str

        class RejectRequest(BaseModel):
            rejected_by: str

        class FlagRequest(BaseModel):
            flagged_by: str

        @app.get("/intents")
        async def list_intents(status: str = "pending"):
            import sqlite3  # noqa: PLC0415
            import json as _json  # noqa: PLC0415
            db_path = os.environ.get("BROKER_SQLITE_PATH", "/var/lib/broker/broker.db")
            db = sqlite3.connect(db_path)
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT pk, item FROM items WHERE pk LIKE 'INTENT%'"
            ).fetchall()
            db.close()

            results = []
            for row in rows:
                pk = row["pk"]
                intent_id = pk.replace("INTENT#", "")
                view = runtime.describe_intent(intent_id)
                if view is None:
                    continue
                if status and view.status != status:
                    continue

                # Extract the raw args from the stored intent for the bridge
                stored_args = {}
                try:
                    raw = _json.loads(row["item"])
                    data = raw.get("data", raw)
                    if isinstance(data, str):
                        data = _json.loads(data)
                    mat = data.get("materializedRequest", data)
                    stored_args = mat.get("args", {})
                except Exception:
                    pass

                results.append({
                    "intent_id": view.intent_id,
                    "status": view.status,
                    "tool": view.tool,
                    "op": view.op,
                    "args_digest": view.args_digest,
                    "rendered_for_human": view.rendered_for_human,
                    "expiry": view.expiry,
                    "ts": view.ts,
                    "agent_id": view.agent_id,
                    "approved_by": view.approved_by,
                    "materialized_request": {
                        "tool": view.tool,
                        "op": view.op,
                        "args": stored_args,
                    },
                })
            return results

        @app.post("/intents/{intent_id}/approve")
        async def approve_intent(intent_id: str, req: ApproveRequest):
            try:
                result = runtime.approve_intent(intent_id, req.approved_by)
                return {
                    "status": "approved",
                    "intent_id": intent_id,
                    "result": str(result) if result else None,
                }
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @app.post("/intents/{intent_id}/reject")
        async def reject_intent(intent_id: str, req: RejectRequest):
            try:
                result = runtime.reject_intent(intent_id, req.rejected_by)
                return {"status": "rejected", "intent_id": intent_id}
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @app.post("/intents/{intent_id}/flag")
        async def flag_intent(intent_id: str, req: FlagRequest):
            try:
                result = runtime.flag_intent(intent_id, req.flagged_by)
                return {"status": "flagged", "intent_id": intent_id}
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        logger.info("Intent management endpoints enabled")

    logger.info("Starting FastAPI REST gateway on :%d", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")  # noqa: S104


def main() -> None:
    """Entry point: build the surface and serve it."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [gateway] %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    port = int(os.environ.get("GATEWAY_PORT", str(_DEFAULT_PORT)))
    surface, runtime = _build_surface()

    try:
        if not _try_mcp_streamable_http(surface, port):
            _run_fastapi_gateway(surface, port, runtime=runtime)
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
