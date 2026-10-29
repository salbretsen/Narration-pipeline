# Shared environment for the Audiobook Studio launchers (sourced, not executed).
# shellcheck shell=bash
STUDIO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export AUDIOBOOK_STUDIO_HOME="${AUDIOBOOK_STUDIO_HOME:-$STUDIO_ROOT}"
export HF_HOME="${HF_HOME:-$AUDIOBOOK_STUDIO_HOME/cache/huggingface}"
export HF_HUB_DISABLE_TELEMETRY=1
export DO_NOT_TRACK=1
export TOKENIZERS_PARALLELISM=false
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1
# shellcheck disable=SC2034  # used by the scripts that source this file
STUDIO_PY="$STUDIO_ROOT/venv/bin/python"
# shellcheck disable=SC2034
STUDIO_APP="$STUDIO_ROOT/app"
if [ ! -x "$STUDIO_PY" ]; then
  echo "Audiobook Studio is not installed correctly (missing $STUDIO_PY). Re-run install_audiobook_studio_claude.sh." >&2
  exit 1
fi
# Offline mode for Hugging Face when model downloads are disabled in Settings.
STUDIO_SETTINGS="$AUDIOBOOK_STUDIO_HOME/config/settings.json"
if [ -f "$STUDIO_SETTINGS" ]; then
  rc=0
  "$STUDIO_PY" -c 'import json,sys
try:
    allow = json.load(open(sys.argv[1])).get("models", {}).get("allow_downloads", True)
except Exception:
    allow = True
sys.exit(0 if allow else 3)' "$STUDIO_SETTINGS" 2>/dev/null || rc=$?
  if [ "$rc" = 3 ]; then export HF_HUB_OFFLINE=1; fi
fi
studio_port() {
  "$STUDIO_PY" -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get("server",{}).get("port",7861))
except Exception:
    print(7861)' "$AUDIOBOOK_STUDIO_HOME/config/settings.json"
}
pid_alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
