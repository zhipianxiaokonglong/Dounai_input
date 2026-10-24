# 测试、故障注入与根因分析报告

> 版本：0.1.0
>
> 测试日期：2026-10-24
>
> 范围：豆奶输入层全部代码、接口、隐私流程、provider 降级、插件/模型安装与 Web 控制台
>
> 原则：从根因查起，区分设计缺陷与实现缺陷；记录影响面、修复副作用与未能消除的残余风险。

## 1. 结论摘要

- **自动化测试：113 passed，0 failed**。
- **总覆盖率：79%**（语句/分支综合；`store` 95%、`api` 91%、`events`/`errors` 100%、`manager` 约 90%）。
- **Lint/格式**：`ruff check` 与 `ruff format --check` 全部通过。
- **依赖安全**：`pip-audit` 报告 **No known vulnerabilities found**。
- **静态安全扫描**：Bandit 未发现未处置问题；仅在 `pyproject.toml` 中跳过 B105（把环境变量**名称**误判为硬编码密码）。
- **构建**：sdist 与 wheel 构建成功；wheel 包含 Web/政策资源，不含 tests、data、.env、docs 或模型权重。
- **独立安装**：在全新 venv 安装 wheel，`dounai-input doctor` 可运行。
- **真实本地模型**：下载并校验 `faster-whisper` tiny/small；在 Windows SAPI 合成中文语音上完成“语音 → 本地 ASR → 事件 → 声纹唤醒”的端到端验证。
- **浏览器 E2E**：Edge + Playwright 实际发送文字，校验事件 JSON、无横向溢出；截图生成。
- **未执行**：Docker 容器运行（本机无 Docker）、GPU 推理、真实第三方 API 调用（无 BYOK 密钥）、多租户/高并发压测。它们属于残余风险，不伪装为已测。

最终端到端实测（推荐 `small` 本地模型、默认唤醒词）：

```json
{
  "modality": "audio",
  "metadata": {
    "asr_text": "你好,豆奶,今天天氣不錯,我想和你聊聊天。",
    "voiceprint_event": "wakeup",
    "model_capabilities": ["audio"]
  }
}
```

响应头：`X-Dounai-Degraded: false`、`X-Dounai-Provider: local-whisper`、`X-Dounai-Model: faster-whisper/small`、`X-Dounai-Voiceprint-Checked: true`。

## 2. 环境与方法

| 项 | 值 |
| --- | --- |
| OS | Windows 11 10.0.26200 |
| CPU | AMD Ryzen Threadripper 2990WX（测试机，性能不代表部署基线） |
| Python | 3.12.13 |
| 关键依赖 | FastAPI 0.162.0 / Pydantic 2.14.1 / NumPy 2.5.4 / httpx 0.28.1 |
| 可选依赖 | faster-whisper 1.2.1 / PyAV 18.1.0 / huggingface-hub 1.33.0 |
| 浏览器 | Microsoft Edge headless + Playwright Core |

测试层次：

1. **契约测试**：字段集合、额外字段拒绝、时间/UUID、能力排序、schema 与代码一致性。
2. **API 集成**：文字/图像/语音、媒体读取、错误码、鉴权、Origin、大小限制。
3. **单元测试**：配置、魔数、声纹特征、provider 协议、存储、模型目录。
4. **故障注入**：供应商异常/超时/畸形响应/全失败/熔断/隐私守卫/非 JSON。
5. **隐私生命周期**：授权、撤回、删除、导出、保留期、模板不出设备。
6. **插件/模型**：目录插件、entry point、冲突、禁用、安装失败、校验、卸载。
7. **真实路径**：本地模型下载/加载/推理、SAPI 合成语音、浏览器 E2E。

所有测试媒体均在 `tmp_path` 运行时生成。仓库不提交音频、图片、日志或测试数据库。

## 3. 覆盖矩阵

