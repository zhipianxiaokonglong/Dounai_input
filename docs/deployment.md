# 部署指南

## 1. 部署形态

| 形态 | 适用场景 | 关键点 |
| --- | --- | --- |
| 本机单用户 | 桌面演示、开发、个人使用 | 默认回环绑定，无 Token；云端转写需能出站访问厂商 |
| 单机 + 反向代理 | 小团队内网/公网单实例 | HTTPS、Token、正确的 `public_base_url`、出站 allowlist |
| Docker | 可重复部署 | 数据卷权限、模型可选、keyring 通常不可用 |
| 多副本横向扩展 | 当前**不支持** | SQLite/本地媒体是单实例语义 |

当前版本没有用户账户、配额、限流或多租户隔离。不要把它直接当作公共 SaaS 网关。

从 0.2.0 起，第三方模型不再通过环境变量或 `[providers.*]` 静态配置接入。用户在 Web 控制台建立 **API 连接**（Base URL + API Key），服务端据此发现模型、探测能力并派生 provider。厂商 Key 不写入 `.env`、TOML 或镜像；本机只保存 AES-256-GCM 密文。运行远程转写时，**服务进程本身**需要能出站访问用户填写的厂商地址（不是浏览器直连）。

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
├── .secret.key          # 仅 file 后端；keyring 后端不存在
├── media/
├── models/
└── plugins/
```

`.secret.key` 用于解密数据库中的厂商 API Key，敏感级别等同于数据库本身。它只在第一次保存 API 连接时创建，`0600`（POSIX）；删除最后一个连接或“删除全部个人数据”时会被删除。

建议使用专用系统账户运行，设置：

```bash
export DOUNAI_DATA_DIR="/var/lib/dounai-input"
export DOUNAI_PUBLIC_BASE_URL="https://dounai.example.com"
export DOUNAI_API_TOKEN="$(dounai-input token)"
```

## 3. Docker

### 3.1 构建与运行

```bash
docker build -t dounai-input:0.2.0 .
docker run --rm -p 127.0.0.1:8000:8000 \
  -e DOUNAI_API_TOKEN="$(dounai-input token)" \
  -e DOUNAI_PUBLIC_BASE_URL="http://127.0.0.1:8000" \
  -v "$PWD/data:/app/data" \
  dounai-input:0.2.0
```

镜像以固定 UID/GID `10001:10001` 的非 root 用户运行。宿主机 `data/` 必须对该 UID 可写；在 Linux 上：

```bash
mkdir -p data
sudo chown -R 10001:10001 data
```

容器通常没有系统钥匙串：`DOUNAI_SECRET_BACKEND=auto` 会使用文件后端，主密钥位于挂载的 `data/.secret.key`，随数据卷一起备份/迁移。如果坚持使用 keyring，需要在容器内提供 D-Bus 与 Secret Service（或 Windows/macOS 的对应凭据服务）；否则在首次保存或读取 API 连接时会报错，`auto` 则记录警告并回落到文件。通常不值得为单容器部署引入这些依赖。

远程转写由容器内的服务进程发起。若使用 `--network` 或防火墙限制出站，请放行用户要接入的厂商域名；否则“发现模型”会连接失败，转写会降级。

不同平台可先查看：

```bash
docker run --rm --entrypoint id dounai-input:0.2.0
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
# 编辑 .env，设置 DOUNAI_API_TOKEN 与需要的外部地址；不要在这里写厂商 API Key
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

反向代理只负责入站。API 连接的发现、探测与远程转写由**网关进程主动出站**：宿主机/容器安全组需要放行到用户填写的厂商域名（通常 443）；若用户启用了明文 HTTP 或私网地址，也需要相应放行。发现与能力探测固定 20 秒超时；转写受 `DOUNAI_PROVIDER_TIMEOUT_SECONDS`（默认 60 秒）和 provider 的 60 秒上限约束。

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
| `DOUNAI_ACTIVE_PROVIDER` | `offline` | 初始 active；运行时切换会持久化并优先 |
| `DOUNAI_PROVIDER_TIMEOUT_SECONDS` | `60` | 单次 provider 超时 |
| `DOUNAI_ALLOW_REMOTE_FALLBACK` | `0` | 允许远程降级（隐私敏感） |
| `DOUNAI_SECRET_BACKEND` | `auto` | API Key 主密钥位置：`auto` / `file` / `keyring` |
| `DOUNAI_CUSTOM_HTTP_TOKEN` | 空 | 自有 HTTP provider |
| `HF_ENDPOINT` | 官方 | 可选模型镜像 |

