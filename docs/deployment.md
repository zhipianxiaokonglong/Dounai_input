# 部署指南

## 1. 部署形态

| 形态 | 适用场景 | 关键点 |
| --- | --- | --- |
| 本机单用户 | 桌面演示、开发、个人使用 | 默认回环绑定，无 Token |
| 单机 + 反向代理 | 小团队内网/公网单实例 | HTTPS、Token、正确的 `public_base_url` |
| Docker | 可重复部署 | 数据卷权限、模型可选 |
| 多副本横向扩展 | 当前**不支持** | SQLite/本地媒体是单实例语义 |

当前版本没有用户账户、配额、限流或多租户隔离。不要把它直接当作公共 SaaS 网关。

## 2. 本机部署

```bash
python -m venv .venv
source .venv/bin/activate
pip install .
dounai-input serve
```

数据默认在当前工作目录的 `data/`：

```text
data/
├── dounai.sqlite3
├── dounai.sqlite3-wal
├── dounai.sqlite3-shm
├── media/
├── models/
└── plugins/
```

建议使用专用系统账户运行，设置：

```bash
export DOUNAI_DATA_DIR="/var/lib/dounai-input"
export DOUNAI_PUBLIC_BASE_URL="https://dounai.example.com"
export DOUNAI_API_TOKEN="$(dounai-input token)"
```

## 3. Docker

### 3.1 构建与运行

```bash
docker build -t dounai-input:0.1.1 .
docker run --rm -p 127.0.0.1:8000:8000 \
  -e DOUNAI_API_TOKEN="$(dounai-input token)" \
  -e DOUNAI_PUBLIC_BASE_URL="http://127.0.0.1:8000" \
  -v "$PWD/data:/app/data" \
  dounai-input:0.1.1
```

镜像以固定 UID/GID `10001:10001` 的非 root 用户运行。宿主机 `data/` 必须对该 UID 可写；在 Linux 上：

```bash
mkdir -p data
sudo chown -R 10001:10001 data
```

不同平台可先查看：

```bash
docker run --rm --entrypoint id dounai-input:0.1.1
```

### 3.2 本地模型

默认镜像**不安装** faster-whisper。需要离线转写时构建：

```bash
docker build --build-arg INSTALL_LOCAL_ASR=1 -t dounai-input:local .
docker run ... dounai-input:local \
  dounai-input models install small
```

模型应持久化在挂载的 `data/models/`。CPU 镜像可用；GPU 需要自行基于 NVIDIA CUDA 运行时构建，并安装匹配的 CT2/CUDA 库，本项目不提供预构建 GPU 镜像。

### 3.3 Compose

```bash
cp docker-compose.example.yml docker-compose.yml
cp .env.example .env
# 编辑 .env，设置 DOUNAI_API_TOKEN 与需要的外部地址
docker compose up -d --build
```

示例只绑定 `127.0.0.1:8000`。对外暴露时请在反向代理层做 TLS，不要直接把端口发布到公网。

## 4. 反向代理（TLS）

### Caddy

```caddyfile
dounai.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
}
```

### Nginx

```nginx
server {
    listen 443 ssl http2;
    server_name dounai.example.com;

    ssl_certificate     /etc/letsencrypt/live/dounai.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/dounai.example.com/privkey.pem;

    client_max_body_size 32m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 120s;
    }
}
```

必须：

- 设置 `DOUNAI_PUBLIC_BASE_URL=https://dounai.example.com`，否则事件里的媒体 URL 指向 localhost；启动日志、`/healthz` 的 `config_warnings` 与 `doctor` 会告警明显不一致；
- 保留 `Host` 头，且与 `public_base_url` 的 host 一致，否则浏览器写请求会被 Origin 校验拒绝；
- 配置足够的请求体上限（至少 26 MB，声纹录入可到 200 MB，见 API 文档）；
- 配置较长的读超时（ASR 可能几十秒）；
- 禁止在代理日志中记录 Authorization、媒体 URL 和请求 body。

