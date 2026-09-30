#!/bin/bash
# 生成自签证书（给 deploy/nginx-rchat-tls.conf 用）。用法：bash deploy/gen-selfsigned.sh <域名或IP> [额外SAN]
#
# 没有自己的域名、或者 80/443 不归你时，申请不到 Let's Encrypt，只能自签：
# 浏览器会提示「不安全」，点继续后流量是加密的（密码不会明文过网）。
set -e
HOST="${1:?用法: $0 <域名或IP> [额外SAN,逗号分隔]}"
EXTRA="${2:-}"

SAN="DNS:$HOST,IP:127.0.0.1"
# 是 IPv4 就同时写进 SAN
if [[ "$HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  SAN="IP:$HOST,IP:127.0.0.1"
fi
[ -n "$EXTRA" ] && SAN="$SAN,$EXTRA"

openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout /etc/ssl/private/rchat.key -out /etc/ssl/certs/rchat.crt \
  -subj "/CN=$HOST" -addext "subjectAltName=$SAN"
chmod 600 /etc/ssl/private/rchat.key
openssl x509 -in /etc/ssl/certs/rchat.crt -noout -subject -ext subjectAltName
echo "证书有效 825 天，过期后重跑本脚本并 reload nginx"
