#!/usr/bin/env bash
# Start the rai-trust:local container with the Groq key injected at runtime.
set -e
cd "$(dirname "$0")/.."
GK=$(grep -hm1 '^GROQ_API_KEY=' .env sdk/examples/mybank_clean/.env 2>/dev/null | head -1 | cut -d= -f2-)
docker rm -f rai-smoke >/dev/null 2>&1 || true
docker run -d --name rai-smoke -p 8080:8080 -e GROQ_API_KEY="$GK" rai-trust:local >/dev/null
echo "container started"
if [ -n "$GK" ]; then echo "groq_key_present=yes"; else echo "groq_key_present=NO"; fi
