#!/bin/sh
# Orchestrator image entrypoint: composable dev wrappers around "$@".
#
#   default            exec "$@" unchanged (CMD = rolemesh; compose
#                      `command:` / `docker compose run ... <cmd>` win).
#   DEBUGPY=1          wrap the command in debugpy listening on
#                      0.0.0.0:5678 (port published by compose). Expects
#                      the command to start with a console script or a
#                      script path (e.g. "rolemesh"); do not combine
#                      with a `python -m ...` command.
#   ROLEMESH_RELOAD=1  wrap the (possibly debugpy-wrapped) command in
#                      watchfiles, restarting it whenever /app/src
#                      changes — pairs with the compose bind mount
#                      ../../src:/app/src for hot reload. Applies ONLY
#                      to the long-running orchestrator command
#                      ("rolemesh"): one-off CLI runs via
#                      `docker compose run --rm orchestrator <cmd>`
#                      inherit the service environment, and wrapping a
#                      command that is supposed to exit in a file
#                      watcher would hang it forever.
set -e

reload_eligible=0
[ "$1" = "rolemesh" ] && reload_eligible=1

if [ "${DEBUGPY:-0}" = "1" ]; then
    first="$1"
    shift
    resolved="$(command -v "$first" || true)"
    [ -n "$resolved" ] || resolved="$first"
    set -- python -m debugpy --listen 0.0.0.0:5678 "$resolved" "$@"
fi

if [ "$reload_eligible" = "1" ] && [ "${ROLEMESH_RELOAD:-0}" = "1" ]; then
    # watchfiles takes the target as a single shell-command string.
    # "$*" is safe here: our commands are simple argv lists without
    # spaces inside arguments.
    exec watchfiles --filter python "$*" /app/src
fi

exec "$@"
