#!/usr/bin/env bash
set -euo pipefail

echo "Starting predmkt-scanner MCP server (streamable HTTP) on port ${PORT:-8000}..."

exec npx -y supergateway \
  --stdio "python server.py" \
  --outputTransport streamableHttp \
  --port "${PORT:-8000}"