| 类别 | 用例数 | 关键覆盖 | 结果 |
| --- | ---: | --- | --- |
| 事件契约 `test_contract.py` | 6 | 精确字段、额外字段拒绝、UTC、能力去重、schema 一致 | 通过 |
| 输入 API `test_api_inputs.py` | 14 | 三模态、媒体 URL、415/413/422、事件查询、安全头 | 通过 |
| 鉴权/安全 `test_auth.py` | 8 | Bearer、Token 不回显、能力 URL、CSRF、超大 Content-Length | 通过 |
| 配置 `test_config.py` | 10 | 默认安全值、非回环拒绝、TOML/env/BOM、非法阈值 | 通过 |
| Provider `test_providers.py` | 15 | 能力、可用性、两种 OpenAI 协议、错误脱敏、畸形 JSON、本地模型 | 通过 |
| 降级 `test_fallback.py` | 10 | 异常/超时/全失败/能力/隐私守卫/熔断/上下文/响应头 | 通过 |
| 声纹 `test_voiceprint.py` | 13 | 正负样本、静音、授权、开关、样本不落盘、API 唤醒 | 通过 |
| 隐私 `test_privacy.py` | 11 | 政策、导出 ZIP、撤回、删除、保留期、权限 | 通过 |
| 插件/模型 `test_plugins.py` | 12 | entry point、目录插件、禁用、冲突、安装失败、校验/卸载 | 通过 |
| UI `test_ui.py` | 5 | CSP、静态资源、DOM ID、Node 语法、政策打包 | 通过 |
| CLI `test_cli.py` | 9 | 列表、状态、授权、导出/删除、doctor、错误处理 | 通过 |

## 4. 故障注入结果

| # | 注入 | 期望 | 实际 | 证据 |
| --- | --- | --- | --- | --- |
| F1 | 主 provider 抛 `ProviderError` | 用 fallback，事件仍成功 | `asr_text=fallback`，`degraded=true` | `test_primary_failure_uses_fallback_and_marks_degraded` |
| F2 | 主 provider 延迟超过请求超时 | 取消并切换 fallback | 首个 attempt 为 `timeout`，最终成功 | `test_primary_timeout_uses_fallback` |
| F3 | 所有 provider 失败 | 201 + 空转写 + 降级 | 不再抛 5xx，事件保存 | `test_all_providers_failing_still_returns_an_outcome` |
| F4 | 主 provider 本地失败，fallback 为云端 | 默认不把音频发云 | 云端 attempt 为 `skipped`，原因 privacy guard | `test_locality_guard_blocks_remote_fallback` |
| F5 | 云端 active，另一云端 fallback | 默认仍不跨云 | 默认 skipped；`allow_remote_fallback=true` 时才允许 | `test_remote_to_remote_fallback_requires_explicit_opt_in` |
| F6 | 云端 active 失败，local fallback | 允许本地降级 | 本地成功，标记 degraded | `test_remote_primary_may_fall_back_to_local` |
| F7 | 连续两次失败（冷却期内） | 跳过熔断 provider | 第二次 active 为 skipped，直接 fallback | `test_circuit_breaker_skips_recently_failed_provider` |
| F8 | 厂商 200 但 JSON 缺字段 | ProviderError，安全降级 | 不泄漏 body，fallback/空转写 | `test_malformed_vendor_response_is_a_provider_error` |
| F9 | 厂商返回 HTML | ProviderError | 报「non-JSON」，不解析、不崩溃 | `test_non_json_vendor_response_is_a_provider_error` |
| F10 | 厂商 401 body 含密钥字符串 | 错误不含 body/密钥 | 只提示检查环境变量名 | `test_provider_error_does_not_echo_response_body` |
| F11 | 声纹关闭 / 未授权 / 模板缺失 | `none`，不阻断音频 | 均返回 none；事件成功 | `test_disabled_voiceprint_does_not_detect` 等 |
| F12 | 声纹开启但样本静音 | 422，不保存模板 | 质量错误，模板为空 | `test_silence_is_rejected_as_a_sample` |
| F13 | 保留期到期，但媒体 `created_at` 未过期 | 媒体跟随事件删除 | 事件与文件均删除 | `test_retention_purges_expired_events_and_media` |
| F14 | 跨站浏览器提交 | 403 | Origin 或 Sec-Fetch-Site 被拒 | `test_cross_origin_writes_are_blocked` |
| F15 | 超大 Content-Length | 413，提前拒绝 | 413 | `test_oversized_content_length_is_rejected_before_parsing` |
| F16 | 非图片/非音频伪装扩展名 | 415 | 魔数校验拒绝 | `test_image_rejects_content_that_is_not_an_image`、`test_audio_rejects_invalid_media` |
| F17 | 未安装本地可选依赖 | 清晰提示，不崩溃 | `local-asr` 安装提示 | `test_local_whisper_reports_missing_dependency` |
| F18 | 模型下载 TLS 失败 | 面向用户的错误，不留半成品 | 包装为 ModelInstallError，staging 清理 | `test_install_wraps_tls_failure_with_actionable_message` |
| F19 | provider 可用性检查抛异常 | 服务仍可用 | 标记不可用，status 不 500 | `test_manager_status_survives_availability_exception` |
| F20 | 未安装的 active provider 重启 | 退回 offline，不崩溃 | 初始化逻辑选择 offline | 代码审查 + CLI/doctor 实测 |

