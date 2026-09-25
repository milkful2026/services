#!/usr/bin/env bash
# Starts the full local-dev backend stack (docker compose: moto, postgres,
# redis, the one-shot bootstrap container, and all ten app services) and
# verifies every service actually responds — not just that its container
# shows "Up". Safe to re-run any time; docker compose reuses/recreates
# containers as needed and bootstrap re-seeds idempotently.
#
# Usage (from anywhere):
#   services/local-dev/start-backend.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "==> Bringing up docker compose stack..."
docker compose up -d

echo "==> Waiting for postgres and moto healthchecks..."
for name in local-dev-postgres-1 local-dev-moto-1; do
  status="unknown"
  for _ in $(seq 1 30); do
    status=$(docker inspect --format '{{.State.Health.Status}}' "$name" 2>/dev/null || echo "unknown")
    [ "$status" = "healthy" ] && break
    sleep 2
  done
  echo "   $name -> $status"
done

echo "==> Waiting for bootstrap (migrations + seeds) to finish..."
docker wait local-dev-bootstrap-1 >/dev/null 2>&1 || true

echo
echo "==> Service health check:"
names=(identity-auth user inventory catalog cart pricing-offer wallet payment subscription order)
ports=(8001 8002 8000 8003 8004 8005 8006 8007 8008 8009)

all_ok=1
for i in "${!names[@]}"; do
  name="${names[$i]}"
  port="${ports[$i]}"
  # App containers have no docker healthcheck, so "Started" doesn't mean
  # "accepting connections yet" — retry a few times before calling it
  # unreachable, rather than a single-shot check racing container startup.
  code="000"
  for _ in 1 2 3 4 5; do
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://localhost:${port}/" 2>/dev/null) || true
    [ -n "$code" ] && [ "$code" != "000" ] && break
    sleep 2
  done
  if [ -z "$code" ] || [ "$code" = "000" ]; then
    printf "   %-15s :%-5s -> UNREACHABLE\n" "$name" "$port"
    all_ok=0
  else
    printf "   %-15s :%-5s -> %s (up)\n" "$name" "$port" "$code"
  fi
done

echo
if [ "$all_ok" = "1" ]; then
  echo "All services responding. Backend is ready."
  echo "See services/local-dev/README.md for how to exercise each API, and"
  echo "milkful-app/README.md's 'Running on an Android emulator' section if"
  echo "you're pointing the Flutter app at this stack from an emulator."
else
  echo "One or more services are unreachable." >&2
  echo "Investigate with: docker compose -f \"$SCRIPT_DIR/docker-compose.yml\" logs <service-name>" >&2
  exit 1
fi
