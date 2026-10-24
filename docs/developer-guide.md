# 开发指南

本文面向要扩展或维护豆奶输入层的开发者。核心目标是让新 provider、新模型或新来源接入时，**不需要修改 API、事件契约或隐私边界**。

## 1. 设计约束

在写代码前，请先接受以下不变量：

1. **事件契约冻结**：输出只能有 `event_id`、`timestamp`、`modality`、`content`、`metadata.{asr_text,voiceprint_event,model_capabilities}`。任何诊断信息通过 HTTP 响应头、日志或独立管理端点表达。
2. **声纹不出设备**：声纹特征、模板、相似度不得传给 provider，不得写日志，不得写遥测。
3. **能力必须真实声明**：provider 的 `capabilities` 表示模型能处理的输入模态；`can_transcribe` 表示它能否产出语音转写。不要为了出现在下拉框而虚报。
4. **失败不能丢输入**：provider 故障应降级为可用事件，而不是 5xx。
5. **密钥只从环境变量读取**：不写入配置文件、数据库、日志或 API 响应。
6. **核心轻量**：重型模型/厂商依赖放入可选 extra 或独立插件包，核心启动不得下载权重。
7. **数据最小化**：只存实现功能所必需的数据，并遵守保留期。

## 2. 代码结构

```text
src/dounai_input/
├── api.py               FastAPI 路由、鉴权、Origin/CSRF、安全头
├── app.py               ASGI 入口
├── cli.py               命令与本地运维
├── config.py            TOML + 环境变量、默认值、校验
├── events.py            冻结事件模型 + JSON Schema
├── ingest.py            三模态校验、编排、响应头
├── media.py             魔数校验、原子写入、能力 URL
├── store.py             SQLite 持久化、导出、保留期
├── privacy.py           授权、导出、删除、政策
├── voiceprint.py        本地 MFCC 特征、声纹、唤醒判断
├── model_catalog.py     本地模型目录与安装器
├── providers/
│   ├── base.py          Provider 接口、Manifest、Trace 数据类
│   ├── registry.py      内置/entry point/目录插件发现
│   ├── manager.py       选择、降级、隐私守卫、熔断、上下文
│   ├── offline.py       零依赖默认 provider
│   ├── local_whisper.py 可选本地转写
│   ├── openai_compatible.py OpenAI 与 Qwen 两种音频协议
│   └── custom_http.py   自有 HTTP 服务适配器
├── resources/           隐私政策
└── web/                 零构建控制台
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
    async def aclose(self) -> None: ...  # 可选
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

### 3.1 availability

返回 `(False, reason)` 时，管理端会展示 `unavailable_reason`，降级链会跳过该 provider。要求：

- 不发起网络请求（避免列表页泄漏或延迟）；
- 检查依赖、配置和密钥**是否存在**，但不读取/返回密钥内容；
- reason 是面向用户的可操作说明，例如 `Set DASHSCOPE_API_KEY to use this provider.`。

### 3.2 失败语义

- 所有预期失败抛 `ProviderError(message, provider_id=..., retryable=...)`。
- **不要**把厂商响应 body、请求头、API Key、音频转写片段拼进异常，因为它们会进入日志或 API。
- 网络错误、超时、429、5xx 可标记 `retryable=True`；认证错误、参数错误标记 `False`。
- 管理器的熔断器不区分 retryable，仍会短时跳过；错误信息不会返回给客户端。

### 3.3 超时

两层超时：

- `ProviderManager` 用 `asyncio.wait_for` 控制总时长；
- provider 自己的 `httpx.Timeout` 应更细。

本地模型的阻塞推理必须放进 `asyncio.to_thread`，并且**把惰性迭代器消费也放进去**，否则会阻塞事件循环。`local_whisper.py` 是参考实现。

## 4. 新增一个 provider（推荐方式：独立包）

以下示例可运行在 `examples/example-provider/`。它是独立 pip 包，不修改核心。

### 4.1 项目结构

```text
my-provider/
├── pyproject.toml
└── src/
    └── dounai_provider_example/
        └── __init__.py
