#!/usr/bin/env bash
# Usage:
#   ./run.sh                         start the GUI on 0.0.0.0:8095
#   ./run.sh --port 9000 --user admin --password secret
#   ./run.sh mock --port 2775 --max-binds 10     standalone mock SMSC
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
if [ "${1:-}" = "mock" ]; then
  shift
  exec .venv/bin/python -m smppclient.mock_smsc "$@"
fi
exec .venv/bin/python -m smppclient.server "$@"
