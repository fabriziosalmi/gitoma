#!/usr/bin/env bash
# Backward-compat shim. The real control-plane is ``deploy/minimac`` now.
# Keep this around so muscle memory + anything that wired the old name
# (docs, aliases) keeps working.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$HERE/minimac" deploy "$@"
