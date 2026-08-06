#!/usr/bin/env bash
cd "$(dirname "$0")/.."
echo "=== docker ps ==="
docker ps --filter name=rai-smoke --format '{{.Status}}  {{.Ports}}'
echo "=== WSL-side UI probe (localhost:8080) ==="
code=""
for i in $(seq 1 25); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/ui --max-time 3 2>/dev/null)
  if [ "$code" = "200" ] || [ "$code" = "304" ]; then echo "UI up HTTP $code after $((i*2))s"; break; fi
  sleep 2
done
echo "final_code=$code"
echo "=== nginx routes ==="
docker exec rai-smoke sh -lc 'grep -nE "location |proxy_pass" /etc/nginx/nginx.conf 2>/dev/null' 2>/dev/null | head -40 || echo "(exec failed)"
echo "=== last 18 log lines ==="
docker logs rai-smoke 2>&1 | tail -18
