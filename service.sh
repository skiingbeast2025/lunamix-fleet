#!/usr/bin/env bash
# ==============================================================================
# HBU2 24/7 Service Shell Launcher
# Usage:
#   ./service.sh            -> Launch Interactive Settings & Controller TUI
#   ./service.sh start      -> Start service detached in background
#   ./service.sh stop       -> Stop service
#   ./service.sh restart    -> Restart service
#   ./service.sh pause      -> Toggle pause/resume
#   ./service.sh progress   -> View live terminal progress monitor
#   ./service.sh status     -> Quick status snapshot
#   ./service.sh autostart  -> Toggle or check startup on boot
#   ./service.sh bots <N>   -> Set bot count (number or 'all')
#   ./service.sh create <N> -> Batch create N accounts via proxy rotator
#   ./service.sh sync [msg] -> Commit and push all fleet files to private GitHub repo
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

python3 "$SCRIPT_DIR/service.py" "$@"
