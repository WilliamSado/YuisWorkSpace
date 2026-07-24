# YuisWorkSpace

面向自建服务器的轻量 CI：cron 定时或网页/Bot 手动触发构建，保存构建历史与日志，并向 Telegram 和 QQ 推送结果。默认串行执行，适合耗费大量 CPU、内存和磁盘 I/O 的 Android ROM 构建。

## 功能

- 多构建任务、标准 5 段 cron、时区、超时和启停配置
- 密码登录的响应式网页面板，状态自动刷新、日志查看、手动构建和取消
- SQLite/WAL 持久化；服务异常重启后将未完成任务标记为 `interrupted`
- Telegram 长轮询 Bot；QQ 通过 OneBot 11（NapCat/Lagrange.One 等实现）接入
- Android ROM 分阶段流水线：构建、发布签名、上传分别显示状态
- 用户/群聊白名单、QQ webhook 签名、HttpOnly 会话 Cookie
- systemd + Nginx 生产部署文件，亦提供 Docker Compose

## 快速启动

需要 Python 3.11+。Android 构建推荐使用 systemd，让进程直接访问源码、编译缓存和宿主机工具链。

```bash
cd ci-panel
cp config.example.toml config.toml
cp .env.example .env
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
set -a; source .env; set +a
.venv/bin/python -m app
```

打开 `http://服务器IP:8080`。正式使用前务必修改 `.env` 中的密码和随机密钥，可用 `openssl rand -hex 32` 生成密钥。构建命令拥有服务器账号的权限，因此 `config.toml` 只能由管理员修改。

设备与构建任务不放在主程序仓库。克隆独立的设备配置仓库，并在 `.env` 中设置它的路径：

```bash
git clone <你的设备配置仓库> /opt/avium-ci-devices
export CI_DEVICES_CONFIG=/opt/avium-ci-devices/devices.toml
```

独立仓库中 `devices.toml` 的任务示例：

```toml
[[devices]]
id = "avium-ishtar"
name = "AviumUI · ishtar"
cron = "0 3 * * *" # 每天 03:00
workdir = "/srv/AviumUI-16.2"
command = "source build/envsetup.sh && lunch lineage_ishtar-bp4a-userdebug && m bacon"
timeout_minutes = 720
enabled = true
```

修改主配置或设备配置后重启服务。日志和数据库默认存放在 `ci-panel/data`，可用 `CI_DATA_DIR` 调整。设备配置仓库可以独立审核、版本控制和回滚，但其中的 `command` 仍是受信任的服务器命令，只允许管理员提交。

## Telegram Bot

1. 在 Telegram 联系 `@BotFather` 创建 Bot，取得 token。
2. 把 token 写入 `.env` 的 `TELEGRAM_BOT_TOKEN`。
3. 将自己的数字 chat ID 填入 `config.toml` 的 `allowed_chat_ids`，并设置 `enabled = true`。
4. 重启服务。Bot 使用长轮询，无需公网 webhook。

支持命令：`/status`、`/jobs`、`/wen ishtar`、`/build fuxi`、`/cancel 123`、`/help`。
Bot 只处理以 `/` 开头的 Telegram 消息；群内触发和取消构建仅允许群管理员。

构建、签名和上传全部成功后，可通过任务的 `release_command` 自动发布带 Banner 的
Telegram Update。发布目标由 `.env` 的 `TELEGRAM_RELEASE_CHAT` 控制，Bot 必须是
目标频道的管理员并拥有“发布消息”权限。LineageOS Banner 会按
`LINEAGE_CUSTOM_MODEL` 和 codename 动态替换设备文字。

## 独立发布签名

Android ROM 可以配置成三个独立阶段：

1. `command` 执行 `m -j32 dist` 并生成 unsigned target-files；
2. `sign_command` 调用 `scripts/sign_dist.sh`，签名 target-files 并生成 OTA；
3. `post_success_command` 将最终签名 OTA 上传到发布平台。

签名脚本从 dist 目录的 `LINEAGE_CUSTOM_MODEL` 元数据读取发布机型名，并接受
`nightly`、`weekly` 或 `monthly` 作为 release type。最终文件名格式为
`LineageOS-${LINEAGE_CUSTOM_MODEL}-${YYYYMMDD}-${release_type}-signed.zip`，只保留
年月日，不包含具体时分。

面板和 `/jobs` 会显示“构建中 → 签名中 → 上传中”。私钥必须放在主程序和
设备配置仓库之外，并通过 `ANDROID_SIGNING_KEYS_DIR` 指向密钥目录。签名脚本要求
`releasekey`、`platform`、`shared`、`media`、`networkstack`、`sdk_sandbox`、
`bluetooth` 和 `nfc` 八组 `.pk8` / `.x509.pem` 文件。不要把私钥提交到 Git。

## QQ Bot（OneBot 11）

先部署一个 OneBot 11 实现，例如 NapCat，并配置：

- HTTP API 地址为 `config.toml` 的 `onebot_api_url`；
- 反向 HTTP 事件上报地址为 `https://ci.example.com/api/onebot/webhook`；
- OneBot API token 与 `.env` 的 `QQ_ONEBOT_ACCESS_TOKEN` 相同；
- webhook secret 与 `.env` 的 `QQ_WEBHOOK_SECRET` 相同（实现需发送 `X-Signature: sha1=...`）；
- 在 `allowed_user_ids` / `allowed_group_ids` 添加允许控制 CI 的 QQ 号或群号。

QQ 中使用 `status`、`jobs`、`build avium-ishtar`、`cancel 123`、`help`。如果 OneBot 实现不支持签名，应让 webhook 仅监听内网，并通过反向代理增加访问控制；不要把无鉴权端点直接暴露到公网。

## systemd 部署

将本目录放到 `/opt/avium-ci`，创建专用 `builder` 用户并确保它能读写源码和编译输出，然后安装服务：

```bash
sudo install -d -o builder -g builder /var/lib/avium-ci
sudo cp deploy/avium-ci.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now avium-ci
sudo journalctl -u avium-ci -f
```

部署 HTTPS 时按实际域名和证书修改 `deploy/nginx.conf`。若源码目录受 systemd 的 `ProtectSystem=full` 限制，请将其明确加入服务的 `ReadWritePaths=`；示例中的 `/srv` 通常可写，但具体取决于系统布局。

## 备份与运维

- 备份 `CI_DATA_DIR` 即可保留数据库和所有构建日志。
- 构建命令输出会持续写盘，建议用定时任务清理过旧的 `*.log`，并同步删除相应数据库记录或整体保留数据库记录。
- 当前为单实例、单 worker 设计；不要同时启动多个服务副本共享一个数据目录。
- 更换 `CI_SECRET_KEY` 会使现有网页登录会话失效，这是预期行为。
