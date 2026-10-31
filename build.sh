#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -Eeuo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ -f "$ROOT/kernel.py" ]]; then
    exec python3 "$ROOT/kernel.py" "$@"
fi
exec python3 "$ROOT/scripts/kernel.py" "$@"