代理层的访问日志仍会记录 IP 与路径。若不希望留存，关闭访问日志或缩短保留期。Uvicorn 访问日志默认关闭；如需开启可给 `serve` 加 `--access-log`，但媒体路径会包含能力 URL。

## 5. 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DOUNAI_CONFIG` | 当前目录 `dounai.toml`（若存在） | 配置文件 |
| `DOUNAI_DATA_DIR` | `./data` | 数据库/媒体/模型根目录 |
| `DOUNAI_HOST` | `127.0.0.1` | 监听地址 |
| `DOUNAI_PORT` | `8000` | 端口 |
| `DOUNAI_PUBLIC_BASE_URL` | `http://127.0.0.1:<port>` | 媒体引用与 Origin 校验使用 |
| `DOUNAI_API_TOKEN` | 空 | 非回环部署必需 |
| `DOUNAI_ALLOW_INSECURE_BIND` | `0` | 仅隔离开发网络使用 |
| `DOUNAI_VOICEPRINT_ENABLED` | `0` | 初始开关；运行时开关会持久化并优先 |
| `DOUNAI_WAKE_PHRASE` | `你好豆奶` | 唤醒词 |
| `DOUNAI_VOICEPRINT_THRESHOLD` | `0.82` | 相似度阈值 |
| `DOUNAI_MEDIA_RETENTION_HOURS` | `72` | 事件/媒体保留 |
| `DOUNAI_PROVIDER_TIMEOUT_SECONDS` | `60` | 单次 provider 超时 |
| `DOUNAI_ALLOW_REMOTE_FALLBACK` | `0` | 允许远程降级（隐私敏感） |
| `DASHSCOPE_API_KEY` | 空 | 推荐第三方 ASR |
| `OPENAI_API_KEY` | 空 | 可选第三方 ASR |
| `DOUNAI_CUSTOM_HTTP_TOKEN` | 空 | 自有 HTTP provider |
| `HF_ENDPOINT` | 官方 | 可选模型镜像 |

TOML 中的配置优先级低于环境变量（仅对实现了环境变量映射的字段）。密钥不支持写 TOML。

## 6. API Token 与轮换

生成：

```bash
dounai-input token
```

设置后，所有 `/v1` 业务接口与 Web 控制台 API 调用需要 Bearer Token；`/healthz`、静态资源、媒体能力 URL 不需要。

轮换：

1. 生成新 Token；
2. 更新环境变量/Secret；
3. 滚动重启（单实例即停止再启动）；
4. 旧 Token 立即失效。

Token 是单实例共享的，不是“每用户”。设备接入共享同一个 Token 时，泄露影响所有来源；需要设备级隔离时，应在反向代理层为不同设备增加独立凭据，或修改代码引入多 Token（当前未实现）。

## 7. 备份与恢复

需要备份：

- `data/dounai.sqlite3`（以及 WAL/SHM；最好停机或使用 SQLite 在线备份）；
- `data/media/`；
- `data/models/`（可选，可重新下载）；
- 配置/环境变量（不要备份明文密钥到不安全位置）。

简单停机备份：

```bash
systemctl stop dounai-input
cp -a data data.backup-$(date +%F)
systemctl start dounai-input
```

SQLite 在线备份：

```bash
sqlite3 data/dounai.sqlite3 ".backup 'backup.sqlite3'"
```

恢复时保持目录结构与权限。恢复旧数据库会恢复其中仍保留的事件/声纹，即使之前已执行删除；如果用户已要求删除，备份也必须按你的数据保留政策清理。备份不是“用户已删除”的例外。

## 8. systemd（Linux 示例）

```ini
[Unit]
Description=Dounai Input Gateway
After=network.target

[Service]
Type=simple
User=dounai
WorkingDirectory=/opt/dounai-input
EnvironmentFile=/etc/dounai-input.env
ExecStart=/opt/dounai-input/.venv/bin/dounai-input serve --host 127.0.0.1
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/dounai-input
AmbientCapabilities=
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
```

