# API 参考

基础路径：`/v1`。服务默认监听 `http://127.0.0.1:8000`，交互式 OpenAPI 文档在 `/docs`。

除 `/healthz`、静态控制台与媒体能力 URL 外，设置 `DOUNAI_API_TOKEN` 后所有 `/v1` 请求都需要：

```http
Authorization: Bearer <token>
```

也支持 `X-Dounai-Token: <token>`，供不便设置 `Authorization` 的本地客户端使用。

## 1. 事件契约（v1.1，向后兼容）

所有输入最终返回如下结构，**没有额外字段**。机器可读 JSON Schema 位于仓库 `schemas/input-event.schema.json`，也可请求 `GET /v1/contract`。

```json
{
  "event_id": "7e2c664e-420d-4993-91ae-5fac73a9c584",
  "timestamp": "2026-10-24T09:38:37.008Z",
  "modality": "text",
  "content": "今天有一点累，想找人说说话。",
  "metadata": {
    "asr_text": "",
    "asr_status": "not_applicable",
    "voiceprint_event": "none",
    "model_capabilities": ["text", "image", "audio"]
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `event_id` | UUID string | 每条事件唯一 |
| `timestamp` | ISO 8601 string | UTC，毫秒精度，`Z` 结尾 |
| `modality` | `text \| image \| audio` | 输入模态 |
| `content` | string | 文字原文，或图像/音频的受控引用 URL |
| `metadata.asr_text` | string | 语音转写文本；不适用时为空字符串 |
| `metadata.asr_status` | `completed \| empty \| unavailable \| not_applicable` | v1.1 新增；语音转写是否真正可用 |
| `metadata.voiceprint_event` | `wakeup \| none` | 本地声纹 + 唤醒词判断 |
| `metadata.model_capabilities` | string[] | 当前所选模型声明的能力，按 `text,image,audio` 规范排序 |

`model_capabilities` 表达**模型能力声明**，不是本层接受输入的许可；三种输入在离线模式下也都能生成事件。

### `asr_status` 语义

| 值 | 含义 | 下游应做什么 |
| --- | --- | --- |
| `completed` | provider 成功返回非空文本 | 可使用 `asr_text` |
| `empty` | provider 成功但识别为空（静音/无语音） | 不要当作用户说了话 |
| `unavailable` | 无可用 ASR、provider 失败/超时、选中模型不支持转写 | 不要使用 `asr_text`；提示或降级 |
| `not_applicable` | 文字/图像输入 | 无需处理 |

该字段在 JSON Schema 中**不是 required**：v1.0 事件仍可被解析，v1.1 生产者始终填充。新消费方必须检查它。契约版本通过 `x-contract-version: "1.1"`、`/healthz` 的 `contract_version` 与所有响应头 `X-Dounai-Contract-Version` 暴露；详见 [10.1 版本协商](#101-版本协商契约版本--模块版本)。

### 媒体引用

图像和音频的 `content` 形如：

```text
http://127.0.0.1:8000/v1/media/506608736c6d43368a0089b6e381e31e
```

- 32 位十六进制、128 位不可猜测 ID，属于能力 URL；持有 URL 即可读取，因此不要公开日志或事件。
- 设置 API Token 后，媒体 URL **仍可无 Token 读取**，以便下游 `<img>`/`<audio>` 和媒体管线直接消费。这是有意设计：事件本身只能通过鉴权接口获得。
- 响应带 `Cache-Control: private, no-store` 与 `X-Content-Type-Options: nosniff`。
- 支持 Starlette `FileResponse` 的常规读取语义；媒体 ID 不存在返回 404。

### 跨机 / 云端交付

`content` 是引用，不是字节。若消费方无法访问 `public_base_url`（跨机、容器网络、云端），使用以下**需要鉴权**的接口：

```http
GET /v1/media/{media_id}/base64
GET /v1/events/{event_id}/media
GET /v1/events/{event_id}/media?include_base64=true
```

- `/v1/media/{id}/base64` 返回 `{media_id,mime,size,sha256,filename,base64}`。
- `/v1/events/{id}/media` 返回事件媒体元数据、`session_id`、可访问 `url`；加 `include_base64=true` 返回字节。
- 文字事件没有媒体，返回 404。
- `public_base_url` 与实际监听地址不一致时，启动日志与 `/healthz` 的 `config_warnings` 会告警；`doctor` 也会报告。反向代理场景是合法的（监听回环、对外域名），不会被误报。

## 2. 输入接口

### 2.1 文字

`POST /v1/inputs/text`

```http
Content-Type: application/json
```

```json
{
  "text": "今天有一点累，想找人说说话。",
  "session_id": "sess_optional_123",
  "source": "web-console"
}
```

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `text` | 是 | 1–20000 字符，不能为空白或包含 NUL/控制字符（换行、Tab 除外） |
| `session_id` | 否 | 1–128 位字母/数字/`._:-`；不传则生成 **UUID**。跨层使用建议传 UUID |
| `source` | 否 | 1–64 位字母/数字/`._:-`；不传为 `api` |

```bash
curl -X POST http://127.0.0.1:8000/v1/inputs/text \
  -H "Content-Type: application/json" \
  -d '{"text":"今天有一点累，想找人说说话。","session_id":"sess_1","source":"curl"}'
