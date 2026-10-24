# 贡献指南

感谢你愿意改进豆奶输入层。这个项目面向外部开发者阅读，代码、测试和文档同样重要。

## 1. 开发环境

```bash
git clone https://github.com/zhipianxiaokonglong/Dounai_input.git
cd Dounai_input

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

pytest -q
ruff check src tests
ruff format --check src tests
```

Windows PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

本地 ASR 开发可选：

```bash
pip install -e ".[dev,local-asr]"
dounai-input models install tiny --yes
```

## 2. 修改前

- 先读 [架构说明](docs/architecture.md) 和 [开发指南](docs/developer-guide.md)；
- 事件契约和隐私边界属于高影响改动，请在 issue 中先说明动机；
- 新增 provider 优先做成独立插件包，除非它是项目维护的核心适配器；
- 新增数据表/文件必须同时更新导出、删除和保留期逻辑；
- 不要把生物特征、原始音频、API Key 或用户数据写入日志/测试/提交。

## 3. 代码规范

- Python 3.11+，类型标注，`from __future__ import annotations`；
- Ruff 行宽 100；提交前 `ruff format` 与 `ruff check`；
- 公共函数/模块写英文 docstring；面向用户的字符串可用中文；
- 错误使用 `DounaiError` 子类，不向客户端泄漏堆栈或厂商响应体；
- 路径操作必须防止穿越；上传必须限大小并按魔数识别；
- 阻塞 I/O/推理必须 `asyncio.to_thread`，惰性生成器也要在 worker 中消费；
- 不引入隐式遥测、分析 SDK 或后台外连。

## 4. 测试

- 使用 `tmp_path` 生成测试媒体，不提交二进制 fixtures；
- 厂商网络用 `httpx.MockTransport`，不访问真实 API；
- 新增行为至少包含成功、边界、失败/降级路径；
- 隐私相关改动必须覆盖未授权、关闭、删除、导出；
- 修改契约必须更新 `schemas/input-event.schema.json`、`docs/api.md` 和测试；
- 不接受通过 sleep 等待真实网络/模型的测试。

运行：

```bash
pytest -q
pytest --cov=dounai_input --cov-report=term-missing
```

当前故障注入与根因分析见 [测试报告](docs/testing-report.md)。

## 5. 提交

使用清晰的 conventional commit：

```text
feat(providers): add self-hosted HTTP adapter
fix(privacy): use runtime switch when enrolling
docs(api): document capability URLs
test(fallback): cover circuit breaker
```

要求：

- 一个提交只做一件事；
- 提交信息不包含内部测试备注、调试过程或工单噪声；
- 提交前删除 `.env`、测试日志、临时脚本、运行数据、模型权重；
- 不提交任何真实密钥或用户数据；
- 文档与代码同步更新。

## 6. Pull Request

PR 描述请包含：

- 动机与行为变化；
- 隐私/安全影响；
- 测试与验证命令；
- 兼容性（事件契约、配置、API）；
- 截图/日志时先脱敏。

维护者会重点审查：数据流、失败路径、错误回显、资源清理、测试覆盖、文档准确性。

## 7. 新增 provider 的检查清单

- [ ] 独立可选包或明确的核心适配器理由
- [ ] `manifest` 能力/隐私声明真实
- [ ] `availability` 不发网络请求、不泄漏密钥
- [ ] 超时、HTTP 错误、畸形 JSON 有测试
- [ ] 不把厂商响应体拼进异常
- [ ] 密钥只从环境变量读取
- [ ] 文档说明注册、配置、卸载、许可证
- [ ] 不改变事件契约

## 8. 行为准则

保持尊重、就事论事。禁止骚扰、歧视、泄露他人隐私或提交真实用户数据。维护者可移除违反者。

## 9. 许可证

提交代码即表示你同意以本仓库的 MIT License 发布你的贡献。
