#!/bin/sh
# Container entrypoint: first run creates a starter admin (password printed once in the container log, must be
# changed at first sign-in), then serves on all interfaces. State lives in /data (mount a volume there).
set -eu
cd /opt/studio
if [ -z "$(python -m studio auth list 2>/dev/null)" ]; then
    echo "First run: creating admin user 'admin' with a generated starter password."
    python -m studio auth add-user admin --role admin --generate --temporary
fi
exec python -m studio serve --host 0.0.0.0 --port 8080 --allow-remote