不再有 `DASHSCOPE_API_KEY`、`OPENAI_API_KEY` 等厂商密钥变量。厂商密钥只在 Web 控制台或 `POST /v1/api-connections` 中提交，由 `SecretStore` 加密入库；`SecretStore` 只从环境变量读取“主密钥放在哪里”（`DOUNAI_SECRET_BACKEND`），从不读取厂商 Key 本身。

TOML 中的配置优先级低于环境变量（仅对实现了环境变量映射的字段）。密钥不支持写 TOML。

## 6. API 连接与密钥管理

### 6.1 建立连接

在 Web 控制台“API 厂商接入”中：

1. 选择预设（阿里云百炼、OpenAI、OpenRouter、SiliconFlow、本机 Ollama）或填自定义 Base URL；
2. 粘贴自己的 API Key；
3. “发现模型”调用 `GET {base_url}/models`；
4. 勾选模型并“测试能力”，服务端用合成文本/PNG/WAV 探测 text/image/audio；
5. 保存后，每个模型成为一个 `api-<连接短id>-<模型slug>` provider，可切换；转写失败时按 `[models].fallback`（默认本地 Whisper）降级。

预设只是便捷入口，不内置任何厂商密钥或账号。支持范围仅限 OpenAI 兼容协议；Azure 原生、Anthropic 原生等暂不支持。

### 6.2 网络与 URL 策略

- 默认只允许 HTTPS；`http` 需要连接上的 `allow_http`，私网/回环需要 `allow_private`；
- 云元数据地址永久拒绝，域名解析结果逐个校验，httpx 不跟随重定向；
- 发现/探测/转写都由服务进程出站；部署方应为厂商域名配置 egress allowlist，或在代理层统一出网；
- 不能绕过 DNS rebinding 的 TOCTOU 窗口。高敏感部署应把出口限制到固定厂商域名，而不是只依赖应用层校验。

### 6.3 主密钥与 keyring

默认 `auto`：如果安装了 keyring 依赖且系统凭据后端可用，就把主密钥放进操作系统钥匙串；否则放在 `data/.secret.key`。

```bash
pip install ".[keyring]"
export DOUNAI_SECRET_BACKEND=keyring   # 或 auto / file
```

- `file`：`data/.secret.key`，`0600`；随 `data/` 一起备份与迁移。
- `keyring`：条目服务名 `dounai-input`，用户名由数据目录绝对路径派生；数据目录里不再有主密钥文件。
- `auto` 在 keyring 读/写失败时回退到文件；从文件切到 keyring 会自动迁移旧主密钥，反向切换也会尝试迁回。
- 移动数据目录或换机后，keyring 条目不会自动跟随（名称绑定旧路径），保存的 Key 会无法解密；需要重新填写 Key，或手动迁移条目。

推荐：单机桌面用 `auto`/`keyring`；容器与服务器用 `file` + 磁盘加密 + 加密备份，避免在没有 Secret Service 的环境里得到“看似 keyring、实际文件”的误解。

### 6.4 轮换与删除

- 轮换单个厂商 Key：控制台编辑连接，或在 `PATCH /v1/api-connections/{id}` 只提交 `api_key`；旧密文被覆盖，无需重启。
- 删除连接：删除其下全部模型与密钥密文；若它是当前 provider，会切回 `offline`。
- 删除最后一个连接或“删除全部个人数据”：主密钥文件与对应 keyring 条目一并删除。
- 主密钥丢失/被替换：已存 Key 无法解密，provider 会被禁用；重新填写 Key 即可，不需要重建数据库。
- 备份 `data/` 时，若使用文件后端，必须同时保护 `.secret.key`；只备份数据库而不备份主密钥，恢复后 Key 不可用。

## 7. API Token 与轮换

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

## 8. 备份与恢复

需要备份：

- `data/dounai.sqlite3`（以及 WAL/SHM；最好停机或使用 SQLite 在线备份）；
- `data/media/`；
- `data/.secret.key`（仅 file 后端；与数据库同等敏感）；
- `data/models/`（可选，可重新下载）；
- 配置/环境变量（不要备份明文密钥到不安全位置）。

