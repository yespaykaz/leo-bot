#!/usr/bin/env bash
set -euo pipefail

uv sync --frozen
pnpm --filter @workspace/api-server run build
