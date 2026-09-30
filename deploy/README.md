# 部署到服务器

本地跑：`uv run -m main`（默认 `auth.enabled: false`，行为和以前完全一样）。
放到服务器上多用户跑，按下面的顺序走一遍。

## 0. 前提：先想清楚信任边界

登录之后能用的工具里有 `world_run`（执行任意 Python）和 `bash`（执行任意 shell），
文件工具也接受绝对路径。**能登录的人 = 能以服务进程身份在这台机器上跑代码。**

所以：

- 只给可信的人开账号（朋友之间）；
- 服务用**专门的系统账号**跑，别用 root，也别和别的服务共用；
- `config.yaml` 里有全部 API key 和明文密码，工具读得到 → `chmod 600`，
  并且**别在这台机器上放任何你在意的其它东西**；
- 真要给不熟的人用，得上容器/独立机器做隔离，光靠账号密码不够。

## 1. 配置

`config.yaml`：

```yaml
server:
  max_concurrent_generations: 4   # 全服务器同时在跑的生成上限，多的排队

auth:
  enabled: true
  users:
    alice: "强密码"
    bob: "另一个强密码"
  session_ttl_days: 30
  cookie_secure: true             # 走 https 反代时必须开
```

- 改这个文件保存即生效，不用重启（账号增删同理；删掉的账号，已发的 cookie 立刻失效）。
- **不要**把 `config.yaml`、`auth_sessions.json` 提交进 git（`.gitignore` 已经排除）。
- 想在服务器上继续用本地那些会话，把它们从 `sessions/<会话名>/` 挪进
  `sessions/<用户名>/<会话名>/`——带用户名的那层就是归属，没挪的目录只在
  未启用鉴权的本地模式下可见。
- **搬完记得改归属**：`chown -R rchat:rchat /srv/rchat/sessions`。
  以 root 传上去的目录服务账号写不进去，症状是那一轮直接抛
  `PermissionError: .../state.json.<pid>.<tid>.<hex>.tmp`、流断在半路（`journalctl -u rchat` 里能看到），
  自检脚本的「目录归属」一节会直接报 FAIL 并给出这条命令。`games/`、`preset/` 同理（文件工具要写它们）。
- `cookie_secure: true` 只在「对外只有 https 入口」时开。若你还要用
  `ssh -L 25530:127.0.0.1:25530` 隧道走 http 访问，开了它浏览器就不会带 cookie、
  登录会「成功但立刻又回登录页」——那种情况保持 `false`。

## 2. 起服务（systemd）

```bash
sudo useradd -r -s /usr/sbin/nologin -d /srv/rchat rchat
sudo mkdir -p /srv/rchat && sudo chown rchat:rchat /srv/rchat
# 把项目放进去，然后装依赖（两种都行，见第 5 节）
cd /srv/rchat && sudo -u rchat uv sync                       # 有 uv，以 uv.lock 为准
# 或： python3 -m venv .venv && .venv/bin/pip install -i <国内镜像> -r deploy/requirements.txt
sudo chmod 600 /srv/rchat/config.yaml

sudo cp deploy/rchat.service /etc/systemd/system/rchat.service
sudo systemctl daemon-reload
sudo systemctl enable --now rchat
journalctl -u rchat -f
```

启动日志里会打印走了几个端点、开了哪些账号，以及**有没有启用鉴权**——没启用会明确警告。
unit 里的 `ExecStart` 直接用 `.venv/bin/python -m main`，所以 uv 装还是 pip 装都能跑。

## 3. 反代 + TLS

能拿到真证书（有自己的域名、且公网 80/443 能指向这台机器）：

- Caddy（最省事）：`sudo cp deploy/Caddyfile /etc/caddy/Caddyfile`（改域名）→ `reload`；
- nginx：`deploy/nginx.conf`，`proxy_buffering off` 那几行是关键，少一个正文就不流式了。

**拿不到真证书**（NAT 机型、80/443 被同 IP 上别的服务占着、域名不归你）：
用 `deploy/nginx-rchat-tls.conf` + `deploy/gen-selfsigned.sh`，TLS 挂在高位端口，
自签证书，浏览器提示「不安全」点继续即可，流量仍是加密的。