## 5. 端到端验证

### 5.1 HTTP 实机

启动独立进程，`/healthz` 200；通过 `httpx` 发送中文文字、PNG 图像、WAV 音频：

- 三条事件均为 201；
- 事件字段严格为 5 个顶层字段；
- 媒体 URL 可读，返回正确 `Content-Type`；
- 未配置模型时音频 `asr_text` 为空、`degraded=false`（offline 本来就不转写，不伪装成失败）。

### 5.2 本地模型

安装命令（需要可选依赖）：

```bash
pip install -e ".[local-asr]"
dounai-input models install small
```

下载结果：

- 模型 revision 固定：`536b0662742c02347bc0e980a01041f333bce120`；
- 权重 SHA-256 记录在 `data/models/whisper-small/dounai-model.json`；
- 权重未进入仓库；
- PyAV 固定 18.1.0。

在 6.59 秒中文 SAPI 音频上（CPU，int8）：

| 指标 | 实测 |
| --- | --- |
| 模型加载 | 2.30 s |
| 推理 | 5.11 s |
| 转写 | 你好,豆奶,今天天氣不錯,我想和你聊聊天。 |
| 端到端事件 | 201，`degraded=false`，`wakeup` |

> 时间仅记录本测试机，不是性能承诺。GPU、CPU 型号、模型档位会显著改变结果。

### 5.3 声纹

- 同一说话人不同语句余弦相似度约 0.95；
- 不同合成说话人约 0.34；
- 阈值 0.82；
- 真实 SAPI 样本 3 段录入 → 同说话人音频 + 默认唤醒词 → `wakeup`；
- 3 段录入的原始样本没有出现于 `media` 表；
- 导出包含 80 维模板，删除/撤回后归零。

### 5.4 浏览器

Edge headless（1440×1200）实际执行：

1. 加载控制台，等待 provider 列表；
2. 输入中文并点击「送入输入层」；
3. 等待事件面板出现；
4. 解析 JSON，校验 `modality`、`content`、`voiceprint_event`；
5. 检查无横向溢出；
6. 截图。

结果：成功。首次截图发现 favicon 404，已修复并复测（控制台无资源错误）。

## 6. 根因分析（重点）

以下问题不是“测试不通过就改断言”，而是定位到代码/设计/依赖层，并说明修复副作用。

### RC-01 声纹特征丢弃频谱均值，导致不同说话人几乎不可分（设计缺陷）

**现象**：初版对 MFCC 做逐句均值归一化（CMVN）后再取均值和标准差。合成 A 与 A 的相似度 0.9981，A 与不同说话人 B 也有 0.9804；阈值无法同时做到低误拒和低误授。

**根因**：归一化抹掉了声道/共振峰的平均位置，而这是说话人区分的主要信息；只保留动态变化，导致所有谐波语音看起来相似。这是**特征设计缺陷**，不是阈值调参问题。

**影响**：授权用户之外的人可能触发 `wakeup`；声纹功能失去意义。

**修复**：保留 MFCC 均值与标准差（80 维），不再做逐句 CMVN；阈值从 0.75 调整为 0.82。修复后同人 0.9556，异人 0.3407。

**副作用**：对信道/麦克风差异更敏感；换设备可能需要重录。已在用户文档和隐私文档中披露，并保留可配置阈值。

**验证**：`test_same_speaker_is_closer_than_different_speaker`、`test_enrollment_and_detection_flow`。

### RC-02 PyAV 19 与 faster-whisper 不兼容，本地 ASR 全部失败（依赖/实现缺陷）

