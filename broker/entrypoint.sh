#!/bin/bash
set -euo pipefail

# Generate a fresh GitHub App installation token from the mounted PEM key.
# Tokens last 1 hour; the pod restarts refresh them.
if [[ -f /run/github-app/private-key.pem ]]; then
    echo "Generating GitHub App installation token..."
    python3 -c "
from src.github_auth import GitHubAppAuth
import os
auth = GitHubAppAuth(
    app_id=os.environ['GITHUB_APP_ID'],
    private_key_path='/run/github-app/private-key.pem',
    installation_id=os.environ['GITHUB_INSTALLATION_ID'],
)
token = auth.get_installation_token()
os.makedirs('/tmp/connector-secrets', exist_ok=True)
with open('/tmp/connector-secrets/github-app-token', 'w') as f:
    f.write(token)
print('Token generated successfully.')
"
else
    echo "WARNING: No GitHub App PEM found at /run/github-app/private-key.pem"
fi

echo "Starting token refresh loop (every 50 minutes)..."
python3 -c "
import time, os, sys
sys.path.insert(0, '/app')
from src.github_auth import GitHubAppAuth
auth = GitHubAppAuth(
    app_id=os.environ['GITHUB_APP_ID'],
    private_key_path='/run/github-app/private-key.pem',
    installation_id=os.environ['GITHUB_INSTALLATION_ID'],
)
while True:
    try:
        token = auth.get_installation_token()
        tmp = '/tmp/connector-secrets/.rotate-token'
        with open(tmp, 'w') as f:
            f.write(token)
        os.replace(tmp, '/tmp/connector-secrets/github-app-token')
        print(f'[token-refresh] Token refreshed at {time.strftime(\"%H:%M:%S UTC\", time.gmtime())}', flush=True)
    except Exception as e:
        print(f'[token-refresh] ERROR: {e}', file=sys.stderr, flush=True)
    time.sleep(3000)  # 50 minutes
" &
TOKEN_PID=$!

echo "Starting broker HTTP server on :8080..."
python3 -m safe_agents.broker.prototype.broker_server &
BROKER_PID=$!

echo "Starting MCP HTTP gateway on :8081..."
python3 -m src.gateway_http &
GATEWAY_PID=$!

# Wait for any to exit
wait -n $BROKER_PID $GATEWAY_PID $TOKEN_PID
EXIT_CODE=$?
echo "A process exited with code $EXIT_CODE, shutting down..."
kill $BROKER_PID $GATEWAY_PID $TOKEN_PID 2>/dev/null || true
exit $EXIT_CODE
