#!/usr/bin/env bash
set -eu
cd -- "$(dirname -- "$0")"
if command -v python3 >/dev/null 2>&1; then
  exec python3 start.py "$@"
elif command -v python >/dev/null 2>&1; then
  exec python start.py "$@"
else
  echo "Please install Python 3.11 or newer, then run this launcher again."
  exit 1
fi