**现象**：真实安装 `faster-whisper 1.2.1` 后，本地转写报 `The local Whisper transcription failed.`，API 仍 201 但 `asr_text` 为空、`degraded=true`。

**根因**：`faster-whisper` 调用 `av.open(..., metadata_errors="ignore")`，PyAV 19 移除了该参数，抛 `TypeError`。项目的 `local-asr` extra 只约束 `av>=11`，解析到 19.0.2。属于**未固定兼容上界**的依赖缺陷；provider 只记录异常类型，进一步掩盖了根因。

**影响**：所有本地语音转写不可用；默认推荐模型路径失效；错误被降级机制“正常化”，用户可能以为模型质量差。

**修复**：在 `local-asr` extra 固定 `av>=11,<19`；在用户文档和部署文档说明；安装器/模型验证路径保持。用 PyAV 18.1.0 复测成功。

**副作用**：无法使用 PyAV 19 的新特性；上游修复后需手动放宽约束。已记录。

**验证**：真实 `faster-whisper` 推理（tiny 与 small）通过；自动化中使用 fake model 验证 worker 行为。

### RC-03 惰性 segment 生成器在事件循环中消费，阻塞服务（实现缺陷）

**现象**：`await asyncio.to_thread(model.transcribe, ...)` 只把“创建生成器”放入线程；`"".join(segment.text for segment in segments)` 实际执行在异步事件循环里，长音频会阻塞所有请求。

**根因**：对 faster-whisper 的惰性迭代语义理解错误。`model.transcribe()` 返回 `(generator, info)`，推理在迭代时发生。

**影响**：本地 ASR 期间健康检查、其他会话、隐私开关请求全部被阻塞；并发下延迟放大。

**修复**：把迭代和拼接放入同一个同步 `run_inference()`，整体用 `asyncio.to_thread` 执行。

**副作用**：无 API 变化；线程内持有模型引用，超时取消只能取消等待，无法中断底层 C 推理。已在残余风险中说明。

**验证**：`test_local_whisper_consumes_segment_generator_in_worker`；真实 small 推理。

### RC-04 运行开关与静态配置混用，授权后仍无法录入（实现缺陷）

**现象**：用户通过 API/控制台授权并开启声纹后，`POST /v1/privacy/voiceprint/enroll` 仍返回 `privacy_disabled`。

**根因**：`PrivacyService.enroll` 检查 `settings.voiceprint_enabled`（启动时的静态默认值）而不是 `VoiceprintService.enabled`（运行时持久化开关）。**同一概念有两份状态源**，运行时状态被忽略。

**影响**：不修改环境变量/重启的用户无法完成核心隐私流程；功能看起来“授权了但没用”。

**修复**：只以运行时服务状态为准；授权会开启功能，开关可独立关闭。静态配置仅做首次默认值。

**副作用**：启动配置不再能“强制关闭”运行时已开启的开关；部署方如需强制，应删除数据或调整运行时状态。文档已说明优先级。

**验证**：`test_api_enrollment_and_wakeup`、`test_enrollment_requires_enabled_switch`。

### RC-05 降级链可能把音频发给用户未选择的第二家云（设计缺陷）

**现象**：初版只限制“本地 active → 云 fallback”；当 active 是云 A、fallback 是云 B 时，仍可能把音频发给云 B。

**根因**：把 fallback 当成纯可用性策略，未把“跨厂商数据流向”作为独立授权决策。用户选择云 A 不等于同意云 B。

**影响**：隐私预期破坏；可能违反用户告知/合同；产生额外费用。

**修复**：任何非 active 的 remote provider 作为 fallback 都必须显式 `allow_remote_fallback=true`；本地 fallback 不限制。默认 false。

**副作用**：云 A 故障时，若没有本地模型且未开启该开关，只能返回空转写；可用性下降是刻意的隐私取舍。文档、API 状态、测试均明确。

**验证**：`test_remote_to_remote_fallback_requires_explicit_opt_in`、`test_locality_guard_blocks_remote_fallback`。

### RC-06 未鉴权 localhost 写接口可被跨站表单利用（设计安全缺陷）

**现象**：默认无 API Token，浏览器恶意页面可用表单向 `127.0.0.1` 提交 multipart 音频，造成未授权写入/转写费用。

