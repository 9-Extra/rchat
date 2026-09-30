#!/bin/bash
# rchat 部署自检：服务 / 反代 / 鉴权 / 归属 / 模型端点出网 / 资源。
# 用法：bash deploy/healthcheck.sh            （默认 /srv/rchat、25530、8081、rchat）
#       APP=/opt/rchat SVC_USER=rchat bash deploy/healthcheck.sh
set -u
APP="${APP:-/srv/rchat}"
SVC_USER="${SVC_USER:-rchat}"
LOCAL_PORT="${APP_PORT:-25530}"
TLS_PORT="${TLS_PORT:-8081}"
PY="$APP/.venv/bin/python"
FAIL=0

ok()   { echo "  ok    $*"; }
bad()  { echo "  FAIL  $*"; FAIL=$((FAIL+1)); }
info() { echo "  --    $*"; }
say()  { echo; echo "== $*"; }

say "服务"
systemctl is-active --quiet rchat && ok "rchat.service active" || bad "rchat.service 不是 active（journalctl -u rchat -n 50）"
systemctl is-enabled --quiet rchat && ok "开机自启已开" || bad "开机自启未开（systemctl enable rchat）"
ss -tln 2>/dev/null | grep -q ":$LOCAL_PORT " && ok "监听 $LOCAL_PORT" || bad "没有监听 $LOCAL_PORT"

say "应用接口"
me=$(curl -s --max-time 8 "http://127.0.0.1:$LOCAL_PORT/api/me")
case "$me" in
  *'"auth_enabled":true'*) ok "/api/me 正常，鉴权已开" ;;
  *'"auth_enabled":false'*) bad "/api/me 说鉴权没开——这台机器对外时必须开" ;;
  *) bad "/api/me 没返回预期内容: ${me:-（空）}" ;;
esac
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "http://127.0.0.1:$LOCAL_PORT/api/sessions")
[ "$code" = 401 ] && ok "未登录访问受保护接口 -> 401" || bad "未登录访问 /api/sessions 得到 $code（期望 401）"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "http://127.0.0.1:$LOCAL_PORT/")
[ "$code" = 200 ] && ok "首页 -> 200" || bad "首页 -> $code"

say "反代"
if systemctl is-active --quiet nginx; then
  ok "nginx active"
  code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 8 "https://127.0.0.1:$TLS_PORT/api/me")
  [ "$code" = 200 ] && ok "https://127.0.0.1:$TLS_PORT 反代正常" || bad "反代 $TLS_PORT -> $code"
  exp=$(date -d "+30 days" +%s 2>/dev/null || echo 0)
  end=$(openssl x509 -in /etc/ssl/certs/rchat.crt -noout -enddate 2>/dev/null | cut -d= -f2)
  if [ -n "$end" ]; then
    endts=$(date -d "$end" +%s 2>/dev/null || echo 0)
    if [ "$endts" -gt "$exp" ]; then ok "证书有效至 $end"; else bad "证书 $end 快过期或已过期，重跑 deploy/gen-selfsigned.sh"; fi
  else
    info "没找到 /etc/ssl/certs/rchat.crt（用真证书的话忽略这条）"
  fi
else
  info "nginx 没在跑（用别的反代或隧道的话忽略）"
fi

say "目录归属（把本地会话/角色卡以 root 传上来的话，服务账号会写不进去）"
for sub in sessions games preset; do
  d="$APP/$sub"
  if [ ! -d "$d" ]; then
    bad "找不到 $d"
    continue
  fi
  foreign=$(find "$d" -not -user "$SVC_USER" 2>/dev/null | head -5)
  if [ -z "$foreign" ]; then
    ok "$sub/ 全部属于 $SVC_USER"
  else
    bad "$sub/ 下有不是 $SVC_USER 所有的文件（落盘会 Permission denied）：$(echo "$foreign" | tr '\n' ' ')"
    info "修复：chown -R $SVC_USER:$SVC_USER $d"
  fi
done

say "模型端点出网（两端点身份＝列表下标；同一 api_base 只测一次）"
if [ -x "$PY" ]; then
  bases=$("$PY" - <<'EOF' 2>/dev/null
import yaml
try:
    cfg = yaml.safe_load(open("/srv/rchat/config.yaml", encoding="utf-8")) or {}
except Exception:
    raise SystemExit
seen = []
for i, ep in enumerate(cfg.get("endpoints") or []):
    b = (ep or {}).get("api_base")
    if b and b not in seen:
        seen.append(b)
        print(f"{i}\t{ep.get('display_name')}\t{b}")
EOF
)
  if [ -n "$bases" ]; then
    while IFS=$'\t' read -r idx name base; do
      code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 "$base/v1/models" 2>/dev/null)
      case "$code" in
        000|"") bad "[$idx] $name 连不上（$base）" ;;
        401|403) ok "[$idx] $name 可达（$code＝正常，需要 key）" ;;
        *) ok "[$idx] $name 可达（$code）" ;;
      esac
    done <<< "$bases"
  else
    info "config.yaml 里没解析到端点"
  fi
else
  bad "找不到 $PY"
fi

say "资源"
free -m | awk 'NR<=2'
df -h "$APP" | tail -1
rss=$(systemctl show -p MemoryCurrent --value rchat 2>/dev/null)
[ -n "${rss:-}" ] && [ "$rss" != "[not set]" ] && info "rchat 常驻 $((rss/1024/1024)) MB"
info "配置里 max_concurrent_generations = $("$PY" -c "import yaml,sys;print((yaml.safe_load(open(sys.argv[1],encoding='utf-8')).get('server') or {}).get('max_concurrent_generations'))" "$APP/config.yaml" 2>/dev/null)"

echo
[ "$FAIL" -eq 0 ] && echo "全部通过" || echo "有 $FAIL 项需要处理"
exit "$FAIL"
