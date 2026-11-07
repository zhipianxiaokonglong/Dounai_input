# 开发指南

本文面向要扩展或维护豆奶输入层的开发者。核心目标是让新厂商、新模型或新来源接入时，
**不需要修改 API、事件契约或隐私边界**。

从 0.2.0 起，OpenAI 兼容厂商优先通过运行时的 **API 连接**接入：用户填 Base URL + API Key，
服务发现模型并逐项实测能力，保存后自动成为 provider。写 adapter 只用于协议不兼容或需要特殊
鉴权的服务。

## 1. 设计约束

在写代码前，请先接受以下不变量：

1. **事件契约版本化**：输出只能有 `event_id`、`timestamp`、`modality`、`content`、`metadata.{asr_text,asr_status,voiceprint_event,model_capabilities}`。`asr_status` 是 v1.1 新增的可选 Schema 字段，生产者必须填充；不允许在运行时添加其他字段。会话、来源、降级等诊断信息通过 HTTP 响应头、日志或独立管理端点表达。
2. **声纹不出设备**：声纹特征、模板、相似度不得传给 provider，不得写日志，不得写遥测。
3. **能力必须真实声明**：内置/插件 provider 的 `capabilities` 由代码声明；API 连接模型的能力只来自实测探测，不能按模型名猜，也不要为了出现在下拉框而虚报。
4. **失败不能丢输入**：provider 故障应降级为可用事件，而不是 5xx。
5. **密钥只进加密存储**：API 连接的密钥只经 `SecretStore` 加密后写 SQLite；插件密钥从环境变量读取。任何密钥都不得写入配置、日志、导出或 API 响应。
6. **出站 URL 必须过策略**：任何用户提供的 Base URL 都要走 `validate_base_url`，不要自己拼 URL、跟随重定向或直接 `httpx.get`。
7. **核心轻量**：重型模型/厂商依赖放入可选 extra 或独立插件包，核心启动不得下载权重。
8. **数据最小化**：只存实现功能所必需的数据，并遵守保留期。

## 2. 代码结构

```text
src/dounai_input/
├── api.py               FastAPI 路由、鉴权、Origin/CSRF、安全头
├── app.py               ASGI 入口
├── cli.py               命令与本地运维
├── config.py            TOML + 环境变量、默认值、校验
├── events.py            版本化事件模型 + JSON Schema
├── ingest.py            三模态校验、编排、响应头
├── media.py             魔数校验、原子写入、能力 URL
├── store.py             SQLite 持久化、API 连接、导出、保留期
├── privacy.py           授权、导出、删除、政策
├── api_access.py        URL/SSRF 策略、模型发现、能力探测、厂商预设
├── api_connections.py   连接生命周期、密钥编排、动态 provider 物化
├── secrets.py           AES-256-GCM 主密钥（文件/钥匙串）与指纹
├── voiceprint.py        本地 MFCC 特征、声纹、唤醒判断
├── model_catalog.py     本地模型目录与安装器
├── providers/
│   ├── base.py          Provider 接口、Manifest、Trace 数据类
│   ├── registry.py      内置/entry point/目录插件发现、动态注册/注销
│   ├── manager.py       选择、降级、隐私守卫、熔断、动态生命周期
│   ├── offline.py       零依赖默认 provider
│   ├── local_whisper.py 可选本地转写
│   ├── openai_compatible.py 通用 OpenAI 兼容适配器（两种音频协议）
│   └── custom_http.py   自有 HTTP 服务适配器
├── resources/           隐私政策
└── web/                 零构建控制台（含“API 厂商接入”对话框）
```

请求主路径：

```text
multipart/json
   │
   ▼
api.py ──► ingest.py ──► media.py (validate/store)
                │
                ├──► ProviderManager ──► BaseProvider.transcribe
                │          │
                │          └── fallback chain / privacy guard / circuit breaker
                │
                ├──► VoiceprintService (local, memory only)
                │
                └──► Store.save_event ──► InputEvent (JSON)
```

API 连接的旁路（不经过 ingest）：

