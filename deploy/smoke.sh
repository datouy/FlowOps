#!/usr/bin/env bash
# 云策 YunCe 上线冒烟：不碰业务数据，只验证关键面是否活着
#
#   bash smoke.sh                                  # 本机直连 5409
#   BASE=https://ops.example.com TOKEN=xxx bash smoke.sh
#
# 退出码 0 = 全通过

set -uo pipefail

BASE="${BASE:-http://127.0.0.1:5409}"
TOKEN="${TOKEN:-${YUNCE_ACCESS_TOKEN:-}}"
FAIL=0

pass() { printf '\033[1;32m✅ %-42s %s\033[0m\n' "$1" "$2"; }
fail() { printf '\033[1;31m❌ %-42s %s\033[0m\n' "$1" "$2"; FAIL=$((FAIL + 1)); }

# 1. 健康检查（不校验令牌）
code=$(curl -s -o /tmp/yunce_health.$$ -w '%{http_code}' --max-time 5 "$BASE/health")
if [[ "$code" == "200" ]] && grep -q '"status":"ok"' /tmp/yunce_health.$$; then
  pass "/health" "200 ok"
else
  fail "/health" "HTTP $code（后端没起来或端口不对）"
fi
rm -f /tmp/yunce_health.$$

# 2. 前端首页能不能拿到（Flask 托管的 index.html）
code=$(curl -s -o /tmp/yunce_index.$$ -w '%{http_code}' --max-time 5 "$BASE/")
if [[ "$code" == "200" ]] && grep -qi '<div id="app">' /tmp/yunce_index.$$; then
  pass "/ (前端首页)" "200 有 index.html"
else
  fail "/ (前端首页)" "HTTP $code（dist 产物没放到后端目录？）"
fi

# 3. 前端静态资源（构建产物的 assets 必须能被 /assets/ 路由命中）
asset=$(grep -oE '/assets/[A-Za-z0-9._-]+\.js' /tmp/yunce_index.$$ | head -n1 || true)
rm -f /tmp/yunce_index.$$
if [[ -n "${asset:-}" ]]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE$asset")
  [[ "$code" == "200" ]] && pass "静态资源 $asset" "200" || fail "静态资源 $asset" "HTTP $code"
else
  echo "   [跳过] 首页里没解析到 /assets/*.js"
fi

# 4. 令牌鉴权：带令牌必须 200，不带必须 401
if [[ -n "$TOKEN" ]]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -H "X-YunCe-Token: $TOKEN" "$BASE/todo/summary")
  [[ "$code" == "200" ]] && pass "带令牌访问业务接口" "200" || fail "带令牌访问业务接口" "HTTP $code"

  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$BASE/todo/summary")
  [[ "$code" == "401" ]] && pass "无令牌访问业务接口" "401 已拦截" || fail "无令牌访问业务接口" "HTTP $code（应该是 401）"
else
  echo "   [跳过] 没给 TOKEN，跳过鉴权校验"
fi

# 5. 关键业务面（账号 / 发布队列 / 私信 / 线索）
for path in /dm/sync/status /dm/priority-leads /todo/summary /dm/analytics; do
  if [[ -n "$TOKEN" ]]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -H "X-YunCe-Token: $TOKEN" "$BASE$path")
  else
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$BASE$path")
  fi
  [[ "$code" == "200" ]] && pass "$path" "200" || fail "$path" "HTTP $code"
done

echo
if [[ $FAIL -eq 0 ]]; then
  echo "冒烟通过。"
  exit 0
fi
echo "失败 $FAIL 项，排查：docker compose logs -f yunce 或 journalctl -u yunce -n 200 --no-pager"
exit 1
