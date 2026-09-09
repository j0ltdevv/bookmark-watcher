#!/usr/bin/env bash
# start-all.sh — démarre le proxy NVIDIA NIM (port 8089) et Siftly (port 3000).
# Usage: ./start-all.sh          démarre ce qui n'est pas déjà lancé
#        ./start-all.sh stop     arrête les deux
#        ./start-all.sh status   affiche l'état
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

port_up() { ss -tln 2>/dev/null | grep -q ":$1 "; }

start_proxy() {
  if port_up 8089; then echo "[proxy] déjà en cours (port 8089)"; return 0; fi
  echo "[proxy] démarrage sur http://127.0.0.1:8089"
  (cd "$ROOT/nvidia-claude-proxy" \
    && nohup "$ROOT/nvidia-claude-proxy/venv/bin/python" main.py \
    > "$LOG_DIR/proxy.log" 2>&1 &)
  for _ in $(seq 1 20); do port_up 8089 && break; sleep 0.5; done
  port_up 8089 && echo "[proxy] OK" || { echo "[proxy] ÉCHEC — voir logs/proxy.log"; return 1; }
}

start_siftly() {
  if port_up 3000; then echo "[siftly] déjà en cours (port 3000)"; return 0; fi
  echo "[siftly] démarrage sur http://localhost:3000"
  (cd "$ROOT/siftly" && nohup npm run dev > "$LOG_DIR/siftly.log" 2>&1 &)
  for _ in $(seq 1 60); do port_up 3000 && break; sleep 1; done
  port_up 3000 && echo "[siftly] OK" || { echo "[siftly] ÉCHEC — voir logs/siftly.log"; return 1; }
}

stop_all() {
  for port in 3000 8089; do
    pid=$(ss -tlnp 2>/dev/null | grep ":$port " | grep -oP 'pid=\K[0-9]+' | head -1)
    if [ -n "$pid" ]; then
      echo "arrêt port $port (pid $pid)"
      kill "$pid" 2>/dev/null
    fi
  done
}

status_all() {
  port_up 8089 && echo "proxy   : UP   (http://127.0.0.1:8089)" || echo "proxy   : DOWN"
  port_up 3000 && echo "siftly  : UP   (http://localhost:3000)" || echo "siftly  : DOWN"
}

case "${1:-start}" in
  start)  start_proxy; start_siftly; status_all ;;
  stop)   stop_all ;;
  status) status_all ;;
  *) echo "Usage: $0 [start|stop|status]"; exit 1 ;;
esac
