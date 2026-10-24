# API 参考

基础路径：`/v1`。服务默认监听 `http://127.0.0.1:8000`，交互式 OpenAPI 文档在 `/docs`。

除 `/healthz`、静态控制台与媒体能力 URL 外，设置 `DOUNAI_API_TOKEN` 后所有 `/v1` 请求都需要：

```http
Authorization: Bearer <token>
```

也支持 `X-Dounai-Token: <token>`，供不便设置 `Authorization` 的本地客户端使用。

## 1. 事件契约（冻结）

所有输入最终返回如下结构，**没有额外字段**。机器可读 JSON Schema 位于仓库 `schemas/input-event.schema.json`，也可请求 `GET /v1/contract`。

```json
{
  "event_id": "7e2c664e-420d-4993-91ae-5fac73a9c584",
  "timestamp": "2026-10-24T09:38:37.008Z",
  "modality": "text",
  "content": "今天有一点累，想找人说说话。",
  "metadata": {
    "asr_text": "",
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
| `metadata.voiceprint_event` | `wakeup \| none` | 本地声纹 + 唤醒词判断 |
| `metadata.model_capabilities` | string[] | 当前所选模型声明的能力，按 `text,image,audio` 规范排序 |

`model_capabilities` 表达**模型能力声明**，不是本层接受输入的许可；三种输入在离线模式下也都能生成事件。

### 媒体引用

图像和音频的 `content` 形如：

```text
http://127.0.0.1:8000/v1/media/506608736c6d43368a0089b6e381e31e
```

- 32 位十六进制、128 位不可猜测 ID，属于能力 URL；持有 URL 即可读取，因此不要公开日志或事件。
- 设置 API Token 后，媒体 URL **仍可无 Token 读取**，以便下游 `<img>`/`<audio>` 和媒体管线直接消费。这是有意设计：事件本身只能通过鉴权接口获得。
- 响应带 `Cache-Control: private, no-store` 与 `X-Content-Type-Options: nosniff`。
- 支持 Starlette `FileResponse` 的常规读取语义；媒体 ID 不存在返回 404。

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
| `session_id` | 否 | 1–128 位字母/数字/`._:-`；不传则生成 |
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

转写失败时仍是 `201`，`asr_text=""`，`X-Dounai-Degraded: true`。

### 2.4 语音转写降级与响应头

| Header | 值 | 含义 |
| --- | --- | --- |
| `X-Dounai-Session` | session id | 服务端确认的会话 |
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
    }
  ],
  "status": {
    "active_provider": "offline",
    "fallback_chain": ["local-whisper", "dashscope"],
    "allow_remote_fallback": false,
    "locality_guard": "Remote fallback candidates are skipped unless …",
    "plugin_errors": []
  }
}
```

**不会返回任何密钥**。云端适配器的 `base_url` 会展示以便调试，但密钥只存在服务端环境变量。

### `PUT /v1/providers/active`

```json
{"provider_id": "dashscope"}
```

运行时不重启切换，写入本地 SQLite。未知 provider 返回 404。允许切换到「当前不可用但已安装」的适配器；注册但无密钥的适配器仍会显示在列表里，便于配置和排障。切换是隐私相关的数据流向决定，客户端应向用户明确展示 `privacy: remote`。

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
  "version": "0.1.0",
  "active_provider": "offline",
  "time_utc": "2026-10-24T09:38:32Z"
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

除 FastAPI 自动校验外，业务错误统一：

```json
{
  "error": {
    "code": "unsupported_media",
    "message": "Unsupported image format. Use JPEG, PNG, WebP or GIF."
  }
}
```

请求参数校验失败时附加：

```json
{
  "error": {
    "code": "invalid_request",
    "message": "Request validation failed.",
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
| 409 | `conflict` | 政策版本不匹配、provider 冲突 |
| 413 | `payload_too_large` | 请求体或文件超过限制 |
| 415 | `unsupported_media` | 魔数/格式不支持 |
| 422 | `invalid_input` / `invalid_request` | 参数、文本、样本校验失败 |
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

## 10. 版本与兼容

当前事件契约版本隐含在字段结构中，尚未引入 `schema_version` 字段；新增字段属于破坏性变更。任何扩展必须先升级契约、同步下游并更新 `schemas/input-event.schema.json`，不得在运行时偷偷添加字段。
