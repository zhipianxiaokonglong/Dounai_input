# 用户指南

本文面向部署和使用豆奶输入层的人。它覆盖四件事：**启动服务、安装本地模型、接入第三方 API、管理声纹与个人数据**。

> 输入层可以完全独立运行。它不会连接豆奶的其他模块，也不需要下游服务存在。默认的离线直通模式不访问网络。

## 1. 安装与启动

### 环境要求

- Python 3.11 或 3.12（推荐 3.12；可选本地模型对 3.13/3.14 的支持取决于 faster-whisper/CT2 的上游版本）
- Windows、macOS 或 Linux
- 语音转写建议至少 4 GB 可用内存；本地 large 档建议使用 GPU

### 启动

```bash
git clone https://github.com/zhipianxiaokonglong/Dounai_input.git
cd Dounai_input

python -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e .

dounai-input serve
```

打开 <http://127.0.0.1:8000/>。

默认配置：

| 项 | 默认值 | 含义 |
| --- | --- | --- |
| 模型 | `offline` | 只做校验与标准化，不做语音转写 |
| 网络 | 仅监听 `127.0.0.1` | 不对外网开放 |
| 声纹 | 关闭 | 不提取任何声纹特征 |
| 数据目录 | `./data` | SQLite、媒体文件和模型都存放在这里 |
| 保留期 | 72 小时 | 事件、音频和图像自动清理 |

首次启动会创建 `data/`。**不要把 `data/` 提交到 Git**；`.gitignore` 已排除。

### 停止与重启

按 `Ctrl+C` 停止。SQLite 使用 WAL 模式，正常停止后可直接重启。若在写入媒体时强制结束进程，启动后的清理任务会移除没有被数据库引用的旧文件。

## 2. 三种输入

Web 控制台有三个标签：文字、图像、语音。

- **文字**：直接输入，原样进入事件的 `content`。
- **图像**：拖入或选择 JPEG / PNG / WebP / GIF，最大 15 MB。事件 `content` 是一个受控媒体 URL。
- **语音**：录音或上传 WAV / MP3 / M4A / FLAC / OGG / WebM，最大 25 MB。若配置了语音模型，转写文本写入 `metadata.asr_text`，转写可用性写入 `metadata.asr_status`。

控制台录音最长 60 秒；浏览器会在上传前尽量转成 16 kHz PCM WAV，以便进行本地声纹判断。直接上传非 WAV 语音仍可正常生成事件，但声纹判断会跳过。

发送后右侧显示**精确的事件 JSON**。下游只应消费这个结构。会话 ID 在响应头 `X-Dounai-Session` 中稳定回传（默认生成的会话 ID 是 UUID，可直接用于下游服务）；事件体不包含会话字段，以保持 v1.0 兼容。

### 外部消费媒体

图像/音频的 `content` 是 URL。若消费方不在同一台机器，无法访问该 URL，可使用需要 API Token 的：

```bash
# 只取字节（base64）
curl -H "Authorization: Bearer $DOUNAI_API_TOKEN" \
  http://127.0.0.1:8000/v1/media/<media_id>/base64

# 或按事件取媒体元数据 + 可选字节
curl -H "Authorization: Bearer $DOUNAI_API_TOKEN" \
  "http://127.0.0.1:8000/v1/events/<event_id>/media?include_base64=true"
```

部署时请把 `DOUNAI_PUBLIC_BASE_URL` 设为消费方实际可访问的地址；启动日志、`/healthz` 的 `config_warnings` 与 `dounai-input doctor` 会在明显不一致时告警。

## 3. 安装本地免费模型

核心包不包含模型权重。本地模型是可选组件，按需安装。

### 3.1 安装可选依赖

```bash
pip install -e ".[local-asr]"
```

这会安装 `faster-whisper` 与 `huggingface-hub`。它比核心包大，但不会自动下载权重。

> 兼容性说明：`faster-whisper 1.2.x` 仍向 PyAV 传递 `metadata_errors`，而 PyAV 19 移除了该参数。因此本项目在 `local-asr` extra 中固定 `av>=11,<19`。如果你手动安装依赖，请使用 `pip install "faster-whisper>=1.1,<2" "av>=11,<19"`，否则本地转写可能在解码阶段报 `TypeError`。上游修复后可以放宽。

