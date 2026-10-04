#!/usr/bin/env bash
# Tests for the secret broker (broker/). They run in a container built from the
# image's `test` stage, so the host needs only Docker.
#   tests/run-broker.sh [pytest args...]
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
docker build -q --target test -t lettuce-secret-broker:test "$ROOT/broker" >/dev/null
docker run --rm --network none lettuce-secret-broker:test python -m pytest -q -p no:cacheprovider "$@"
