#!/bin/zsh
# One-command native node: collector (foreground, --show works normally)
# + hourly shipper (background). Auto-restarts the collector on crash,
# blocks idle sleep, cleans up the shipper on exit.
#
# Setup once:  cp .env.example .env   # put your TW_CENTRAL_DSN in it
cd "$(dirname "$0")"

[ -f .env ] && set -a && source .env && set +a

SHIPPER_PID=""
if [ -n "$TW_CENTRAL_DSN" ]; then
  mkdir -p data
  ../.venv/bin/python aggregator.py --loop >> data/shipper.log 2>&1 &
  SHIPPER_PID=$!
  echo "shipper running (pid $SHIPPER_PID, log: node/data/shipper.log)"
else
  echo "TW_CENTRAL_DSN not set (node/.env) -- collecting locally, NOT shipping" >&2
fi

cleanup() { [ -n "$SHIPPER_PID" ] && kill "$SHIPPER_PID" 2>/dev/null; }
trap cleanup EXIT INT TERM

while true; do
  caffeinate -i ../.venv/bin/python watch.py "$@"
  code=$?
  [ $code -eq 0 ] && break        # clean exit (ctrl-c) stops the loop
  echo "watch.py exited with $code -- restarting in 10s" >&2
  sleep 10
done