### 3.2 查看与下载模型

```bash
dounai-input models list
dounai-input models install small
```

推荐先安装 `small`（约 466 MB）：中文效果与 CPU 速度平衡。可选档位：

| ID | 大约大小 | 说明 |
| --- | ---: | --- |
| `tiny` | 75 MB | 速度最快，中文准确率低 |
| `base` | 145 MB | 轻量短语音 |
| `small` | 466 MB | **默认推荐** |
| `medium` | 1.5 GB | 更高准确率 |
| `large-v3-turbo` | 1.6 GB | 大模型效果，建议 GPU |
| `large-v3` | 3.1 GB | 本地最高准确率，建议 GPU |

模型来自 `Systran/faster-whisper-*` 的 CTranslate2 转换权重，上游 OpenAI Whisper 权重为 MIT 许可。安装器会：

1. 读取并固定 Hugging Face commit revision；
2. 下载到 `data/models/whisper-<id>/`；
3. 记录权重文件的 SHA-256；
4. 不把权重放进仓库。

校验与删除：

```bash
dounai-input models verify small
dounai-input models remove small --yes
```

下载慢或无法直连 Hugging Face 时，可设置镜像后重试：

```bash
# PowerShell
$env:HF_ENDPOINT = "https://hf-mirror.com"
# macOS / Linux
export HF_ENDPOINT="https://hf-mirror.com"
```

镜像由第三方提供，请自行评估可用性与信任。

如果下载报 TLS 证书错误（常见于公司代理/自签根证书）：

```bash
# macOS / Linux：指向系统或企业 CA bundle
export SSL_CERT_FILE="/path/to/company-ca-bundle.pem"
dounai-input models install small
```

Windows 可把系统根证书导出为 PEM 后设置 `SSL_CERT_FILE`。安装器不会建议关闭 TLS 校验来“绕过”证书错误。若只需固定 revision 并已有本地模型，也可把 `model` 配置成模型目录的绝对路径。

### 3.3 启用本地模型

```bash
dounai-input providers activate local-whisper
```

或在 Web 控制台的当前模型下拉框中选择「本地 Whisper」。若模型 ID 不是 `small`，在 `dounai.toml` 中修改：

```toml
[providers.local-whisper]
model = "medium"
device = "auto"        # auto / cpu / cuda
compute_type = "int8"  # CPU 推荐 int8；GPU 可尝试 float16
```

音频不会离开设备。首次转写会加载模型，可能比后续请求慢。

## 4. 接入第三方 API（BYOK）

本层不内置任何厂商的静态配置，也不提供账号或密钥。所有云端模型都在 Web 控制台的 **API 厂商接入** 里现场接入：填写 OpenAI 兼容的 Base URL 与自己的 API Key，服务端调用厂商的 `/models` 列出可用模型，再对选中的模型真实发起文字、图像、语音请求，只有**实测通过**的能力才会被保存。

一个**连接**对应一个厂商端点与一把密钥，连接下可以保存多个模型；每个保存的模型都会自动注册成一个 provider，可像本地模型一样切换、参与能力感知降级。

> 0.2.0 起，静态的 `dashscope` / `openai` provider 与 `DASHSCOPE_API_KEY` / `OPENAI_API_KEY` 已移除，也不需要把密钥写进环境变量。`dounai.toml` 里的 `[providers.dashscope]`、`[providers.openai]` 会被忽略，`[models].fallback` 中的旧 provider id 会被跳过。

### 4.1 推荐厂商

中文情感陪伴场景推荐 **Qwen3-ASR-Flash**（阿里云百炼 / Model Studio）：

- 中文、方言与中英混说识别稳定；
- 单次调用即完成转写，无需本地 GPU；
- 同一账号体系可继续用 **Qwen3.8-Omni-Flash** 做图文音全模态理解；
- 提供 OpenAI 兼容协议，可直接用本节的连接流程接入。

这不是对绝对准确率的担保；不同口音、噪声和领域词汇仍应做自己的抽样评估。内置预设如下，也可以在「预设」里选「手动输入」填任意 OpenAI 兼容端点：