使用 keyring 时，主密钥不在 `data/` 中，需要单独备份操作系统凭据条目，或接受“换机后重新填写厂商 Key”。keyring 条目名称由数据目录绝对路径派生，恢复到不同路径时也不会自动匹配。

简单停机备份（文件后端）：

```bash
systemctl stop dounai-input
cp -a data data.backup-$(date +%F)
chmod -R go-rwx data.backup-$(date +%F)
systemctl start dounai-input
```

SQLite 在线备份：

```bash
sqlite3 data/dounai.sqlite3 ".backup 'backup.sqlite3'"
```

在线备份只覆盖数据库，不包含媒体、`.secret.key` 或 keyring 条目；恢复时要把它们一起还原，否则 API Key 无法解密、媒体引用会悬空。

恢复时保持目录结构与权限。恢复旧数据库会恢复其中仍保留的事件/声纹，即使之前已执行删除；如果用户已要求删除，备份也必须按你的数据保留政策清理。备份不是“用户已删除”的例外。

只恢复数据库而不恢复 `.secret.key` 时，事件/声纹仍可用，但已保存的 API Key 会解密失败，需要用原 Key 重新保存连接。

## 9. systemd（Linux 示例）

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

`/etc/dounai-input.env` 里放 `DOUNAI_DATA_DIR=/var/lib/dounai-input`、`DOUNAI_API_TOKEN`、`DOUNAI_SECRET_BACKEND=file` 等，权限 `0600`，**不要**放进仓库。厂商 API Key 不写这里；它在控制台保存后加密进数据库。`ProtectHome` 可能影响放在 `/home` 下的数据目录，按实际调整。

## 10. Windows

- 使用专用本地账户运行；
- `data/` 放在非共享、受 BitLocker 保护的分区；
- 用“任务计划程序”或 NSSM 注册服务；
- Windows 上 `chmod` 不影响 ACL，必须用 `icacls` 限制数据目录与 `.secret.key`；
- 安装 `pip install ".[keyring]"` 并保持 `DOUNAI_SECRET_BACKEND=auto` 时，主密钥会存进 Windows Credential Manager（当前用户作用域）；不安装则落回 `data/.secret.key`；
- 控制台录音需要浏览器允许麦克风；HTTPS 或 localhost 才允许 `getUserMedia`；
- 防火墙只放行反向代理到 `127.0.0.1:8000`，不要直接暴露 Python 进程；同时允许进程出站访问用户接入的厂商域名。

示例（管理员 PowerShell）：

```powershell
icacls "D:\dounai-input\data" /inheritance:r /grant:r "$env:USERNAME:(OI)(CI)F"
```

## 11. 资源与容量

- 核心服务：约 100–200 MB 内存；
- faster-whisper small：模型加载后约 1 GB 内存（取决于 CT2/设备）；
- `local-asr` extra 固定 `av<19`，以避开 faster-whisper 1.2.x 与 PyAV 19 的解码参数不兼容；升级依赖时先在测试环境验证转写。
- 媒体：按输入大小和保留期线性增长；72 小时内大量音频可能占数 GB；
- SQLite 单文件：事件元数据很小，媒体在文件系统；
- 上传上限：图像 15 MB、音频 25 MB；声纹录入最多 8 文件，代理层上限需相应提高。
- API 连接：连接/模型/密文只占很少的行；主要成本是出站带宽与厂商调用费用。发现与探测各发一次或数次请求（探测最多 4 次：文本、图像、音频两种协议）；保存 20 个模型会产生 20 组能力记录，但不会在保存时自动重新探测。

监控建议：`/healthz`、磁盘余量、SQLite 文件大小、媒体目录大小、进程内存、provider 失败日志、出站调用失败率。项目不提供 Prometheus 指标，避免隐式遥测。

## 12. 升级

1. 阅读 `CHANGELOG.md`；
2. 停止服务；
3. 备份 `data/`（文件后端同时备份 `data/.secret.key`）；
4. 更新代码并 `pip install -e .`（或构建新镜像）；
5. 启动，检查 `/healthz` 和 `/v1/providers`；
6. 测试文字、语音、导出、删除。

### 12.1 从 0.1.x 升到 0.2.0

