#!/usr/bin/env bash
# Run the full DevSupervisor suite. No network, no paid calls.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 -m unittest discover -s tests -t . -v "$@"