| 预设 | Base URL | 说明 |
| --- | --- | --- |
| 阿里云百炼 Qwen（推荐） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 中文语音转写首选；推荐模型 `qwen3-asr-flash`、`qwen3.8-omni-flash` |
| OpenAI | `https://api.openai.com/v1` | 官方转写与视觉接口；需要能直连 OpenAI 的网络 |
| OpenRouter | `https://openrouter.ai/api/v1` | 多厂商聚合，模型列表很大，按需搜索勾选 |
| SiliconFlow | `https://api.siliconflow.cn/v1` | 国内 OpenAI 兼容聚合，可用模型以账号为准 |
| Ollama（本机） | `http://127.0.0.1:11434/v1` | 完全本地；必须同时勾选「允许访问本机/内网地址」与「允许明文 HTTP」 |

百炼的 API Key 与地域绑定。若控制台给的是 Workspace 专属域名（形如 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`），把 `{WorkspaceId}` 换成自己的 ID，在「手动输入」里填写该域名即可，不需要改配置文件。

### 4.2 在控制台接入

1. 打开 <http://127.0.0.1:8000/>，在「模型与本地安装」卡片里找到 **API 厂商接入**，点「+ 添加连接」。
2. 在「接入厂商 API」对话框里选「预设」。列表第一项是「手动输入」，其后是各预设，默认选中推荐的阿里云百炼。选预设会填好 Base URL 与连接名称；连接名称可以改，最长 80 字符。
3. 粘贴该厂商的 API Key。输入框不回显；密钥只在本机加密保存，请求厂商时才在内存中即时解密。
4. 只有本机或内网端点才需要勾选权限复选框（见 4.3）。
5. 点「发现模型」。服务端请求 `GET {base_url}/models`，结果列在「发现的模型」里，可以按 ID 搜索过滤。列表里没有目标模型时，勾选「模型列表没有目标模型，手动输入 ID」再填模型 ID。
6. 勾选要用的模型（每个连接最多 20 个），它们会进入「待保存的模型」。
7. 点「测试所选模型能力」。服务端对每个模型依次实测文字、图像、语音，结果显示在「测试结果」：`通过 / 不支持 / 错误 / 未测试`；语音通过时还会标出实际使用的转写协议。
8. 点「保存连接」。只有至少一项能力通过的模型才会被保存；全部能力都不通的模型会被拒绝。

保存之后：

- 顶部「当前模型」下拉框会多出 `连接名 · 模型 ID` 条目，选中即可切换。切到云端模型前控制台会确认「语音会被发送到该第三方厂商」，声纹模板始终留在本机。
- 「API 厂商接入」下的连接卡片显示 `Base URL · key <指纹>`；每个模型标签显示能力（文本/图像/语音）和「使用中 / 切换 / 移除模型」。
- 卡片上的「重新发现」只用已保存的密钥重新拉取模型列表，**不会**自动改写已保存模型的能力。厂商更新模型后需要重新测试该模型。
- 卡片上的「删除」会删除该连接、其下所有模型与本机密文；如果它是当前模型，会自动切回「离线直通」。

验证：切换后上传一段中文语音，事件里应出现 `asr_text` 且 `metadata.asr_status="completed"`，响应头 `X-Dounai-Provider` 是 `api-<连接短 id>-<模型 slug>`。若失败，先看第 9 节的排查步骤，也可以运行 `dounai-input doctor` 检查配置与适配器状态。

### 4.3 网络权限与 URL 规则

厂商 URL 由服务端校验，不是简单拼接：

- 默认只接受 `https://`。明文 `http://` 必须显式允许（控制台「允许明文 HTTP」/ `allow_http`），只应用于可信本机或内网；
- 私网与回环地址默认拒绝。`127.0.0.1`、`10.x.x.x`、`192.168.x.x`、`172.16–31.x.x` 等需要「允许访问本机/内网地址」/ `allow_private`；Ollama 预设会替你勾好这两项；
- 云元数据地址（如 `169.254.169.254`、`metadata.google.internal`）始终拒绝，任何开关都无法放行；
- URL 不能包含用户名密码、查询串或 fragment，最长 512 字符；
- 域名会先解析，**所有**解析结果都必须满足上述规则；
- 请求不跟随重定向，厂商返回 `3xx` 视为失败。

