#!/usr/bin/env bash
# FlowOps split 模式只读验收。不会创建任务、领取任务或终止进程。
#
# 在 deploy/ 目录运行：
#   bash verify_split.sh
#
# 可覆盖阈值：
#   MAX_PENDING=10 MAX_UNCERTAIN=0 MAX_QUEUE_AGE=600 \
#   MAX_QUEUE_P95=120 MAX_P95=900 bash verify_split.sh
#
# 退出码：0 通过；非 0 失败。

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

BASE_URL="${BASE_URL:-http://127.0.0.1:5409}"
HOURS="${HOURS:-24}"
REQUIRE_EXECUTOR="${REQUIRE_EXECUTOR:-1}"
OUT_DIR="${OUT_DIR:-out/acceptance}"
STAMP="$(date +%Y%m%d-%H%M%S)"
AUDIT_FILE="$OUT_DIR/split-audit-$STAMP.json"
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.split.yml)

pass() { printf '[PASS] %-38s %s\n' "$1" "$2"; }
fail() { printf '[FAIL] %-38s %s\n' "$1" "$2" >&2; exit 1; }
info() { printf '[INFO] %-38s %s\n' "$1" "$2"; }

command -v docker >/dev/null 2>&1 || fail "docker" "docker 命令不存在"
docker compose version >/dev/null 2>&1 || fail "docker compose" "Compose v2 不可用"
command -v curl >/dev/null 2>&1 || fail "curl" "curl 命令不存在"

mkdir -p "$OUT_DIR"

"${COMPOSE[@]}" config --quiet || fail "compose config" "split 编排解析失败"
pass "compose config" "docker-compose.yml + docker-compose.split.yml 可解析"

RUNNING="$("${COMPOSE[@]}" ps --status running --services)"
grep -qx yunce <<<"$RUNNING" || fail "web container" "yunce 未处于 running"
grep -qx yunce-executor <<<"$RUNNING" || fail "executor container" "yunce-executor 未处于 running"
pass "containers" "yunce / yunce-executor 均在运行"

WEB_ROLE="$(${COMPOSE[@]} exec -T yunce printenv FLOWOPS_PROCESS_ROLE | tr -d '\r')"
EXECUTOR_ROLE="$(${COMPOSE[@]} exec -T yunce-executor printenv FLOWOPS_PROCESS_ROLE | tr -d '\r')"
[[ "$WEB_ROLE" == "web" ]] || fail "web role" "期望 web，实际 ${WEB_ROLE:-empty}"
[[ "$EXECUTOR_ROLE" == "executor" ]] || fail "executor role" "期望 executor，实际 ${EXECUTOR_ROLE:-empty}"
pass "process roles" "web / executor"

WEB_IMAGE="$(docker inspect -f '{{.Image}}' yunce)"
EXECUTOR_IMAGE="$(docker inspect -f '{{.Image}}' yunce-executor)"
[[ "$WEB_IMAGE" == "$EXECUTOR_IMAGE" ]] || fail "image identity" "两个容器不是同一镜像"
pass "image identity" "${WEB_IMAGE:0:19}"

READY_CODE="$(curl -sS -o "$OUT_DIR/ready-$STAMP.json" -w '%{http_code}' \
  --max-time 10 "$BASE_URL/health/ready")"
[[ "$READY_CODE" == "200" ]] || fail "health/ready" "HTTP $READY_CODE"
pass "health/ready" "HTTP 200"

AUDIT_ARGS=(python production_audit.py --base-dir /app --hours "$HOURS" --format json)
if [[ "$REQUIRE_EXECUTOR" == "1" ]]; then
  AUDIT_ARGS+=(--require-executor)
fi
if [[ -n "${MAX_PENDING:-}" ]]; then
  AUDIT_ARGS+=(--max-pending "$MAX_PENDING")
fi
if [[ -n "${MAX_UNCERTAIN:-}" ]]; then
  AUDIT_ARGS+=(--max-uncertain "$MAX_UNCERTAIN")
fi
if [[ -n "${MAX_QUEUE_AGE:-}" ]]; then
  AUDIT_ARGS+=(--max-queue-age "$MAX_QUEUE_AGE")
fi
if [[ -n "${MAX_QUEUE_P95:-}" ]]; then
  AUDIT_ARGS+=(--max-queue-p95 "$MAX_QUEUE_P95")
fi
if [[ -n "${MAX_P95:-}" ]]; then
  AUDIT_ARGS+=(--max-p95 "$MAX_P95")
fi

if "${COMPOSE[@]}" exec -T yunce "${AUDIT_ARGS[@]}" >"$AUDIT_FILE"; then
  AUDIT_RC=0
else
  AUDIT_RC=$?
fi
[[ "$AUDIT_RC" -eq 0 ]] || fail "production audit" "gate FAIL，报告：$AUDIT_FILE"
pass "production audit" "gate PASS，报告：$AUDIT_FILE"

info "web log dir" "$(${COMPOSE[@]} exec -T yunce printenv FLOWOPS_LOG_DIR | tr -d '\r')"
info "executor log dir" "$(${COMPOSE[@]} exec -T yunce-executor printenv FLOWOPS_LOG_DIR | tr -d '\r')"
info "next step" "真实发布、强杀、分片崩溃仍须按 production-acceptance-2026-11.md 单独演练"
echo "split 只读验收通过。"