```

### 4.2 pyproject

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

Entry point **name**决定 `[providers.<name>]` 配置表：

```toml
[providers.example-http]
enabled = true
endpoint = "https://asr.example.com/transcribe"
token_env = "EXAMPLE_ASR_TOKEN"
model = "example-asr"
```

### 4.3 实现

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

### 4.4 安装、验证与卸载

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

卸载时若它还是当前 active provider，启动会自动退回到 `offline`；不会崩溃。未安装的 active provider 会出现在 `providers/status` 的降级说明里。

### 4.5 本地单文件插件（开发期）

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

配置表使用文件名（不含 `.py`）作为键：`[providers.local_echo] enabled = false` 可以禁用。目录插件加载会执行任意本地代码，只应放入你信任的文件。

## 5. 新增本地模型

本地模型由两部分组成：

1. `model_catalog.py` 的 `MODEL_CATALOG` 条目（repo、大小、许可证、描述）；
2. 一个 `BaseProvider` 实现，通常复用 `LocalWhisperProvider`。

如果只是新增 CTranslate2 Whisper 权重，不需要写新 provider：在目录中加入 `ModelSpec` 即可。安装器固定 Hugging Face revision 并记录 SHA-256。不要提交权重文件；`.gitignore` 已排除 `data/`、`models/` 和常见权重后缀。

如果新增完全不同的本地模型（例如 ONNX VAD），建议做成独立插件包，并通过 `availability()` 检查可选依赖与模型文件。核心包不添加该依赖。

## 6. 运行上下文与模型切换

- 会话上下文不是 provider 内部状态，而是本地事件表；切换 provider 不会丢失。
- `ProviderManager` 在每次转写时读取最近 `session_context_events` 条文本/转写，通过 `TranscriptionRequest.context` 传入。
- 不是所有厂商都支持 prompt；例如 Qwen ASR 的 chat 协议没有传上下文，OpenAI 转写会传 `prompt`。**能力差异由 provider 自己处理**，不要修改核心拼接。
- 切换写入 `settings.active_provider`，重启保留。
- 降级链：active → `[models].fallback` 顺序；去重；跳过不支持 audio、不可用、熔断中、被隐私守卫阻止的 provider。
- 隐私守卫：默认任何非 active 的 remote provider 都不允许作为 fallback。允许远程降级必须显式设置 `allow_remote_fallback=true`，并在产品界面告知用户。
- 全部失败：返回空 `asr_text`；`X-Dounai-Degraded: true`；事件仍保存。

## 7. 事件与隐私实现要点

### 7.1 事件

`events.py` 的 Pydantic 模型设置了 `extra="forbid"`，`metadata` 只有三个字段。不要在 provider 或 ingest 中往 dict 里塞诊断字段。若下游需要模型 ID，先讨论契约升级，不要本地扩展。

`model_capabilities` 使用 canonical 排序，且去重。新增能力（如 video）属于契约变更，当前明确不支持。

### 7.2 声纹

- `voiceprint.py` 提取 80 维向量（20 MFCC + 20 delta 的均值/标准差）。
- 原始 WAV 仅在内存中处理；模板写入 SQLite BLOB。
- `VoiceprintService.detect` 任何特征/解码异常都返回 `none`，不阻断音频事件。
- 唤醒要求：开关 + 授权 + 模板 + 相似度阈值 + 唤醒词（可关闭）。
- 声纹阈值和特征是基线，不是安全认证。不要在 README 或产品文案中把它宣传为身份验证。

### 7.3 数据删除/导出

- 任何新表都必须纳入 `Store.export_snapshot()` 和 `delete_all_personal_data()`。
- 任何新落盘文件都必须纳入保留期/删除/孤儿清理。
- 新增敏感字段时更新 `docs/privacy.md` 的数据清单和导出说明。
- 不要绕过 `Store` 直接写 SQLite 到别的文件。

## 8. API 约定

- 路由集中在 `api.py`；业务在 service 层，路由只做参数/异常/响应。
- 新业务端点放在 `/v1` 下，默认继承 Bearer 鉴权。
- 媒体能力 URL 是唯一例外，必须使用高熵 ID 并保持只读。
- 错误用 `DounaiError` 子类；不要把内部异常直接抛给客户端。
- 所有响应加 `no-store`（已由中间件统一处理）。
- 新增写接口必须考虑 CSRF：浏览器跨站写会被 Origin/Sec-Fetch-Site 中间件拒绝。
- 新增上传必须限制大小并用魔数检测，不要信任 `Content-Type` 或文件名。

## 9. 测试

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

当前测试与故障注入结果见 [测试报告](testing-report.md)。

## 10. 代码风格与提交

- Python 3.11+，类型标注优先，`from __future__ import annotations`。
- Ruff 行宽 100；CI 会跑 `ruff check` 与 `ruff format --check`。
- 注释解释“为什么”，不写测试临时备注。
- 提交信息使用清晰的 conventional commits，例如：
  - `feat(providers): add self-hosted HTTP adapter`
  - `fix(privacy): enforce runtime switch during enrollment`
  - `docs(api): document media capability URLs`
- 不提交 `.env`、真实密钥、用户数据、模型权重、测试日志、临时脚本或调试输出。
- 新 provider 的文档至少包括：配置项、密钥变量、能力、隐私类别、失败行为、许可证。

## 11. 发布检查清单

- [ ] `pytest` 全绿，`ruff check`、`ruff format --check` 全绿
- [ ] 事件 schema 与代码一致
- [ ] 新 provider 的能力/隐私声明经过测试
- [ ] 新增数据表/文件已加入导出、删除、保留期
- [ ] 无 `.env`、密钥、权重、日志、临时文件
- [ ] 用户文档和开发文档已更新
- [ ] 版本号与 `CHANGELOG.md` 更新
- [ ] `python -m build` 产物包含 web/resources
- [ ] Docker/本地启动 smoke test 通过