```text
api.py ──► ApiConnectionService ──► api_access.py (validate/discover/probe)
                │        │
                │        └──► Store(api_connections · api_models)
                │                     │
                │                     └── SecretStore (AES-GCM)
                └──► ProviderManager.register_dynamic ──► api-* provider
```

`services.create_services()` 是唯一的组装点；`build_app()` 组装应用时还会调用一次
`api_connections.resync()`，确保数据库里的连接先于请求变成 provider。

## 3. Provider 接口

`src/dounai_input/providers/base.py`：

```python
@dataclass(frozen=True)
class ProviderManifest:
    id: str
    name: str
    version: str
    description: str
    capabilities: tuple[str, ...]   # text | image | audio
    privacy: str                    # local | remote
    requires_api_key: bool = False
    can_transcribe: bool = False
    homepage: str | None = None


class BaseProvider(abc.ABC):
    manifest: ProviderManifest

    def __init__(self, config: Mapping[str, Any] | None = None) -> None: ...
    def availability(self) -> tuple[bool, str | None]: ...
    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult: ...
    async def aclose(self) -> None:  # 可选
    def info(self) -> dict[str, Any]: ... # 默认实现，可覆盖补充 model/base_url
```

`TranscriptionRequest` 只暴露：

```python
path: Path                 # 本地临时/媒体文件
mime: str
language: str | None
context: tuple[str, ...]   # 最近会话文本/转写，最多 settings.session_context_events
timeout_seconds: float
```

`request.safe_context()` 返回按字符截断的上下文，不要把整段会话原样塞进 prompt。

`TranscriptionResult`：

```python
text: str
provider_id: str
model: str | None
language: str | None
```

**两类构造方式**：

- 内置/插件：`BaseProvider.__init__(config)`，配置来自 `[providers.<id>]`；
- API 连接：`ApiConnectionService` 直接构造 `OpenAICompatibleProvider(provider_id=..., base_url=..., model=..., api_key=..., protocol=..., capabilities=...)`，不读配置、不读环境变量。不要在 adapter 里重新引入厂商静态 profile。

### 3.1 availability

返回 `(False, reason)` 时，管理端会展示 `unavailable_reason`，降级链会跳过该 provider。要求：

- 不发起网络请求（避免列表页泄漏或延迟）；
- 检查依赖、配置和密钥**是否存在**，但不读取/返回密钥内容；
- reason 是面向用户的可操作说明，例如 `Set providers.example-http.endpoint.` 或
  `Re-save the connection API key.`。

### 3.2 失败语义

- 所有预期失败抛 `ProviderError(message, provider_id=..., retryable=...)`。
- **不要**把厂商响应 body、请求头、API Key 或音频转写片段拼进异常，因为它们会进入日志或 API。
- 网络错误、超时、429、5xx 可标记 `retryable=True`；认证错误、参数错误标记 `False`。
- 管理器的熔断器不区分 retryable，仍会短时跳过；错误信息不会返回给客户端。
- 401/403 一律映射为“检查 Key 与端点”，不要回显厂商原文。

### 3.3 超时

两层超时：

- `ProviderManager` 用 `asyncio.wait_for` 控制总时长；
- provider 自己的 `httpx.Timeout` 应更细（API 连接默认单次 60 秒，探测默认 20 秒）。

本地模型的阻塞推理必须放进 `asyncio.to_thread`，并且**把惰性迭代器消费也放进去**，否则会阻塞
事件循环。`local_whisper.py` 是参考实现。

## 4. 接入厂商：优先用 API 连接

### 4.1 什么时候不用写代码

满足以下全部条件时，直接用 API 连接，不需要 adapter：

- 服务提供 OpenAI 兼容的 `GET /models` 与 `POST /chat/completions`；
- 音频（用于转写时）要么走 `POST /audio/transcriptions`（multipart），要么走
  `/chat/completions` 的 base64 `input_audio`；
- 鉴权是 `Authorization: Bearer <key>`；
- 不需要特殊签名、mTLS、代理或自定义 CA。