创建连接、发现模型、测试模型与「重新发现」都会解析域名，并逐条校验解析结果；列连接、读详情这些只读操作不会触发 DNS。被策略拒绝的 URL 返回 422 `invalid_input`，密钥错误返回 422 `api_auth_failed`，厂商网络/服务异常返回 502 `api_discovery_failed`。

### 4.4 命令行与 API

控制台调用的就是下面这些接口；需要脚本化或用远程实例时可直接调用。设置了 `DOUNAI_API_TOKEN` 时，每个请求都要带 `Authorization: Bearer $DOUNAI_API_TOKEN`。

发现模型（只读，不保存任何内容）：

```bash
curl -X POST http://127.0.0.1:8000/v1/api-connections/discover \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $DOUNAI_API_TOKEN" \
  -d '{
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key": "<你的密钥>"
  }'
```

测试单个模型：

```bash
curl -X POST http://127.0.0.1:8000/v1/api-connections/probe \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $DOUNAI_API_TOKEN" \
  -d '{
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key": "<你的密钥>",
    "model_id": "qwen3-asr-flash"
  }'
```

保存连接。`models[].probes` 用上一步的结果；能力只按 `probes` 中 `status` 为 `supported` 的项计算，音频通过时必须带 `transcription_protocol`：

```bash
curl -X POST http://127.0.0.1:8000/v1/api-connections \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $DOUNAI_API_TOKEN" \
  -d '{
    "name": "百炼 Qwen",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key": "<你的密钥>",
    "models": [
      {
        "model_id": "qwen3-asr-flash",
        "capabilities": ["text", "image", "audio"],
        "transcription_protocol": "audio_transcriptions",
        "probes": {
          "text": {"status": "supported", "detail": "chat completion accepted", "latency_ms": 412},
          "image": {"status": "supported", "detail": "image input accepted", "latency_ms": 780},
          "audio": {"status": "supported", "detail": "audio transcriptions accepted", "latency_ms": 1260}
        }
      }
    ]
  }'
```

查看、重新发现与删除：

```bash
curl -H "Authorization: Bearer $DOUNAI_API_TOKEN" \
  http://127.0.0.1:8000/v1/api-connections

curl -X POST -H "Authorization: Bearer $DOUNAI_API_TOKEN" \
  http://127.0.0.1:8000/v1/api-connections/conn_xxxxxxxx/refresh

curl -X DELETE -H "Authorization: Bearer $DOUNAI_API_TOKEN" \
  http://127.0.0.1:8000/v1/api-connections/conn_xxxxxxxx
```

轮换密钥用 `PATCH /v1/api-connections/{id}`，只传 `api_key`，省略的字段不变；改过 `api_key` 或 `base_url` 后建议重新测试模型能力，旧的能力结论不会自动更新。保存后也可以用 CLI 切换：

```bash
dounai-input providers list
dounai-input providers activate api-1a2b3c4d-qwen3-asr-flash
```

