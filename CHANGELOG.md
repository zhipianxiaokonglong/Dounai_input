# Changelog

## [0.2.1] - 2026-11-07

### Added

- 所有响应（含错误响应与 `/healthz`）新增版本头：
  `X-Dounai-Contract-Version`（事件契约版本）与 `X-Dounai-Module-Version`（包版本），
  消费方无需额外请求即可发现契约漂移
- 错误体新增增量字段 `retryable`（是否值得重试）与 `request_id`（与 `X-Request-ID` 一致）；
  参数校验失败仍带 `fields`
- `api.md` 新增「版本协商」小节，明确契约版本 ≠ 模块版本、同主版本降级告警 /
  主版本不同拒绝的消费方策略

### Changed

- `ApiDiscoveryError`（502 `api_discovery_failed`）标记为可重试；未预期 500 标记为可重试；
  鉴权、参数、隐私、来源与冲突类错误明确为不可重试
- 事件契约仍为 v1.1，接口路径、事件字段、数据库结构均未变化

## [0.2.0] - 2026-11-04

### Added

- Web 控制台“API 厂商接入”：填 Base URL + API Key，发现模型、勾选、实测能力后保存；
  一个连接可以保存多个模型，随时切换或移除
- 预设阿里云百炼 / OpenAI / OpenRouter / SiliconFlow / 本机 Ollama，也可自定义 Base URL
- 新增接口：`GET /v1/api-presets`、`POST /v1/api-connections/discover`、
  `POST /v1/api-connections/probe`，以及连接的增删改查、重新发现、模型重新探测与移除
- API Key 以 AES-256-GCM 加密入库；安装 `.[keyring]` 并设置
  `DOUNAI_SECRET_BACKEND=keyring` 后可把主密钥放进系统钥匙串
- 删除全部个人数据时一并清除连接、密文与主密钥

### Changed

- provider 接入从静态环境变量改为运行时 API 连接：移除 `[providers.dashscope]`、
  `[providers.openai]` 与 `DASHSCOPE_API_KEY`、`OPENAI_API_KEY`
- 一个连接可派生多个模型 provider；模型能力由服务端探测（文字 / 图像 / 语音与
  转写协议），不再依赖厂商名硬编码
- SQLite schema 升级为 v3，新增 `api_connections`、`api_models`，自动迁移旧库
- 默认 fallback 只保留 `local-whisper`；动态 API provider 一律按 `remote` 处理
- 内置 HTTP custom provider 保持不变；插件机制不受影响

### Security

- 厂商 URL 校验：默认仅 HTTPS，私网与 HTTP 需显式放行，云元数据地址永久拒绝，
  校验 DNS 解析结果且不跟随重定向
- 密钥只在服务端即时解密，不进入响应、日志或导出；界面只展示不可逆指纹
- 连接对应的 provider 参与既有的远程降级守卫，`allow_remote_fallback=false` 时
  本地失败不会把音频转给云端

### Known limitations

- 仅支持 OpenAI 兼容协议；Azure 原生、Anthropic 原生暂不接入
- 直接调用保存接口时，能力结果是调用方提交的探测结论；控制台始终先调用服务端探测
- 主密钥与数据库同机存放（除非启用 keyring），无法防御同一 OS 用户下的恶意进程

## [0.1.1] - 2026-11-03

### Changed (contract v1.1)

- 事件 `metadata` 新增可选 `asr_status`：`completed | empty | unavailable | not_applicable`
- 语音在无可用 ASR/失败时显式标记 `unavailable` 并返回 `X-Dounai-Degraded: true`，不再静默空文本
- 默认自动生成会话 ID 改为 UUID；会话 ID 在输入响应和事件读回响应头稳定回传
- 新增鉴权媒体交付：`GET /v1/media/{id}/base64`、`GET /v1/events/{id}/media?include_base64=true`
- `public_base_url` 与监听地址明显不一致时启动告警、`/healthz` 暴露 `config_warnings`、doctor 报告
- SQLite schema 升级为 v2，自动迁移旧库并回填 `asr_status`

## [0.1.0] - 2026-10-24

### Added

- 文字、图像、语音三种输入，统一为严格的标准事件契约
- 插件化 provider 适配层：内置 offline、faster-whisper、Qwen3-ASR、OpenAI、自有 HTTP 服务
- 支持 Python entry point 与 `data/plugins/*.py` 的第三方 provider 发现，可独立安装/卸载
- 运行时模型切换、会话上下文、超时、熔断、能力感知降级与隐私守卫
- 可选本地 faster-whisper 模型下载、校验与删除，权重不入仓库
- 基于本地 MFCC 统计的声纹录入与唤醒判断，含明确授权、开关、撤回、删除
- 数据导出 ZIP、全部个人数据删除、72 小时默认保留期
- 零构建 Web 控制台、CLI、Dockerfile、CI
- 用户、开发、API、架构、隐私、部署文档与故障注入测试报告

### Security

- 默认回环绑定；非回环无 Token 拒绝启动
- Bearer 鉴权、常量时间比较、Origin/Sec-Fetch-Site CSRF 防护
- 魔数校验、大小限制、路径穿越防护、媒体能力 URL
- 声纹模板仅存本地 SQLite，永不传给 provider；日志不记录正文/密钥/模板

### Known limitations

- 单实例、无多租户/限流
- 声纹为体验基线，不具备抗重放/抗合成能力，不能用于身份认证
- 事件契约尚未加 `schema_version`
- 无流式 ASR 和异步任务队列
