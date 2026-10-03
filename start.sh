#!/usr/bin/env bash
# One-command start: ./start.sh            (TuringDB, the real store)
#                    EDTH_REPO=memory ./start.sh   (no database, in-memory fallback)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
source .venv/bin/activate

export EDTH_REPO="${EDTH_REPO:-turing}"
if [ "$EDTH_REPO" = "turing" ]; then
  # In-memory server: writes take ~6 ms instead of ~80 ms; we reseed on every start anyway.
  turingdb stop >/dev/null 2>&1 || true
  turingdb start -demon -in-memory -ui
  trap 'turingdb stop >/dev/null 2>&1 || true' EXIT
fi

# TODO(Sasank): also start sim/simulator.py once it exists.
echo "Dashboard: http://localhost:8000   TuringDB UI: http://localhost:8080"
uvicorn backend.app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