```

响应 `201 Created`，body 为事件。

### 2.2 图像

`POST /v1/inputs/image`，`multipart/form-data`：

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `file` | 是 | JPEG / PNG / WebP / GIF，最大 15 MB，按魔数校验 |
| `session_id` | 否 | 同上 |
| `source` | 否 | 同上 |

```bash
curl -X POST http://127.0.0.1:8000/v1/inputs/image \
  -F "file=@photo.jpg;type=image/jpeg" \
  -F "source=curl"
```

不接收 SVG（避免脚本注入）或无法通过魔数识别的文件。

### 2.3 语音

`POST /v1/inputs/audio`，`multipart/form-data`：

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `file` | 是 | WAV / MP3 / M4A / FLAC / OGG / WebM，最大 25 MB |
| `session_id` | 否 | 同上 |
| `source` | 否 | 同上 |
| `language` | 否 | BCP-47 形式，如 `zh`、`en-US`；默认 `zh` |

```bash
curl -X POST http://127.0.0.1:8000/v1/inputs/audio \
  -F "file=@voice.wav;type=audio/wav" \
  -F "language=zh" \
  -F "source=curl"
```

处理顺序：

1. 魔数校验并写入本地媒体目录；
2. 读取同会话最近上下文；
3. 调用当前 provider 链转写（失败不阻断事件）；
4. 若开关、授权、模板均满足且文件是 WAV，本地计算声纹；
5. 保存并返回事件。

转写不可用时仍是 `201`：`metadata.asr_status="unavailable"`、`asr_text=""`，响应头 `X-Dounai-Degraded: true` 与 `X-Dounai-ASR-Status: unavailable`。默认 `offline` provider 也属于此类——语音被正常接收，但没有转写能力，必须显式标记而不是静默返回空字符串。

### 2.4 语音转写降级与响应头

| Header | 值 | 含义 |
| --- | --- | --- |
| `X-Dounai-Session` | session id | 服务端确认的会话；`GET /v1/events/{id}` 也会回传 |
| `X-Dounai-ASR-Status` | `completed \| empty \| unavailable \| not_applicable` | 与事件体 `metadata.asr_status` 一致，便于不看 body 的调用方 |
| `X-Dounai-Provider` | provider id | 实际产出转写的 provider；无转写时为当前 provider |
| `X-Dounai-Model` | string | 模型标签（如有） |
| `X-Dounai-Degraded` | `true/false` | 是否发生了失败/不可用导致的降级 |
| `X-Dounai-Voiceprint-Checked` | `true/false` | 本次是否实际执行了声纹判断 |
| `X-Request-ID` | hex | 日志关联 ID |

降级信息不写入事件，保证契约稳定。

## 3. 查询与模型接口

### `GET /v1/events/{event_id}`

返回该事件，结构与输入响应完全一致。不存在返回 404。

### `GET /v1/providers`

返回所有已启用插件/内置适配器及当前状态：

```json
{
  "providers": [
    {
      "id": "offline",
      "name": "离线直通（无模型）",
      "version": "1.0.0",
      "description": "…",
      "capabilities": ["text", "image", "audio"],
      "privacy": "local",
      "requires_api_key": false,
      "can_transcribe": false,
      "available": true,
      "unavailable_reason": null,
      "homepage": null,
      "active": true,
      "circuit_open": false,
      "consecutive_failures": 0
    },
    {
      "id": "api-1a2b3c4d-qwen3-asr-flash",
      "name": "阿里云百炼 Qwen · qwen3-asr-flash",
      "version": "1.0.0",
      "description": "用户接入的 OpenAI 兼容 API：…",
      "capabilities": ["text", "image", "audio"],
      "privacy": "remote",
      "requires_api_key": true,
      "can_transcribe": true,
      "available": true,
      "unavailable_reason": null,
      "homepage": null,
      "active": false,
      "circuit_open": false,
      "consecutive_failures": 0
    }
  ],
  "status": {
    "active_provider": "offline",
    "fallback_chain": ["local-whisper"],
    "allow_remote_fallback": false,
    "locality_guard": "Remote fallback candidates are skipped unless …",
    "plugin_errors": []
  }
}
```

**不会返回任何密钥**。云端适配器的 `base_url` 会展示以便调试，但密钥只以
AES-256-GCM 密文保存在本机数据库，接口只返回不可逆的 `key_fingerprint`。
`api-<连接短 id>-<模型 slug>` 形式的 provider 来自 API 连接，见下文。

### `PUT /v1/providers/active`

```json
{"provider_id": "api-1a2b3c4d-qwen3-asr-flash"}
```

运行时不重启切换，写入本地 SQLite。未知 provider 返回 404。允许切换到「当前不可用但已安装」的适配器；注册但无密钥的适配器仍会显示在列表里，便于配置和排障。切换是隐私相关的数据流向决定，客户端应向用户明确展示 `privacy: remote`。

### API 连接（BYOK）

本层不内置任何厂商的静态配置。第三方模型要先建立“连接”，连接里再保存一个或多个
模型；每个保存的模型自动成为一个 provider，可像内置 provider 一样切换、降级。

连接与密钥都保存在本机 SQLite。`api_key` 只用于请求厂商，不会出现在任何响应、
日志或导出中。厂商 URL 默认只允许 HTTPS；私网/回环地址需要 `allow_private=true`，
明文 HTTP 需要 `allow_http=true`；云元数据地址始终拒绝，且不跟随重定向。

#### `GET /v1/api-presets`

返回预设厂商（阿里云百炼、OpenAI、OpenRouter、SiliconFlow、本机 Ollama）：

```json
{
  "presets": [
    {
      "id": "dashscope",
      "name": "阿里云百炼 Qwen",
      "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
      "docs_url": "https://bailian.console.aliyun.com/",
      "note": "中文、方言与中英混说场景推荐；使用 qwen3-asr-flash 做语音转写。",
      "recommended_models": ["qwen3-asr-flash", "qwen3.8-omni-flash"],
      "requires_http_permission": false,
      "requires_private_permission": false,
      "recommended": true
    }
  ]
}
```

#### `POST /v1/api-connections/discover`

```json
{
  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
  "api_key": "sk-…",
  "allow_private": false,
  "allow_http": false
}
```

返回 `{base_url, resolved_addresses, models:[{id, owned_by}]}`。只调用厂商的
`GET {base_url}/models`，不会保存任何内容。密钥错误返回 422 `api_auth_failed`；
网络或厂商异常返回 502 `api_discovery_failed`；URL 被策略拒绝返回 422
`invalid_input`。

#### `POST /v1/api-connections/probe`

在上面的字段上增加 `model_id`，逐项实测：

| 能力 | 请求 |
| --- | --- |
| `text` | `POST {base_url}/chat/completions`，一条极短文本 |
| `image` | `POST {base_url}/chat/completions`，`image_url` 为 1×1 PNG |
| `audio` | 先试 `POST {base_url}/audio/transcriptions`，失败再试 `chat/completions` + `input_audio` |

```json
{
  "model_id": "qwen3-asr-flash",
  "capabilities": ["text", "image", "audio"],
  "transcription_protocol": "audio_transcriptions",
  "probes": {
    "text": {"status": "supported", "detail": "chat.completions 返回 200", "latency_ms": 412},
    "image": {"status": "supported", "detail": "image_url 返回 200", "latency_ms": 780},
    "audio": {"status": "supported", "detail": "audio/transcriptions 返回文本", "latency_ms": 1260}
  }
}
```

`status` 为 `supported / unsupported / error / not_tested`；`capabilities` 只包含
`supported` 的项。音频可用时 `transcription_protocol` 为 `audio_transcriptions`
或 `chat_audio`。探测只发送上述合成样本，不会发送用户事件、媒体或声纹。

#### `GET /v1/api-connections`

```json
{
  "connections": [
    {
      "id": "conn_1a2b3c4d5e6f",
      "name": "阿里云百炼 Qwen",
      "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
      "key_fingerprint": "q7Xh…",
      "allow_private": false,
      "allow_http": false,
      "created_at": "2026-11-04T09:30:00Z",
      "updated_at": "2026-11-04T09:30:00Z",
      "models": [
        {
          "provider_id": "api-1a2b3c4d-qwen3-asr-flash",
          "model_id": "qwen3-asr-flash",
          "display_name": "qwen3-asr-flash",
          "capabilities": ["text", "image", "audio"],
          "transcription_protocol": "audio_transcriptions",
          "probes": {"text": {"status": "supported", "detail": "…", "latency_ms": 412}},
          "updated_at": "2026-11-04T09:30:00Z",
          "active": true
        }
      ]
    }
  ]
}
```

`key_fingerprint` 是密钥的不可逆短标识，只用于辨认；响应里永远没有 `api_key`
或密文。

#### `POST /v1/api-connections`

```json
{
  "name": "阿里云百炼 Qwen",
  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
  "api_key": "sk-…",
  "allow_private": false,
  "allow_http": false,
  "models": [
    {
      "model_id": "qwen3-asr-flash",
      "capabilities": ["text", "image", "audio"],
      "transcription_protocol": "audio_transcriptions",
      "probes": {
        "text": {"status": "supported", "detail": "…", "latency_ms": 412},
        "image": {"status": "supported", "detail": "…", "latency_ms": 780},
        "audio": {"status": "supported", "detail": "…", "latency_ms": 1260}
      }
    }
  ]
}
```

`201` 返回连接视图。约束：名称 1–80 字符；`base_url` 最长 512；`api_key`
1–4096 字符；每个连接最多 20 个模型；每个模型至少要有一个 `supported` 能力；
音频可用时必须带 `transcription_protocol`。`capabilities` 可以省略——服务端
始终按 `probes` 重新推导。控制台总是把 `probe` 的结果原样提交；直接调用本接口
时，能力结论由调用方提交（本机单用户信任模型，见
[privacy.md](privacy.md)）。

#### `GET /v1/api-connections/{id}`、`PATCH`、`DELETE`

`PATCH` 可改 `name`、`base_url`、`api_key`（轮换）、`allow_private`、`allow_http`；
省略的字段不变，响应不回显密钥。已有模型的探测结果会保留，改动 `base_url`
后应重新探测对应模型。`DELETE` 删除连接与全部模型；若当前 provider
属于该连接，会自动切回 `offline`，返回
`{deleted, connection_id, models_removed, active_provider}`。

#### `POST /v1/api-connections/{id}/refresh`

用已保存的密钥重新调用 `GET {base_url}/models`，返回
`{connection_id, models:[{id, owned_by}]}`。只发现，不自动更新已保存模型的能力；
能力变化需要重新探测。

#### `POST /v1/api-connections/{id}/models/{model_id}/probe`

用已保存的连接重新探测某个模型；若该模型已保存，能力与探测结果会被覆盖，并立即
重建 provider。

#### `DELETE /v1/api-connections/{id}/models/{provider_id}`

只删除一个模型，路径参数是 `provider_id` 而不是 `model_id`；如果它是当前模型，
会自动回退，`active_provider` 随响应返回。

### `GET /v1/models`

列出可选本地 Whisper 模型、安装状态、大小与许可证描述。**不会**因为该请求下载权重。安装只能通过 CLI 显式执行。

### `GET /v1/providers/status`

仅返回 `GET /v1/providers` 中的 `status` 对象，用于轻量健康检查。

## 4. 隐私接口

### `GET /v1/privacy/policy`

```json
{
  "version": "1.0",
  "sha256": "…64 hex…",
  "text": "# 豆奶输入层声纹功能授权说明…"
}
```

### `GET /v1/privacy/status`

```json
{
  "enabled": false,
  "consent_granted": false,
  "consent_policy_version": null,
  "consent_recorded_at": null,
  "required_policy_version": "1.0",
  "enrolled": false,
  "enrolled_at": null,
  "template_dimension": null,
  "threshold": 0.82,
  "wake_phrase_required": true,
  "local_only": true,
  "policy_version": "1.0",
  "policy_sha256": "…",
  "retention_hours": 72,
  "data": {
    "events": 3,
    "media_files": 2,
    "media_bytes": 481203,
    "voiceprint_templates": 0
  }
}
```

不返回模板内容、原始样本或相似度。

### `PUT /v1/privacy/settings`

```json
{"voiceprint_enabled": true}
```

只控制功能开关。授权和模板仍可能缺失；此时不会做声纹判断。

### `POST /v1/privacy/consent`

```json
{"policy_version": "1.0", "granted": true}
```

- 版本必须等于当前政策，否则 `409 Conflict`。
- 成功会记录带时间戳的授权并开启功能。
- `granted: false` 等同于撤回：删除模板并关闭功能。

### `DELETE /v1/privacy/consent`

撤回授权、删除声纹模板、关闭功能。

### `POST /v1/privacy/voiceprint/enroll`

`multipart/form-data`：

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `files` | 是 | 3–8 个 PCM WAV；每段 1–20 秒，单文件不超过音频上限 |
| `subject` | 否 | 最多 64 字符，默认 `default` |

```bash
curl -X POST http://127.0.0.1:8000/v1/privacy/voiceprint/enroll \
  -F "files=@a.wav;type=audio/wav" \
  -F "files=@b.wav;type=audio/wav" \
  -F "files=@c.wav;type=audio/wav"
