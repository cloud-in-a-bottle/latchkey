#!/bin/bash
# Run the app server in a single process. Everything else — the latchkey
# gateway and the virtual display stack (Xvfb + matchbox + x11vnc) for
# browser-based logins — is started on demand by the server and stopped
# again when idle, keeping the idle memory footprint minimal.
#
# bash stays as PID 1 (instead of exec'ing the server) so orphans reparented
# here — e.g. a login browser whose gateway died — get reaped, not zombified.
set -euo pipefail

/app/.venv/bin/python -m server.web.main &
child=$!

forward_term() {
    kill -TERM "$child" 2>/dev/null
    wait "$child"
    exit $?
}
trap forward_term TERM INT

wait "$child"