`/etc/dounai-input.env` 里放 `DOUNAI_DATA_DIR=/var/lib/dounai-input`、`DOUNAI_API_TOKEN`、`DASHSCOPE_API_KEY` 等，权限 `0600`，**不要**放进仓库。`ProtectHome` 可能影响放在 `/home` 下的数据目录，按实际调整。

## 9. Windows

- 使用专用本地账户运行；
- `data/` 放在非共享、受 BitLocker 保护的分区；
- 用“任务计划程序”或 NSSM 注册服务；
- Windows 上 `chmod` 不影响 ACL，必须用 `icacls` 限制数据目录；
- 控制台录音需要浏览器允许麦克风；HTTPS 或 localhost 才允许 `getUserMedia`；
- 防火墙只放行反向代理到 `127.0.0.1:8000`，不要直接暴露 Python 进程。

示例（管理员 PowerShell）：

```powershell
icacls "D:\dounai-input\data" /inheritance:r /grant:r "$env:USERNAME:(OI)(CI)F"
```

## 10. 资源与容量

- 核心服务：约 100–200 MB 内存；
- faster-whisper small：模型加载后约 1 GB 内存（取决于 CT2/设备）；
- `local-asr` extra 固定 `av<19`，以避开 faster-whisper 1.2.x 与 PyAV 19 的解码参数不兼容；升级依赖时先在测试环境验证转写。
- 媒体：按输入大小和保留期线性增长；72 小时内大量音频可能占数 GB；
- SQLite 单文件：事件元数据很小，媒体在文件系统；
- 上传上限：图像 15 MB、音频 25 MB；声纹录入最多 8 文件，代理层上限需相应提高。

监控建议：`/healthz`、磁盘余量、SQLite 文件大小、媒体目录大小、进程内存、provider 失败日志。项目不提供 Prometheus 指标，避免隐式遥测。

## 11. 升级

1. 阅读 `CHANGELOG.md`；
2. 停止服务；
3. 备份 `data/`；
4. 更新代码并 `pip install -e .`（或构建新镜像）；
5. 启动，检查 `/healthz` 和 `/v1/providers`；
6. 测试文字、语音、导出、删除。

SQLite schema 目前版本为 1；新增迁移必须向后兼容并提供备份说明。不要直接手改生产数据库。

## 12. 故障排查

| 现象 | 排查 |
| --- | --- |
| 启动时报非回环地址错误 | 设置 `DOUNAI_API_TOKEN`，或仅绑定 `127.0.0.1` |
| 控制台所有 API 401 | 输入 Token；确认与 `DOUNAI_API_TOKEN` 一致 |
| 浏览器写请求 403 forbidden_origin | `public_base_url`/`Host` 不匹配，修正代理头 |
| 媒体 URL 指向 localhost | 设置 `DOUNAI_PUBLIC_BASE_URL` |
| 语音无转写 | `providers list`、密钥、本地依赖/权重、日志中的降级原因 |
| 声纹不触发 | 授权/开关/模板/WAV/唤醒词/阈值逐项检查 |
| 模型加载失败 | 校验模型文件、`compute_type`、显存/内存、Python 版本 |
| Docker 无法写 data | 修正 UID/GID 或挂载权限 |
| 磁盘持续增长 | 检查保留期任务日志、上传量、模型目录、备份 |

健康检查：

```bash
curl -s http://127.0.0.1:8000/healthz
curl -s -H "Authorization: Bearer $DOUNAI_API_TOKEN" http://127.0.0.1:8000/v1/providers/status
```

## 13. 明确不支持

- 多副本负载均衡（SQLite 与本地媒体）；
- Kubernetes 多租户/多用户；
- 内置 TLS（请用反向代理）；
- 设备配对、OAuth、每设备独立密钥；
- 云端任务队列/回调；
- 审计日志外送（避免遥测）；
- 自动备份（由部署方负责）。