```

原始样本只在内存中处理，不写媒体目录；成功响应只含模板维度、样本数、时间和 `raw_samples_persisted: false`。

失败：

- 未开启 → 403 `privacy_disabled`
- 未授权 → 403 `consent_required`
- 样本质量差/格式错 → 422 `invalid_input`

### `DELETE /v1/privacy/voiceprint`

删除模板，保留授权与开关。

### `GET /v1/privacy/export`

返回 `application/zip`，`Content-Disposition: attachment`。压缩包包含：

- `manifest.json` — 导出格式、政策版本/摘要、风险提示
- `data.json` — 设置、事件行、媒体元数据、授权与隐私审计
- `voiceprint.json` — 如已录入，含 base64 float32 模板
- `media/<id>.<ext>` 与 `media-index.json`
- `privacy-policy.md`

导出文件包含生物特征；响应禁止缓存。

### `DELETE /v1/privacy/data?confirm=true`

必须显式传 `confirm=true`，否则 422。删除事件、媒体、声纹模板、授权与审计，并返回删除计数。随后写入一条不含个人内容的删除操作审计记录。

## 5. 健康检查

`GET /healthz` 无需鉴权：

```json
{
  "status": "ok",
  "version": "0.2.0",
  "contract_version": "1.1",
  "active_provider": "offline",
  "config_warnings": [],
  "time_utc": "2026-11-03T09:38:32Z"
}
```

不包含用户数据、密钥或 provider 密钥状态。

## 6. 设备与自有产品接入

任何能发 HTTP 的产品/设备都可以作为来源。推荐：

1. 为设备配置独立 API Token（当前为单实例 Token，非多租户）；
2. 设备将音频/图像/文字经上述接口发送；
3. 用 `source` 标明设备类型或 ID（仅服务端保存，不进入事件契约）；
4. 若设备有自己的模型，部署一个实现 `multipart POST -> {"text":"..."}` 的服务，然后在 `[providers.custom-http]` 配置端点；这样它作为 provider 参与切换与降级。

```bash
curl -X POST https://gateway.example.com/v1/inputs/audio \
  -H "Authorization: Bearer $DOUNAI_API_TOKEN" \
  -F "file=@utterance.wav;type=audio/wav" \
  -F "source=device:living-room" \
  -F "session_id=sess_living_room"