**根因**：只考虑了 CORS 读保护，忽略了 HTML 表单可以跨站写；对本地优先服务这是常见盲区。

**影响**：本机用户被恶意网页利用；可能产生音频数据与模型调用费用。

**修复**：所有 `/v1` 写方法校验 `Origin` 必须匹配 `Host` 或 `public_base_url`，并拒绝 `Sec-Fetch-Site: cross-site`。非浏览器客户端通常无 Origin，不受影响。

**副作用**：反向代理必须正确传递 Host 与 `public_base_url`，否则控制台写请求 403；部署文档明确要求。

**验证**：`test_cross_origin_writes_are_blocked`、`test_same_origin_write_is_allowed`、`test_non_browser_write_without_origin_is_allowed`。

### RC-07 模型下载异常直接抛 traceback，用户无法自助（实现缺陷）

**现象**：TLS 校验失败时，CLI 打印 Hugging Face/httpx 的完整 traceback，退出码非预期，且没有可操作提示。

**根因**：`install_model` 只捕获了自己的 `ModelInstallError`，没有把第三方库的网络/TLS 异常转换为领域错误；CLI 只捕获领域错误。

**影响**：普通用户看到陌生堆栈；无法判断是网络、证书还是磁盘问题；日志可能过长。

**修复**：下载/取 revision 的异常统一包装为 `ModelInstallError`，按 TLS/proxy 给出提示，保留 `staging` 清理。CLI 统一输出。

**副作用**：丢失底层 traceback；需要时可通过调试日志/直接在 Python 中复现。错误信息不回显可能含 token 的 URL。

**验证**：`test_install_wraps_tls_failure_with_actionable_message`。实际环境用系统 CA bundle 成功完成下载。

### RC-08 Windows UTF-8 BOM 配置无法解析（实现缺陷）

**现象**：Windows 编辑器保存的 `dounai.toml` 带 UTF-8 BOM，启动报 `Invalid statement (at line 1, column 1)`。

**根因**：直接以二进制交给 `tomllib.load`，而 TOML 规范不允许 BOM；没有做输入清洗。**编码假设未覆盖 Windows 常见编辑器行为。**

**影响**：用户按文档复制/编辑配置后无法启动，错误看起来像语法错误。

**修复**：以 `utf-8-sig` 读取，兼容 BOM；非法 UTF-8 转成清晰配置错误。

**副作用**：无；仍不接受 UTF-16/GBK（明确要求 UTF-8）。

**验证**：`test_utf8_bom_config_is_accepted`、`test_invalid_utf8_config_is_a_clean_error`。

### RC-09 保留期只按媒体自身时间删除，可能留下“事件已删、文件仍在”的孤儿（实现缺陷）

**现象**：代码审查构造“事件超过 72 小时、媒体 `created_at` 较新”的状态，媒体不会被清理，URL 却已无法从事件访问。

**根因**：事件与媒体的保留策略分别按各自 `created_at` 判断，没有使用事件引用关系；存储层缺少一致性不变量。

**影响**：磁盘泄漏；用户以为已删除但文件仍在，违反数据最小化预期。

**修复**：清理媒体时同时匹配“自身过期”或“被过期事件引用”；清理顺序先媒体后事件；保留孤儿清理兜底。

**副作用**：正常同时写入时无差异；引用查询增加少量开销。

**验证**：`test_retention_purges_expired_events_and_media` 特意只改事件时间，验证媒体跟随删除。

### RC-10 前端把 fetch JSON 当 Response 使用，所有控制台发送都会失败（实现缺陷）

**现象**：`api()` 已返回解析后的 JSON，但发送逻辑仍调用 `response.headers` 和 `response.json()`，运行时会抛错。

**根因**：封装函数的返回契约在实现中变化后，调用方没有同步；单元测试没有覆盖浏览器端。属于**接口边界不一致**。

**影响**：Web 控制台核心“发送”功能不可用，尽管 API 正常。

**修复**：输入请求显式传 `raw: true` 获取 Response；其余调用继续拿 JSON；浏览器 E2E 验证。

**副作用**：调用方需要区分 raw 与 JSON；代码中已显式。

**验证**：Playwright 实际输入、点击、等待事件并解析 JSON；截图。

### RC-11 模型目录中的 turbo repo 不存在（数据缺陷）