这是本项目的默认路径：用户填 URL 与 Key → 发现模型 → 逐项探测 → 保存 → 在 provider 列表里
切换。控制台的“API 厂商接入”对话框调用的就是同一组接口；直接 curl 也一样。预设
（`GET /v1/api-presets`）只是 UI 便捷入口，不写数据库，也不改变 URL/Key 的校验规则。

### 4.2 发现与探测契约

**发现**：`GET {base_url}/models`，带 Bearer。接受：

```json
{"data": [{"id": "qwen3-asr-flash", "owned_by": "aliyun"}]}
```

也接受 `models` 键、字符串数组元素；去重、最多 500 个、响应体上限 2 MB；不跟随重定向。

**探测**：`POST /chat/completions`（text）、`POST /chat/completions` + `image_url`（image）、
`POST /audio/transcriptions` → 失败再 `POST /chat/completions` + `input_audio`（audio）。
只用合成 1×1 PNG 和 0.8 秒合成 WAV，绝不发送用户事件/媒体/声纹。

```json
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
```

| 字段 | 规则 |
| --- | --- |
| `status` | `supported` / `unsupported` / `error` / `not_tested` |
| `capabilities` | 只取 `supported` 项，由服务端按 probes 重算 |
| `transcription_protocol` | `audio_transcriptions` 或 `chat_audio`；audio 为 `supported` 时必需 |
| 保存条件 | 每个模型至少一项 `supported`；每个连接最多 20 个模型 |
| 探测短路 | text 鉴权失败时不再探测 image/audio，避免用坏 Key 反复打厂商 |

错误映射：URL 被策略拒绝 → 422 `invalid_input`；Key 错误 → 422 `api_auth_failed`；网络/厂商
异常 → 502 `api_discovery_failed`。厂商响应体不会回显，只给安全说明。

### 4.3 保存后发生什么

`ApiConnectionService` 是唯一把连接变成 provider 的地方：

1. 校验 URL（`validate_base_url`，带连接级 `allow_private`/`allow_http`）与 Key（非空、≤4096、无控制字符）；
2. 加密 Key（`SecretStore.encrypt(..., purpose="api-connection")`），写 `api_connections`；
3. 逐个模型写 `api_models`，`provider_id` 由服务端生成：以 `api-` 开头、只含 `[a-z0-9-]`、最长 48 字符（通常是 `api-<连接 id 后 8 位>-<模型 slug>`）；客户端不能指定；
4. 失败时删除刚插入的连接行（回滚），不留下半条记录；
5. 调用 `_sync_providers()`：为每个模型构造 `OpenAICompatibleProvider` 并
   `register_dynamic`；不存在的 `api-*` 一律 `unregister_dynamic`；active 失效则回退。

其他入口：

- `PATCH`：改名/换 URL/轮换 Key/改权限开关；省略字段不变；已有探测结果保留，换 URL 后应重新探测；
- `refresh`：用已存 Key 重新发现，只返回列表，不自动改已保存模型；
- `models/{model_id}/probe`：重测并覆盖已有模型的能力，立即重建 provider；
- `delete_model` / `delete_connection`：注销 provider；若它是 active，回退到 `offline` 或
  `[models].fallback`；删除最后一个连接后删除主密钥；
- 全量删除：清空 `api_models`、`api_connections`，resync，再删主密钥；
- Key 无法解密（主密钥丢失或被替换）时，`resync()` 只注销 provider，不会自动重写 active；
  需要重新保存连接密钥或手动切换，否则 active 会指向一个已注销的 id（转写仍走 fallback 链，
  但状态显示为不可用）。

### 4.4 什么时候需要写 adapter

- 协议不是 OpenAI 兼容（自定义 JSON、WebSocket、异步任务轮询）；
- 需要签名（AWS SigV4、HMAC）、mTLS、企业代理或私有 CA；
- 音频格式/字段差异无法用现有两种协议表达；
- 需要本地模型或非 HTTP 推理。

此时按第 5 节写独立插件包，**不要**改 `api_connections.py` 或内核。反过来，也不要为了一个
普通 OpenAI 兼容厂商新增 adapter——那会重新引入静态厂商配置和密钥管理负担。

