# Changelog

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