- SQLite schema 会自动迁移到 **v3**，新增 `api_connections` 与 `api_models`；旧库无需手工操作，但仍应先备份。
- `[providers.dashscope]`、`[providers.openai]` 与 `DASHSCOPE_API_KEY`、`OPENAI_API_KEY` 已移除。旧 TOML 段不再有内置适配器，不会生效；旧环境变量不再读取。
- 旧数据库里若 active provider 是 `dashscope`/`openai`，启动时会因为 provider 不存在而回退到 `offline`；不会报错，但需要重新建立连接并切换。
- 在控制台建立 API 连接（Base URL + Key → 发现 → 探测 → 保存），再切换到保存出的 `api-*` provider。
- 旧密钥不会迁移：即使以前用环境变量配置成功，也必须重新粘贴一次，之后以密文形式入库。
- 升级后首次使用建议逐一探测所选模型；厂商模型名或协议可能已变化。

SQLite schema 当前版本为 3；新增迁移必须向后兼容并提供备份说明。不要直接手改生产数据库。

## 13. 故障排查

| 现象 | 排查 |
| --- | --- |
| 启动时报非回环地址错误 | 设置 `DOUNAI_API_TOKEN`，或仅绑定 `127.0.0.1` |
| 控制台所有 API 401 | 输入 Token；确认与 `DOUNAI_API_TOKEN` 一致 |
| 浏览器写请求 403 forbidden_origin | `public_base_url`/`Host` 不匹配，修正代理头 |
| 媒体 URL 指向 localhost | 设置 `DOUNAI_PUBLIC_BASE_URL` |
| 语音无转写 | `providers list`、当前 active、本地依赖/权重、日志中的降级原因 |
| API 发现模型 401/403 | 厂商 Key 错误或无该模型权限；重新粘贴/在厂商控制台检查 |
| API 发现模型 404 | Base URL 缺少 `/v1` 或路径不对；确认是 OpenAI 兼容根路径 |
| 提示私网/回环地址被拒绝 | 仅在信任目标时勾选 `allow_private`；不要对公网地址开启 |
| 提示 `http://` 被拒绝 | 确认目标可信后勾选 `allow_http`；生产应改用 HTTPS |
| 发现/探测超时 | 服务进程出站被防火墙/DNS 拦截；检查代理环境变量与 20 秒超时 |
| 保存时报“没有已验证能力” | 重新探测；该模型确实不支持文字/图像/语音中的任一项 |
| 连接存在但 provider 不可用 | 检查当前 active、`allow_remote_fallback`、密钥是否可解密 |
| 提示“无法解密已保存的 API Key” | 主密钥被替换或恢复数据库时漏了 `.secret.key`；重新保存 Key |
| `DOUNAI_SECRET_BACKEND=keyring` 启动失败 | 安装 `.[keyring]` 并提供 Secret Service/Credential Manager；或改 `auto`/`file` |
| 换机后 keyring 里的 Key 失效 | 条目绑定旧数据目录绝对路径；重新保存 Key 或迁移条目 |
| 声纹不触发 | 授权/开关/模板/WAV/唤醒词/阈值逐项检查 |
| 模型加载失败 | 校验模型文件、`compute_type`、显存/内存、Python 版本 |
| Docker 无法写 data | 修正 UID/GID 或挂载权限 |
| 磁盘持续增长 | 检查保留期任务日志、上传量、模型目录、备份 |

健康检查：

```bash
curl -s http://127.0.0.1:8000/healthz
curl -s -H "Authorization: Bearer $DOUNAI_API_TOKEN" http://127.0.0.1:8000/v1/providers/status
curl -s -H "Authorization: Bearer $DOUNAI_API_TOKEN" http://127.0.0.1:8000/v1/api-connections
```

## 14. 明确不支持

- 多副本负载均衡（SQLite 与本地媒体）；
- Kubernetes 多租户/多用户；
- 内置 TLS（请用反向代理）；
- 设备配对、OAuth、每设备独立密钥；
- 云端任务队列/回调；
- 审计日志外送（避免遥测）；
- 自动备份（由部署方负责）；
- 厂商密钥托管/恢复/HSM（主密钥丢失只能重新填写 Key）；
- Azure 原生、Anthropic 原生等非 OpenAI 兼容协议（可用自定义 Base URL 的兼容层，但不保证）；
- 出站网络隔离/egress 防火墙（需由部署环境提供）。