**现象**：`Systran/faster-whisper-large-v3-turbo` 返回 401/不存在。

**根因**：复制了 tiny/base 的命名模式，未逐个校验上游 repo；目录条目是静态数据，缺少校验。

**影响**：用户选择 turbo 时下载失败。

**修复**：改用 `deepdml/faster-whisper-large-v3-turbo-ct2`（MIT，含所需 CTranslate2 文件）。通过 Hugging Face API 校验了全部 6 个 repo。

**副作用**：依赖社区维护的转换仓库；文档应提示许可证与来源。目录内记录了 repo id 和 license。

**验证**：HF API 六个 repo 全部 200；tiny/small 实际下载成功。

### RC-12 熔断打开时降级标记为 false（实现缺陷）

**现象**：active provider 因冷却被跳过、fallback 成功后，响应头 `degraded` 可能在“没有本次失败”的情况下为 false。

**根因**：`degraded` 只看本次 failed/timeout 和 fallback 使用，没有把“因熔断跳过 active”视为降级。

**影响**：下游/运维误以为主模型正常。

**修复**：把 active 因 paused 被 skip 纳入降级判定。

**副作用**：冷却期内每次请求都会标记降级，符合实际。

**验证**：`test_circuit_breaker_skips_recently_failed_provider` 断言第二次 `degraded=True`。

### RC-13 削波率在归一化之后计算，永远接近 0（实现缺陷）

**现象**：声纹质量对象里的 `clipping_ratio` 基本无意义。

**根因**：先按峰值归一化到 0.95，再统计 `abs >= 0.999`。顺序错误。

**影响**：当前只用于诊断，未参与拒绝策略；但会误导质量评估。

**修复**：在任何增益前统计。

**副作用**：无行为回退；未来若用该指标拒绝样本，需要重新标定阈值。

**验证**：代码审查 + 声纹质量测试。

### RC-14 回环地址用字符串前缀判断，可被外部主机名绕过（安全设计缺陷）

**现象**：`host.startswith("127.")` 会把 `127.0.0.1.nip.io` 这类解析到别处的主机名当成回环地址，从而跳过“非回环必须设置 API Token”的保护。

**根因**：用字符串前缀近似 IP 语义，未解析 IP 地址；把“看起来像回环”当成“确定是回环”。属于**安全校验设计缺陷**。

**影响**：如果用户把 `DOUNAI_HOST` 配成此类域名，服务可能在未鉴权情况下对外可达。

**修复**：使用 `ipaddress.ip_address(...).is_loopback`；无法解析为 IP 的主机名只有 `localhost` 被信任，其余一律按公网处理。IPv6 方括号被规范化。

**副作用**：以前可用的自定义回环主机名（非 localhost）现在需要显式 Token 或 insecure 开关；这是有意的 fail-closed。

**验证**：`test_non_loopback_bind_requires_token` 新增 `127.0.0.1.nip.io`、IPv6、localhost 分支。

## 7. 设计缺陷 vs 实现缺陷

| 编号 | 类型 | 是否影响安全/隐私 | 影响面 | 修复副作用 |
| --- | --- | --- | --- | --- |
| RC-01 | 设计（特征） | 高 | 所有声纹判断 | 信道更敏感 |
| RC-02 | 依赖/实现 | 高（功能） | 所有本地 ASR | 固定旧 PyAV |
| RC-03 | 实现 | 中（可用性） | 本地 ASR 并发 | 不能中断底层推理 |
| RC-04 | 实现 | 中（隐私流程） | 声纹录入 | 配置不再是强制关闭 |
| RC-05 | 设计（隐私） | 高 | 云端降级 | 可用性降低 |
| RC-06 | 设计（安全） | 高 | 未鉴权本地写接口 | 代理配置要求提高 |
| RC-07 | 实现 | 低 | 模型安装体验 | 丢失底层堆栈 |
| RC-08 | 实现 | 低 | Windows 配置加载 | 仅接受 UTF-8 |
| RC-09 | 实现/数据一致性 | 高（删除） | 保留期清理 | 少量查询开销 |
| RC-10 | 实现 | 中（功能） | Web 控制台 | 调用方需区分 raw |
| RC-11 | 数据 | 中（功能） | turbo 下载 | 依赖社区 repo |
| RC-12 | 实现 | 低（可观测） | 降级标记 | 冷却期总标记降级 |
| RC-13 | 实现 | 低（诊断） | 声纹质量字段 | 无 |
| RC-14 | 设计（安全校验） | 高 | 非回环绑定保护 | 自定义回环域名需 Token |