### 4.5 安全红线

- 任何新入口只要接受 URL，必须调用 `validate_base_url(...)`，并原样传递
  `allow_private`/`allow_http`；不要自己判断私网或 scheme。
- 不要把 Key 放进 URL、query、日志、异常、导出或响应；只返回 `key_fingerprint`。
- 解密只发生在需要调用厂商的那一刻；不要在 `list`/`get` 的序列化路径解密（变更时由
  `_sync_providers()` 统一解密并构造 provider）。
- 探测只用合成数据；不要为了“测得更准”发送用户音频。
- 提交的探测结果由调用方给出，服务端按状态重算能力但不重放探测——这是本机单用户信任模型。
  控制台回传的 `probes` 来自服务端 `/probe` 的响应，而不是浏览器自行判断；若要支持多用户/
  公网部署，必须在服务端重新探测，不能沿用客户端结论。
- `provider_id` 必须服务端生成，不能接受客户端指定的 id，否则可能覆盖内置 provider。

## 5. 写一个 provider 适配器（独立包）

以下示例可运行在 `examples/example-provider/`。它是独立 pip 包，不修改核心。

### 5.1 项目结构

```text
my-provider/
├── pyproject.toml
└── src/
    └── dounai_provider_example/
        └── __init__.py
```

### 5.2 pyproject

```toml
[build-system]
build-backend = "hatchling.build"
requires = ["hatchling>=1.27"]

[project]
name = "dounai-provider-example"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["dounai-input>=0.1.0", "httpx>=0.28,<1"]

[project.entry-points."dounai_input.providers"]
example-http = "dounai_provider_example:ExampleHttpProvider"
```

Entry point **name** 决定 `[providers.<name>]` 配置表：

```toml
[providers.example-http]
enabled = true
endpoint = "https://asr.example.com/transcribe"
token_env = "EXAMPLE_ASR_TOKEN"
model = "example-asr"
```

### 5.3 实现

```python
from collections.abc import Mapping
from typing import Any

from dounai_input.errors import ProviderError
from dounai_input.providers.base import (
    BaseProvider,
    ProviderManifest,
    TranscriptionRequest,
    TranscriptionResult,
)


class ExampleHttpProvider(BaseProvider):
    manifest = ProviderManifest(
        id="example-http",
        name="Example self-hosted ASR",
        version="0.1.0",
        description="把音频转发到自建服务。",
        capabilities=("audio",),
        privacy="remote",
        requires_api_key=True,
        can_transcribe=True,
    )

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        super().__init__(config)
        self.endpoint = str(self.config.get("endpoint", ""))

    def availability(self) -> tuple[bool, str | None]:
        if not self.endpoint:
            return False, "Set providers.example-http.endpoint."
        return True, None

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        if not self.availability()[0]:
            raise ProviderError("Not configured.", provider_id=self.manifest.id)
        # 这里实现真正的 HTTP 调用。要点：
        # 1. request.path.read_bytes() 用 asyncio.to_thread；
        # 2. httpx.AsyncClient(timeout=...)，follow_redirects=False；
        # 3. 错误只抛安全消息，不回显 body；
        # 4. 校验厂商 JSON。
        return TranscriptionResult(
            text="...",
            provider_id=self.manifest.id,
            model="example-asr",
            language=request.language,
        )
```

完整实现见 [examples/example-provider](../examples/example-provider/src/dounai_provider_example/__init__.py)。

### 5.4 安装、验证与卸载

```bash
pip install -e examples/example-provider
export EXAMPLE_ASR_TOKEN="..."
dounai-input serve
dounai-input providers list
dounai-input providers activate example-http
```

重启后 provider 才会被发现（插件在启动时加载，不在请求时执行任意包导入）。

```bash
pip uninstall dounai-provider-example
```

卸载时若它还是当前 active provider，启动会自动退回到 `offline`；不会崩溃。未安装的 active
provider 会出现在 `providers/status` 的降级说明里。

### 5.5 本地单文件插件（开发期）

