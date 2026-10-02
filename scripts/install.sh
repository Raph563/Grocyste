#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'Python 3.10 ou ultérieur est requis sur cet hôte.' >&2
    exit 1
fi
if ! python3 -c 'import sys;sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
    printf '%s\n' 'Python 3.10 ou ultérieur est requis sur cet hôte.' >&2
    exit 1
fi
# Only the standard library is used here. Container dependencies are installed
# during docker compose build; do not install pip packages on the Grocy host.
exec python3 "$ROOT/scripts/install.py" "$@"
