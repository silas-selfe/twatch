#!/bin/zsh
# Long-running collector: prevents idle sleep, auto-restarts on crash.
cd "$(dirname "$0")"
while true; do
  caffeinate -i .venv/bin/python watch.py "$@"
  code=$?
  [ $code -eq 0 ] && break        # clean exit (ctrl-c) stops the loop
  echo "watch.py exited with $code -- restarting in 10s" >&2
  sleep 10
done