也可以把 `*.py` 放入 `data/plugins/` 或 `DOUNAI_PLUGIN_PATH`，模块级变量 `PROVIDER` 指向类或工厂：

```python
# data/plugins/local_echo.py
from dounai_input.providers.base import BaseProvider, ProviderManifest, TranscriptionResult


class Echo(BaseProvider):
    manifest = ProviderManifest(...)

    def availability(self):
        return True, None

    async def transcribe(self, request):
        return TranscriptionResult(text="…", provider_id="local-echo")


PROVIDER = Echo
```

配置表使用文件名（不含 `.py`）作为键：`[providers.local_echo] enabled = false` 可以禁用。
目录插件加载会执行任意本地代码，只应放入你信任的文件。不要在插件里用 `api-` 前缀做 id：该
前缀保留给运行时连接，服务端会注销不再对应任何保存模型的 `api-*` provider。

## 6. 新增本地模型

本地模型由两部分组成：

1. `model_catalog.py` 的 `MODEL_CATALOG` 条目（repo、大小、许可证、描述）；
2. 一个 `BaseProvider` 实现，通常复用 `LocalWhisperProvider`。

如果只是新增 CTranslate2 Whisper 权重，不需要写新 provider：在目录中加入 `ModelSpec` 即可。
安装器固定 Hugging Face revision 并记录 SHA-256。不要提交权重文件；`.gitignore` 已排除
`data/`、`models/` 和常见权重后缀。

如果新增完全不同的本地模型（例如 ONNX VAD），建议做成独立插件包，并通过 `availability()`
检查可选依赖与模型文件。核心包不添加该依赖。

## 7. 运行上下文与模型切换

- 会话上下文不是 provider 内部状态，而是本地事件表；切换 provider 不会丢失。
- `ProviderManager` 在每次转写时读取最近 `session_context_events` 条文本/转写，通过
  `TranscriptionRequest.context` 传入。
- 不是所有厂商都支持 prompt；例如 Qwen ASR 的 chat 协议没有传上下文，OpenAI 转写会传
  `prompt`。**能力差异由 provider 自己处理**，不要修改核心拼接。
- 切换写入 `settings.active_provider`，重启保留；值可以是内置 id，也可以是 `api-*`。
- 降级链：active → `[models].fallback` 顺序；去重；跳过不支持 audio、不可用、熔断中、被隐私守卫阻止的 provider。`[models].fallback` 默认只列内置 provider；`api-*` id 随连接创建/删除而出现/消失，不建议写进配置文件以免悬空。
- 隐私守卫：默认任何非 active 的 remote provider 都不允许作为 fallback，`api-*` 的
  `privacy` 恒为 `remote`，因此同样受限。允许远程降级必须显式设置
  `allow_remote_fallback=true`，并在产品界面告知用户。
- 全部失败：返回空 `asr_text`；`X-Dounai-Degraded: true`；事件仍保存。
- 删除连接/模型会让 active 失效：`fallback_active()` 先找 `offline`，再按 `[models].fallback`
  找已注册 provider；结果写回 `settings.active_provider` 并记审计。

## 8. 事件与隐私实现要点

### 8.1 事件

`events.py` 的 Pydantic 模型设置了 `extra="forbid"`，`metadata` 只有四个字段（v1.1 增加了
`asr_status`）。不要在 provider 或 ingest 中往 dict 里塞诊断字段。若下游需要模型 ID 或
session，先讨论契约升级，不要本地扩展；session/source 通过响应头回传。

`asr_status` 由 `ingest.py` 根据 provider 结果计算：

- 非空文本 → `completed`；
- provider 成功但空文本 → `empty`；
- 无可用 provider / 失败 / 选中模型不能转写 → `unavailable`；
- 文字/图像 → `not_applicable`。

`model_capabilities` 使用 canonical 排序，且去重。新增能力（如 video）属于契约变更，当前明确不支持。

### 8.2 声纹

