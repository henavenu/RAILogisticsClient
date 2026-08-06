#!/usr/bin/env bash
# wait_for.sh HOST PORT [TIMEOUT_SECONDS]
# Blocks until a TCP port accepts a connection (used to order service startup
# inside the single container — e.g. Policy Engine waits for PAP's DB init).
host="$1"; port="$2"; timeout="${3:-90}"; n=0
echo "[wait_for] waiting for ${host}:${port} (up to ${timeout}s)..."
until (exec 3<>"/dev/tcp/${host}/${port}") 2>/dev/null; do
  n=$((n+2)); sleep 2
  if [ "$n" -ge "$timeout" ]; then
    echo "[wait_for] TIMEOUT on ${host}:${port} after ${timeout}s — starting anyway"
    exit 0
  fi
done
echo "[wait_for] ${host}:${port} is up (after ${n}s)"
