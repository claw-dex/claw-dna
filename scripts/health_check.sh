#!/bin/bash
set -uo pipefail
# health_check.sh — Multi-service health check with retry logic
# Usage: ./health_check.sh [--retries N] [--delay SECONDS]
# Returns exit 0 if all checks pass, exit 1 if any fail after retries

RETRIES=3
DELAY=2
STREAMLIT_URL="http://localhost:8081/app"
CADDY_URL="http://localhost:8080"
FAILED=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --retries) RETRIES="$2"; shift 2 ;;
    --delay) DELAY="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

check_streamlit_health() {
  local attempt=0
  local resp=""

  while [ $attempt -lt $RETRIES ]; do
    attempt=$((attempt + 1))
    resp=$(curl -s --max-time 5 "${STREAMLIT_URL}/_stcore/health" 2>/dev/null || echo "")
    if [ "$resp" = "ok" ]; then
      printf "  %-30s %s\n" "Streamlit health (8081)" "OK"
      return 0
    fi
    [ $attempt -lt $RETRIES ] && sleep "$DELAY"
  done

  printf "  %-30s %s\n" "Streamlit health (8081)" "FAIL (got: '$resp' after $RETRIES attempts)"
  FAILED=1
  return 1
}

check_caddy_health() {
  local resp
  resp=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "${CADDY_URL}/app/" 2>/dev/null || echo "000")
  if [ "$resp" = "200" ] || [ "$resp" = "301" ] || [ "$resp" = "302" ] || [ "$resp" = "401" ]; then
    printf "  %-30s %s\n" "Caddy gateway (8080)" "OK (HTTP $resp)"
    return 0
  fi
  printf "  %-30s %s\n" "Caddy gateway (8080)" "FAIL (HTTP $resp)"
  FAILED=1
  return 1
}

echo "=== Agent Health Check ==="
echo "Timestamp: $(date -Iseconds)"
echo ""

# Check if Streamlit process is running
STREAMLIT_PID=$(pgrep -f "streamlit run server.py" 2>/dev/null | head -1)
if [ -n "$STREAMLIT_PID" ]; then
  echo "Streamlit PID: $STREAMLIT_PID"
else
  echo "Streamlit: NOT RUNNING"
  FAILED=1
fi

# Check Caddy process
CADDY_PID=$(pgrep -f "caddy run" 2>/dev/null | head -1)
if [ -n "$CADDY_PID" ]; then
  echo "Caddy PID: $CADDY_PID"
else
  echo "Caddy: NOT RUNNING"
  FAILED=1
fi

# Check port bindings
for port in 8080 8081; do
  if netstat -tlnp 2>/dev/null | grep -q ":${port}" || ss -tlnp 2>/dev/null | grep -q ":${port}"; then
    echo "Port ${port}: BOUND"
  else
    echo "Port ${port}: NOT BOUND"
    FAILED=1
  fi
done

check_app_render() {
  echo ""
  echo "--- App Render Check ---"
  if timeout 45 uv run python /agent/scripts/app_check.py 2>&1; then
    printf "  %-30s %s\n" "App render (AppTest)" "OK"
  else
    printf "  %-30s %s\n" "App render (AppTest)" "FAIL"
    FAILED=1
  fi
}

echo ""
echo "--- Health Checks ---"
check_streamlit_health
check_caddy_health
check_app_render

echo ""
if [ $FAILED -eq 0 ]; then
  echo "Result: ALL CHECKS PASSED"
  exit 0
else
  echo "Result: SOME CHECKS FAILED"
  exit 1
fi