- `voiceprint.py` 提取 80 维向量（20 MFCC + 20 delta 的均值/标准差）。
- 原始 WAV 仅在内存中处理；模板写入 SQLite BLOB。
- `VoiceprintService.detect` 任何特征/解码异常都返回 `none`，不阻断音频事件。
- 唤醒要求：开关 + 授权 + 模板 + 相似度阈值 + 唤醒词（可关闭）。
- 声纹阈值和特征是基线，不是安全认证。不要在 README 或产品文案中把它宣传为身份验证。

### 8.3 数据删除/导出与密钥

- 数据库 schema 当前为 **v3**：v2 增加 `events.asr_status`，v3 增加 `api_connections` 与
  `api_models`。新增列必须提供可重入迁移并保留旧行可读；不要只改 `CREATE TABLE`，因为已有
  部署不会重建表。
- 任何新表都必须进入导出与删除路径：通用快照走 `Store.export_snapshot()`，ZIP 导出在
  `PrivacyService.export_zip()` 追加，删除走 `delete_all_personal_data()`。API 连接目前由
  `export_zip()` 追加，且**只导出元数据**：id、名称、base_url、密钥指纹、模型 id/能力/协议；
  绝不导出密文、nonce 或明文 Key。
- 主密钥不在数据库里：默认 `data/.secret.key`（项目尽力设 0600，目录 0700），或安装
  `.[keyring]` 并设 `DOUNAI_SECRET_BACKEND=keyring` 后存系统钥匙串。两者都不进入导出。
- 删除最后一个连接或执行全量删除时，必须调用 `SecretStore.delete_master_key()`，同时清理
  文件与钥匙串中的副本。
- 任何新落盘文件都必须纳入保留期/删除/孤儿清理。
- 新增敏感字段时更新 `docs/privacy.md` 的数据清单和导出说明。
- 不要绕过 `Store` 直接写 SQLite 到别的文件。

## 9. API 约定

- 路由集中在 `api.py`；业务在 service 层，路由只做参数/异常/响应。
- 新业务端点放在 `/v1` 下，默认继承 Bearer 鉴权。
- 媒体能力 URL 是唯一例外，必须使用高熵 ID 并保持只读。
- 错误用 `DounaiError` 子类；不要把内部异常直接抛给客户端。
- 错误体统一由 `api.py::_error_json` 生成，必须包含 `retryable` 与 `request_id`；
  新增错误类时显式声明类级 `retryable`（默认 `False`，仅瞬时故障才为 `True`）。
- 所有响应都必须带 `X-Dounai-Contract-Version` 与 `X-Dounai-Module-Version`，
  不要手工设置；中间件已统一注入，`/v1/contract` 响应体保持纯 JSON Schema。
- 所有响应加 `no-store`（已由中间件统一处理）。
- 新增写接口必须考虑 CSRF：浏览器跨站写会被 Origin/Sec-Fetch-Site 中间件拒绝。
- 新增上传必须限制大小并用魔数检测，不要信任 `Content-Type` 或文件名。
- API 连接相关端点：
  - 请求模型用 `extra="forbid"`，新增字段必须同步 `docs/api.md` 与控制台；
  - 任何响应都不得包含 `api_key`、密文或 nonce，只回 `key_fingerprint`；
  - `discover`/`probe` 是“临时凭据”接口，不得写库、不得记 Key；
  - `POST /v1/api-connections` 失败必须回滚连接行；
  - `provider_id` 由服务端生成，路径里的 `provider_id` 只用于删除/定位，不接受客户端创建；
  - 连接级 `allow_private`/`allow_http` 必须原样传给 `validate_base_url`，不能被全局开关悄悄放宽。

## 10. 测试

```bash
pip install -e ".[dev]"
pytest -q
ruff check src tests
ruff format --check src tests
```

测试原则：

- 不依赖真实厂商网络；用 `httpx.MockTransport` 注入 wire-level 响应。
- 不提交测试音频/图像/日志；在 `tmp_path` 生成。
- provider 失败、超时、畸形 JSON、401、全部失败、隐私守卫、熔断、上下文切换都必须有覆盖。
- 新增 provider 至少覆盖：`availability`、成功路径、超时/HTTP 错误、畸形响应、密钥不泄漏。
- 涉及声纹的改动必须覆盖正样本、负样本、无授权、关闭开关、删除模板。
- 任何契约改动必须同时更新 `schemas/input-event.schema.json` 和 `docs/api.md`。