## 8. 为什么不是表面修复

- 声纹问题没有通过“降低阈值”掩盖，而是分析了特征归一化对说话人信息的影响，重新设计特征并做正负样本验证。
- 本地 ASR 没有通过“把异常吞掉”解决，而是定位到 PyAV 上游不兼容，固定版本并在真实模型上验证。
- CSRF 不是加一个前端提示，而是在服务端中间件拒绝跨站写；并补充了反向代理约束。
- 降级隐私问题不是文档提醒，而是改变状态机并加测试；默认关闭远程降级。
- 保留期不是清一下目录，而是修正“事件-媒体”一致性查询并加回归测试。
- 事件契约没有增加诊断字段，而是坚持把诊断放 header，避免破坏下游。

## 9. 残余风险与未测项

| 风险 | 状态 | 建议 |
| --- | --- | --- |
| Docker 容器运行 | **未执行**（本机无 Docker） | 在目标平台执行 `docker build`、健康检查、卷权限测试；CI 当前只构建 Python 包 |
| 真实第三方 API | **未执行**（无 BYOK 密钥） | 用真实 Qwen/OpenAI key 做一次音频抽样；确认地域/Workspace 域名 |
| GPU 推理 | 未执行 | 在 CUDA 环境验证 `device=cuda` 与 `compute_type` |
| 长音频/大并发 | 未压测 | 单实例同步 ASR；高并发需队列/限流 |
| 分块传输绕过 Content-Length | 未完全防护 | 反向代理设置 body 上限；后续可加 ASGI 流式限流 |
| 声纹抗重放/合成 | 未实现 | 明确不作为认证；如需高安全，接入独立 PAS/anti-spoofing 本地模型 |
| 备份中的已删除数据 | 未管理 | 全盘加密与备份生命周期策略 |
| 图片内容安全 | 未处理 | 下游需自行做 NSFW/OCR/安全检测；本层只校验格式 |
| 文本内容安全 | 未处理 | 下游做注入/合规过滤；本层不篡改用户原文 |
| 浏览器录音兼容性 | Edge/Chromium 已验证 | Safari/Firefox 的 MediaRecorder 差异需实测 |
| 部分依赖为未来版本 | 固定上界 | 定期跑 CI 与 `pip-audit`，尤其是 PyAV |

## 10. 复现命令

```bash
# 全量测试 + 覆盖率
pip install -e ".[dev]"
pytest --cov=dounai_input --cov-report=term-missing -q

# 静态检查
ruff check src tests examples
ruff format --check src tests examples
uvx bandit -c pyproject.toml -q -r src
uvx pip-audit

# 构建与包内容
python -m build
python - <<'PY'
import glob, zipfile
names = zipfile.ZipFile(glob.glob("dist/*.whl")[0]).namelist()
assert "dounai_input/web/app.js" in names
assert not any(name.startswith("tests/") for name in names)
print("wheel ok", len(names))
PY

# 本地模型/真实音频（可选）
pip install -e ".[local-asr]"
dounai-input models install small
# 启动并发送 WAV：
dounai-input serve --port 8766
curl -F "file=@voice.wav;type=audio/wav" http://127.0.0.1:8766/v1/inputs/audio
```

浏览器 E2E 的 Playwright 脚本仅在测试机临时目录运行，没有作为依赖或测试数据提交；控制台截图保存在 `docs/assets/console-demo.png`。

## 11. 仓库清洁声明

- 测试使用的音频、图片、数据库、模型权重、日志均在临时目录或 `data/`（已忽略），**不提交**。
- 仓库不含 `.env`、真实 API Key、用户数据、`__pycache__`、测试日志或临时脚本。
- `tests/helpers.py` 的 `StubProvider` 仅供测试导入，不在运行时加载，也没有被注册为内置 provider。
- Docker 构建上下文通过 `.dockerignore` 排除 tests/docs/data/models。
- 模型下载只通过显式 CLI 安装，仓库不含权重大文件。