完整字段、错误码与响应示例见 [API 参考](api.md#api-连接byok)。

### 4.5 密钥安全

- **入库即加密**：API Key 用 AES-256-GCM 加密后写入本机 SQLite，明文只在服务端内存中短暂存在，仅用于请求该厂商。
- **不回显**：`GET /v1/api-connections` 只返回 `key_fingerprint`——密钥 SHA-256 的 10 字符短标识，无法反推密钥；响应、日志与导出里都没有明文或密文。
- **主密钥在本机**：默认是 `data/.secret.key`（POSIX 上以 0600 创建，与数据库同目录）。想把它移出数据目录，可安装可选依赖并指定后端：

  ```bash
  pip install -e ".[keyring]"
  # macOS / Linux
  export DOUNAI_SECRET_BACKEND=keyring
  # PowerShell
  # $env:DOUNAI_SECRET_BACKEND = "keyring"
  ```

  `DOUNAI_SECRET_BACKEND` 取值 `auto`（默认：优先系统钥匙串，不可用时回退文件）、`file`、`keyring`（要求系统钥匙串可用，否则保存或读取连接时会报错）。首次从文件切到钥匙串时，已有主密钥会自动迁移，不需要重建连接。Windows 凭据管理器、macOS 钥匙串与 Linux Secret Service 都可用。
- **删除即清除**：控制台「删除全部个人数据」或 `DELETE /v1/privacy/data?confirm=true` 会删除所有连接、密文与主密钥；只删掉最后一个连接时也会删除主密钥。导出 ZIP 只包含连接名称、Base URL、指纹与模型 ID/能力，不含密钥。
- **仍有的边界**：同一操作系统账户下的恶意进程可以读到 `.secret.key`（或调用钥匙串）并解密。加密防的是数据库或数据目录被单独复制、备份泄漏，不是本机进程隔离。详见 [隐私与安全设计](privacy.md)。
- **怀疑泄漏**：先在厂商控制台轮换密钥，再用 `PATCH /v1/api-connections/{id}` 更新，或删除连接后重建，最后确认旧密钥已经失效。

## 5. 模型切换与降级

在 Web 控制台顶部「当前模型」下拉框即可切换，无需重启。也可以：

```bash
dounai-input providers list
dounai-input providers activate api-1a2b3c4d-qwen3-asr-flash
```

API 连接派生的 provider id 形如 `api-<连接短 id>-<模型 slug>`，以 `providers list` 的实际输出为准；删除连接或模型后旧 id 不再存在。

- 选择会写入本地 SQLite，重启后保留。
- 会话上下文保存在本地事件表中。转写请求会携带最近几条文本/转写作为上下文（厂商支持时）。
- 主模型不可用、超时、限流、返回格式异常时，会按 `fallback` 顺序尝试。
- **隐私守卫**：默认 `allow_remote_fallback = false`。当主模型是本地模型时，不会自动把音频发到云端；当主模型是云端时，也不会自动再发给另一家云端。只有显式启用才允许云端降级。
- 全部失败或没有可转写的模型时仍会返回事件：`asr_text` 为空、`metadata.asr_status="unavailable"`、响应头 `X-Dounai-Degraded: true` 与 `X-Dounai-ASR-Status: unavailable`。下游不会因为模型故障而收不到输入，也不会误把空文本当可用转写。

控制台的「降级」标记来自响应头；事件体只增加契约 v1.1 的 `asr_status`，不写其他诊断字段。

## 6. 声纹与唤醒

### 6.1 声纹是什么、不是什么

本实现提取 80 维本地特征（MFCC 均值/标准差 + delta 统计），用于判断「这句话是不是已登记的人说的」。它**不是身份认证**，不能用于门禁、支付、风控等安全决策，也不能抵抗录音回放或合成语音攻击。

### 6.2 打开授权流程

1. 打开控制台右下「声纹与数据」；
2. 点击「阅读声纹授权说明」，阅读政策 v1.0；
3. 勾选「我已阅读并同意…」；
4. 点击「授权并开启」。

也可以调用 API：

```bash
curl -X POST http://127.0.0.1:8000/v1/privacy/consent \
  -H "Content-Type: application/json" \
  -d '{"policy_version":"1.0","granted":true}'
```

授权会同时开启功能开关，但**仍然需要录入模板**才会产生 `wakeup`。

### 6.3 录入声音

准备 3–8 段你自己说话的 PCM WAV，每段 1–20 秒，环境尽量安静：

- 16-bit PCM WAV；
- 单声道或双声道均可（会下混）；
- 不要用音乐、多人对话或纯噪声。

在控制台点击「选择 WAV 样本」，选 3 段以上，再点击「提取并保存声纹模板」。原始样本只在本次请求的内存中用于提取，不落盘；数据库只保存 80 维模板。

也可以：

```bash
dounai-input privacy consent --accept
dounai-input privacy enroll a.wav b.wav c.wav
```

### 6.4 唤醒判断条件

音频事件的 `metadata.voiceprint_event` 只有在以下条件全部满足时才是 `wakeup`：

1. 开关开启；
2. 授权有效；
3. 存在模板；
4. 说话人与模板相似度 ≥ 阈值（默认 0.82）；
5. 转写文本包含唤醒词（默认「你好豆奶」）。

任一条件不满足都是 `none`。如果不想强制唤醒词，在 `dounai.toml` 设置：

```toml
[privacy]
wake_phrase_required = false
```

这样任何通过声纹相似度的语音都会触发 `wakeup`，更容易误唤醒。配置修改后需重启。

阈值可以在 `[privacy].voiceprint_threshold` 调整（0–1）。阈值越高越难唤醒。请用真实录音评估，不要只依赖示例值。

### 6.5 关闭、撤回与删除

| 操作 | Web 控制台 | API |
| --- | --- | --- |
| 关闭功能（保留模板） | 「声纹唤醒」开关 | `PUT /v1/privacy/settings` |
| 撤回同意（删除模板并关闭） | 「撤回同意」 | `DELETE /v1/privacy/consent` |
| 删除模板（保留同意） | 「删除声纹模板」 | `DELETE /v1/privacy/voiceprint` |
| 删除全部个人数据 | 「删除全部个人数据」 | `DELETE /v1/privacy/data?confirm=true` |
| 导出全部数据 | 「导出全部数据」 | `GET /v1/privacy/export` |

关闭功能后不会再提取特征。删除是不可逆的。

### 6.6 声纹不出的边界

- 声纹模板不发送给任何 provider，不进入日志，不进入遥测。
- 若你选择云端语音转写，**音频**会发送给该厂商用于转写；这是转写所必需，和声纹判断是两条独立路径。
- 声纹判断对非 WAV 输入会跳过；Web 录音会自动转 WAV。
- 环境噪声、麦克风变化、感冒会影响相似度。换设备后建议重新录入。

## 7. 隐私开关与数据权利

所有开关在控制台「声纹与数据」区域，也可用 API。

- **数据最小化**：只在有输入时保存事件和媒体；不采集通讯录、位置、设备标识或遥测。
- **保留期**：`retention_hours` 默认 72 小时；到期删除事件与关联媒体。声纹模板与授权记录不按时间删除，等你主动删除或撤回。
- **导出**：ZIP 包含清单、事件、媒体文件、授权记录、隐私操作记录、声纹模板（如有）、API 连接元数据（名称、Base URL、密钥指纹、模型 ID 与能力）和政策文本。导出文件包含生物特征；密钥明文与密文都不会出现在导出包里，请仍妥善保管。
- **删除全部**：删除事件、媒体、声纹模板、授权记录、隐私审计记录、API 连接与其加密密钥（最后一个连接被删除时主密钥也一并清除）。删除后媒体 URL 立即失效；之后会写入一条不含生物特征的操作记录，证明删除发生。
- **本地存储**：数据在 `data/dounai.sqlite3` 与 `data/media/`。数据库和媒体文件权限尽量收紧为仅当前用户可读；请确保操作系统账户本身可信。

详细的安全边界见 [隐私与安全设计](privacy.md)。

## 8. 远程部署与 API Token

默认只监听回环地址，因此不需要 Token。若要监听 `0.0.0.0` 或对外提供服务：

```bash
# 生成一个随机 Token
dounai-input token

# 设置后启动
export DOUNAI_API_TOKEN="生成的-token"
dounai-input serve --host 0.0.0.0
```

**安全限制**：未设置 `DOUNAI_API_TOKEN` 时，绑定非回环地址会直接失败；只有显式设置 `DOUNAI_ALLOW_INSECURE_BIND=1` 才会放行（不建议）。

- Web 控制台会提示输入 Token，只保存在当前标签页的 `sessionStorage`。
- 所有 `/v1/*` 业务接口需要 `Authorization: Bearer <token>`。
- 媒体 URL 使用 128 位不可猜测 ID，作为能力 URL 供 `<img>/<audio>` 和下游按引用读取；不要把它公开。
- 跨站浏览器写请求会被 Origin / `Sec-Fetch-Site` 校验拒绝，避免未授权 localhost CSRF。
- 放反向代理时，把 `public_base_url` 设置为用户实际访问的 HTTPS 地址；代理必须保留 `Host` 或设置成与 `public_base_url` 一致，否则跨站校验会拒绝浏览器写请求。

部署细节见 [部署指南](deployment.md)。

## 9. 常见问题

### 语音事件没有转写文本

1. `dounai-input providers list` 看当前 provider 是否「可用」；
2. 本地模型看是否安装了可选依赖与权重；
3. 云端看「API 厂商接入」里是否有保存好的连接与模型，并确认顶部「当前模型」选的就是它；`dounai-input providers list` 中 `api-…` 条目应为「可用」；
4. 看响应头 `X-Dounai-Degraded`；为 `true` 说明发生了降级；
5. 查看服务日志（不会包含音频内容），确认超时、认证失败、限流或网络错误。

本地模型若日志只显示 `The local Whisper transcription failed.`，先确认 PyAV 版本 `<19`（见第 3 节）。可通过下面的命令复现底层异常：

```bash
python -c "import av; print(av.__version__)"
pip install "av>=11,<19"
```

### 声纹一直不触发

- 确认状态里「已授权 / 已录入 / 已开启」；
- 确认输入是 WAV（Web 录音通常已转换）；
- 确认转写文本包含唤醒词；
- 阈值过高时适当降低并重新测试；
- 用同一麦克风、相近距离重新录入。环境差异很大时，基线模型可能失效。

### 上传返回 413

图像 15 MB、音频 25 MB。Web 控制台也会在前端提前提示。大文件请先压缩或分段。

### 上传返回 415

服务按魔数校验格式，不信文件名。图像支持 JPEG/PNG/WebP/GIF；音频支持 WAV/MP3/M4A/FLAC/OGG/WebM。把文件转成受支持格式后再试。

### 端口被占用

```bash
dounai-input serve --port 8010
```

若通过环境变量修改端口，同时设置 `DOUNAI_PUBLIC_BASE_URL`，否则媒体引用仍然指向旧端口。

### 数据太大

调小 `media_retention_hours`，或定期调用 `DELETE /v1/privacy/data?confirm=true`。删除本地模型用 `dounai-input models remove <id> --yes`。

### 「发现模型」失败

按顺序检查：

1. Base URL 是否是该厂商文档给的 OpenAI 兼容前缀，通常以 `/v1` 结尾（百炼是 `.../compatible-mode/v1`）。少了 `/v1` 会返回 404；
2. Key 是否完整、是否属于该地域或 Workspace；百炼的 Key 与地域绑定，Workspace 专属域名要换成自己的；
3. 本机/内网端点（如 Ollama）是否**同时**勾选了「允许访问本机/内网地址」与「允许明文 HTTP」；
4. 服务进程能否访问外网（代理、防火墙、容器网络）；容器里的 `127.0.0.1` 不是宿主机；
5. 看返回码区分原因：422 `invalid_input` 是 URL 或参数被策略拒绝，422 `api_auth_failed` 是密钥或端点不对，502 `api_discovery_failed` 是网络或厂商异常。错误信息不会包含密钥。

### 所有能力都测试不通过

- 该模型可能不是对话/转写模型（embedding、rerank、纯图像生成通常不提供 `/chat/completions`）；
- 账号未开通该模型，或模型 ID 与账号地域不匹配；
- 只需要语音转写时，只要「语音」一项通过就能保存，文字/图像显示「不支持」不影响转写；
- 「未测试」表示根本没有发出请求（例如鉴权先失败了），不是能力结论；
- 语音探测会先试 `/audio/transcriptions`，再试 `chat/completions` + `input_audio`；两条都不通说明该端点不使用这两种 OpenAI 兼容形态，需要换模型；
- 既要转写又要图像理解时，在同一连接下分别保存 `qwen3-asr-flash` 与一个多模态模型即可，不需要建两个连接。

### 删除连接或移除模型后没有转写了

- 如果删掉的是当前模型，服务会自动切回「离线直通」：事件仍然生成，但 `asr_text` 为空、`metadata.asr_status="unavailable"`、响应头 `X-Dounai-Degraded: true`；
- 在顶部「当前模型」里重新选一个可用模型即可恢复，不需要重启服务；
- 删除最后一个连接会连主密钥一起删除，重新接入时会生成新的主密钥，旧密文无法也无需恢复。

## 10. 卸载

```bash
# 停止服务后
dounai-input models remove small --yes
# 删除本地数据（不可恢复）
dounai-input data delete --yes
```

再删除虚拟环境与仓库目录即可。服务没有系统级守护进程或注册表项。