```bash
sudo bash deploy/gen-selfsigned.sh ab.example.com 103.40.13.67
sudo install -m 644 deploy/nginx-rchat-tls.conf /etc/nginx/sites-available/rchat
sudo ln -sf /etc/nginx/sites-available/rchat /etc/nginx/sites-enabled/rchat
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

**续期（825 天一次；自检脚本会在到期前 30 天报 FAIL）**：

```bash
sudo bash /srv/rchat/deploy/gen-selfsigned.sh ab.example.com 103.40.13.67 && sudo systemctl reload nginx
```

别想着签一张「10 年」的省事：Apple 平台对证书有效期有硬限制（825 天上限，现行 398 天规则），
超了不是多点一次「继续」的问题，而是 **iOS 直接拒绝握手**，比警告更糟。
想彻底没有警告只能「域名 + 真证书」，而 NAT 机型上**服务器自己签不了**（连不上任何 CA），
得在能上网的电脑上签好再传上来——第 5 节有说明。

**给朋友的访问说明**（自签模式，可直接转发）：

> 打开 https://ab.example.com:47060 → 浏览器提示「不安全 / 证书无效」，点「高级」→「继续前往」；
> 手机上点「显示详情」→「访问此网站」。然后输入给你的用户名和密码。
> 这个提示只说明证书不是公信机构签的，连接本身仍然是加密的。

反代只转发到 `127.0.0.1:25530`，所以 `--host 127.0.0.1` 起服务即可。
uvicorn 侧已经开了 `proxy_headers`，日志与登录限速用的是真实客户端地址。

## 4. 运维

- **重启会打断正在生成的那一轮**：用户输入在开局时就落盘了，界面上那个按钮会变成
  「继续」，点一下接着生成，不用重新打字。systemd 的 `Restart=always` 因此是安全的。
- **不要加 `--workers`**：生成状态和 world 命名空间都在进程内存里，多 worker 会让
  `/stream` 重连随机落到没有这轮事件的进程上，同一会话还可能被两个进程各持一份状态。
  单进程 asyncio 足够几十个人用，瓶颈在上游 API。
- 备份：`sessions/`（会话与世界状态）、`config.yaml`、`auth_sessions.json`（可不备）。
- 单用户生成卡死时：前端点「打断」，或 `curl -X POST .../api/sessions/<名字>/interrupt`。
- 踢掉某人：从 `auth.users` 删掉他，他的 cookie 立刻失效。
- 常驻内存约 120MB（4 核 8G 的机器绰绰有余），unit 里 `MemoryMax=1G` 是防止跑飞的
  兜底：上限设在 cgroup 内，超了只杀本服务，不会把同机器上的别的服务拖进系统级 OOM。

## 5. 中国大陆 / NAT 机型实测补充

在雨云一台 Ubuntu 24.04 NAT 机器上完整跑通过一遍，踩到的坑：

- **pypi.org 连不上**（IPv4/IPv6 都超时），`pip` 必须走国内镜像：
  `-i https://pypi.tuna.tsinghua.edu.cn/simple`（阿里/腾讯镜像也通）。apt 源同理。
- **uv 的安装脚本（astral.sh）连不上** → 用 `python3 -m venv` + `pip -r deploy/requirements.txt`
  这条路，版本与本地一致，systemd 照样能跑。apt 里 `python3-venv`/`python3-pip` 要先装。
- **模型端点出网要单独确认**：实测同一台机器上 `api.deepseek.com`、`api.kimi.com` 通，
  `openrouter.ai`、`opencode.ai` 不通。装完先 `curl -s -o /dev/null -w '%{http_code}'`
  试一遍你要用的 `api_base`；不通的端点直接从 `config.yaml` 里去掉，否则用户选中它
  只会得到网络错误。**注意端点身份是列表下标**，删中间某一项会让后面的索引前移。
- **`config.yaml` 用 `install -o <服务用户> -g <服务用户> -m 600`**，别用 `cp` 之后再
  `chmod` 的顺序搞错；服务账号读不到 `/root/...`（`/root` 是 700），要喂给它的脚本
  放 `/tmp` 或直接 `chown` 过去。
- **以 root 传上去的任何东西都要 `chown -R <服务用户>`**：实测线上踩过——用户把自己的会话
  以 root 放进 `sessions/yty/` 后，服务账号写不进去，那一轮的 `save_history` 直接
  `PermissionError`、SSE 流断在半路；自检脚本的「目录归属」那一节就是为这个加的。

- **NAT 机型的 80/443 往往不归你**：公网 IP 上这两个端口由面板或别的客户占用
  （实测 `:80` 上回答 302 的是同 IP 上别人的默认站点，`:443` 用这个域名握手直接
  `tlsv1 alert internal error`，借不到）。别浪费时间申请 Let's Encrypt：**这台机器连不上
  任何 CA**——`acme-v02.api.letsencrypt.org` 解析到 Cloudflare 的 172.65.32.248，TCP 直接不通，
  ZeroSSL / Buypass 同样 000。所以「服务器上自动签发/自动续期」在这类机器上不成立，
  真证书只能**在能上网的电脑上签好（DNS-01 最省事，不需要 80/443）再传到服务器**。
  没有域名就用高位端口 + 自签证书，面板做「公网端口 → 本机 8081」映射。
- **反代要监听 `0.0.0.0`（nginx 默认就是），别绑死 `127.0.0.1`**：面板做映射时是往
  这台机器的**内网 IP**（`hostname -I` 看到的那个，如 172.16.76.67）转发，
  绑回环的话面板映射过来会被拒。
- 自签证书的 SAN 里把**域名和公网 IP 都写上**，这样用 IP 直接访问也不会多报一次错。
- 证书有效期别超过 825 天：Apple 平台有硬限制（825 天上限 / 现行 398 天规则），
  超了 iOS 会直接拒绝握手，比「点一次继续」更糟。