```

设备应只发送实现功能必需的数据，不应上传连续环境录音。输入层不提供设备配对、OAuth 或多租户隔离，这些属于部署方职责。

## 7. 错误格式

除 FastAPI 自动校验外，业务错误统一为下面的信封。`retryable` 与 `request_id`
是 0.2.1 新增的**增量字段**：只读 `code`/`message` 的旧消费方不受影响。

```json
{
  "error": {
    "code": "unsupported_media",
    "message": "Unsupported image format. Use JPEG, PNG, WebP or GIF.",
    "retryable": false,
    "request_id": "6f1c9b0a4e2d4d7ab3f2c1e58d9a0b7c"
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `code` | string | 稳定错误码，见下表 |
| `message` | string | 可展示给用户的摘要；不含厂商正文、密钥或堆栈 |
| `retryable` | boolean | `true` 表示同一请求稍后重试可能成功 |
| `request_id` | string | 与 `X-Request-ID` 相同，用于日志关联 |
| `fields` | array | 仅参数校验失败时出现 |

`retryable` 的判定：

| 场景 | `retryable` |
| --- | --- |
| provider 失败/超时/连接失败（`provider_unavailable`，仅发生在会调用厂商的路径） | `true` |
| 模型发现网络/服务异常（`api_discovery_failed`） | `true` |
| 未预期内部错误（`internal_error`） | `true` |
| 401 / 403 / 404 / 409 / 413 / 415 / 422（含 `api_auth_failed`） | `false` |
| 厂商密钥错误、本地模型缺失等确定性失败 | `false` |

请求参数校验失败时附加 `fields`：

```json
{
  "error": {
    "code": "invalid_request",
    "message": "Request validation failed.",
    "retryable": false,
    "request_id": "…",
    "fields": [{"field": "body.text", "message": "Field required"}]
  }
}
```

| HTTP | code | 场景 |
| --- | --- | --- |
| 401 | `unauthorized` | API Token 缺失或错误 |
| 403 | `consent_required` | 未授权声纹 |
| 403 | `privacy_disabled` | 声纹开关关闭 |
| 403 | `forbidden_origin` | 浏览器跨站写请求 |
| 404 | `not_found` | 事件/provider/媒体不存在 |
| 409 | `conflict` | 政策版本不匹配、provider 冲突、密钥无法解密 |
| 413 | `payload_too_large` | 请求体或文件超过限制 |
| 415 | `unsupported_media` | 魔数/格式不支持 |
| 422 | `invalid_input` / `invalid_request` | 参数、文本、样本、厂商 URL 校验失败 |
| 422 | `api_auth_failed` / `api_connection_error` | 厂商密钥错误或连接不可用 |
| 502 | `api_discovery_failed` | 模型发现时厂商网络/服务异常 |
| 500 | `internal_error` | 未预期内部错误（不返回堆栈） |

模型厂商的错误不会直接变成 5xx 泄漏给客户端；语音场景会降级并仍返回 201。图片/文字不调用模型，因此没有厂商错误。

## 8. 跨域与来源校验

- 未启用 CORS。Web 控制台与 API 同源。
- 所有 `/v1` 的 `POST`/`PUT`/`PATCH`/`DELETE` 若带 `Origin`，必须匹配请求 `Host` 或 `DOUNAI_PUBLIC_BASE_URL` 的主机；`Sec-Fetch-Site: cross-site` 一律拒绝。
- 非浏览器客户端通常不带 `Origin`，不受影响。
- 反向代理必须配置正确的 `Host`/`public_base_url`，否则控制台写请求会被 403。

## 9. 限制速查

| 项 | 默认 |
| --- | --- |
| 文字 | 20,000 字符 |
| 图像 | 15 MiB |
| 音频 | 25 MiB |
| 请求体上限 | `max(图像, 音频) * 8 + 1 MiB`（支持一次最多 8 个声纹样本） |
| 媒体/事件保留 | 72 小时 |
| 单 provider 超时 | 60 秒（可配置） |
| 熔断冷却 | 30 秒 |
| 上下文 | 最近 3 条文本/转写 |
| 声纹样本 | 3–8 段，每段 1–20 秒 |
| 声纹模板 | 80 维 float32 |
| API 连接名称 | 1–80 字符 |
| 每个连接的模型 | 最多 20 个 |
| 厂商 Base URL | 最长 512 字符，必须绝对 URL |
| 厂商 API Key | 1–4096 字符，只以密文入库 |

## 10. 版本与兼容

当前事件契约版本为 **v1.1**：

- `metadata.asr_status` 为 v1.1 新增、Schema 中可选；生产者始终填充。
- 其余字段与 v1.0 保持一致，未新增 `session_id` 到事件体。
- 版本通过 `GET /v1/contract` 的 `x-contract-version`、`GET /healthz` 的
  `contract_version` 暴露。
- v1.1 不新增必需字段，旧 v1.0 消费方仍可解析；新消费方必须检查
  `asr_status`。后续任何新增**必需**字段都视为破坏性变更，必须先升级契约并
  同步下游。

### 10.1 版本协商（契约版本 ≠ 模块版本）

两者是不同概念，不要混用：

| 名称 | 含义 | 当前值 | 出现在 |
| --- | --- | --- | --- |
| 契约版本 | 事件与接口的兼容承诺 | `1.1` | `x-contract-version`、`/healthz.contract_version`、响应头 |
| 模块版本 | 本包的可安装版本 | 见 `/healthz.version` | `/healthz.version`、响应头 |

`GET /v1/contract` 的响应体**始终是纯 JSON Schema**，模块版本不会混入其中；
版本元数据放在响应头与 `/healthz`：

```http
X-Dounai-Contract-Version: 1.1
X-Dounai-Module-Version: <模块版本>
```

所有响应（含错误响应与 `/healthz`）都会带这两个头，消费方不必额外请求即可发现
契约漂移。头缺失按“未知版本”处理，不要硬失败。

兼容策略：

| 情况 | 期望行为 |
| --- | --- |
| 同一主版本（如 `1.0` ↔ `1.1`） | **降级并告警**：按可选字段缺失处理，记录日志即可继续 |
| 主版本不同（如 `1.x` ↔ `2.x`） | **拒绝新契约请求并告警**，或显式降级为已知主版本 |
| 契约版本头缺失 | 视为未知版本，按最保守的已知版本解析并告警 |

原因：v1.1 只新增可选字段，旧 v1.0 消费方仍可解析；主版本升级才会引入破坏性
字段或语义变化。

包版本 **0.2.1** 只增量补错误体与版本头，事件契约仍为 v1.1。**0.2.0** 改变了
provider 的接入方式：静态的 `dashscope` / `openai` provider 与
`DASHSCOPE_API_KEY` / `OPENAI_API_KEY` 已移除，改用运行时的 API 连接
（`/v1/api-connections*`）。已有 `dounai.toml` 中的 `[providers.dashscope]`、
`[providers.openai]` 会被忽略；`[models].fallback` 里的旧 provider id 也会被
跳过，默认只回退到 `local-whisper`。

会话与来源不进入事件体，通过以下响应头稳定回传：

| Header | 出现在 |
| --- | --- |
| `X-Dounai-Session` | 所有输入响应、事件读回 |
| `X-Dounai-Source` | 事件读回 |
| `X-Dounai-Media-Id` | 事件读回（有媒体时） |
| `X-Dounai-ASR-Status` | 所有输入响应、事件读回 |
| `X-Dounai-Contract-Version` | 所有响应（含错误响应） |
| `X-Dounai-Module-Version` | 所有响应（含错误响应） |
