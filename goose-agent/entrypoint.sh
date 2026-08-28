#!/bin/bash
set -euo pipefail

# The airlock handles everything: validates inbound requests through the
# 9-gate dispatch, then runs `goose run -t "message"` for each accepted
# request. No separate ACP server needed.

echo "Starting PTC Airlock on :${AIRLOCK_PORT:-8082}..."
exec python3 /app/airlock.py