改动 API 连接或密钥时，按下表补齐 `tests/test_api_connections.py` 的覆盖：

| 范围 | 必须覆盖 |
| --- | --- |
| 密钥 | 加解密往返、随机 nonce、密文不含明文、主密钥轮换后解密失败、指纹不可逆 |
| 钥匙串 | `file` / `keyring` / `auto` 选择、文件迁移到钥匙串、切回文件可恢复、依赖缺失报错、未知后端拒绝、删除主密钥两边都清 |
| URL/SSRF | 非 http(s)、userinfo、query/fragment、私网/回环、明文 HTTP、元数据地址、DNS 解析到私网/元数据、重定向不跟随 |
| 发现 | Bearer 鉴权、`data`/`models` 两种形状、字符串元素、去重/截断、401 安全报错、非 JSON、超大响应 |
| 探测 | text/image、`audio_transcriptions`、回退 `chat_audio`、不支持时不按模型名猜、鉴权短路、超时是 error 不是崩溃 |
| 连接 | 加密入库且注册 provider、API 不回显 Key、必须有 supported 能力、audio 必须有协议、PATCH 轮换 Key、删除回退、只删模型、导出/全量删除联动、配置里没有静态厂商 |
| 健壮性 | Key 无法解密时注销旧 provider、SSRF 在 API 层被拒、私网+HTTP 需显式开关、`discover`/`probe` 不落库、预设可用 |

- DNS 测试用注入的 `resolver`，不要依赖真实域名解析；持久化读取用
  `resolve_dns=False`，避免测试被网络拖住。
- 钥匙串测试用假后端，不要读写开发机的真实钥匙串。
- 探测断言只看 `status`/`transcription_protocol`/`latency_ms` 形状，不断言厂商文案。
- 新增连接字段：补迁移、导出、删除三组测试。

当前测试与故障注入结果见 [测试报告](testing-report.md)。

## 11. 代码风格与提交

- Python 3.11+，类型标注优先，`from __future__ import annotations`。
- Ruff 行宽 100；CI 会跑 `ruff check` 与 `ruff format --check`。
- 注释解释“为什么”，不写测试临时备注。
- 提交信息使用清晰的 conventional commits，例如：
  - `feat(api): add OpenAI-compatible API connections`
  - `feat(providers): add self-hosted HTTP adapter`
  - `fix(secrets): keep master key out of exports`
  - `docs(api): document connection discovery and probes`
- 不提交 `.env`、真实密钥、用户数据、模型权重、测试日志、临时脚本或调试输出。
- 新厂商文档分两种：
  - 能用 API 连接接入：只写 Base URL、权限开关、推荐模型、计费/限流提示，不需要密钥变量；
  - 需要 adapter：写配置项、密钥变量、能力、隐私类别、失败行为、许可证。

## 12. 发布检查清单

- [ ] `pytest` 全绿，`ruff check`、`ruff format --check` 全绿
- [ ] 事件 schema 与代码一致
- [ ] 新厂商优先验证能否用 API 连接接入；确实需要 adapter 才新增代码
- [ ] 连接端点不回显 `api_key`/密文；日志、异常、导出无明文
- [ ] URL 策略（HTTPS/私网/HTTP/元数据/DNS）有测试，且未绕过 `validate_base_url`
- [ ] 删除连接/模型后 active 回退正确，最后一个连接删除主密钥
- [ ] 新增 provider 的能力/隐私声明经过测试
- [ ] 新增数据表/文件已加入导出、删除、保留期
- [ ] 无 `.env`、密钥、权重、日志、临时文件
- [ ] 用户文档和开发文档已更新
- [ ] 版本号与 `CHANGELOG.md` 更新
- [ ] `python -m build` 产物包含 web/resources
- [ ] Docker/本地启动 smoke test 通过
