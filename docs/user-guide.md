# 用户指南

本文面向部署和使用豆奶输入层的人。它覆盖四件事：**启动服务、安装本地模型、填写第三方密钥、管理声纹与个人数据**。

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
- **语音**：录音或上传 WAV / MP3 / M4A / FLAC / OGG / WebM，最大 25 MB。若配置了语音模型，转写文本写入 `metadata.asr_text`。

控制台录音最长 60 秒；浏览器会在上传前尽量转成 16 kHz PCM WAV，以便进行本地声纹判断。直接上传非 WAV 语音仍可正常生成事件，但声纹判断会跳过。

发送后右侧显示**精确的事件 JSON**。下游只应消费这个结构。

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

## 4. 第三方推荐与 BYOK

### 4.1 推荐：阿里云百炼 Qwen3-ASR-Flash

本层推荐 **Qwen3-ASR-Flash**（阿里云百炼 / Model Studio）作为第三方语音转写模型。理由：

- 中文、方言与中英混说识别稳定，适合中文情感陪伴产品；
- 单次调用即完成转写，无需本地 GPU；
- 同一账号体系可继续使用 **Qwen3.8-Omni-Flash** 做图文音全模态理解；
- 提供 OpenAI 兼容协议，适配成本低。

这不是对绝对准确率的普遍担保；不同口音、噪声和领域词汇仍应做自己的抽样评估。若你的用户主要在海外或已有 OpenAI 企业协议，内置的 OpenAI provider 可作为备选。

### 4.2 注册与获取密钥

我们**不提供账号或 API Key**，也不会代付费用。步骤：

1. 打开 [阿里云百炼控制台](https://bailian.console.aliyun.com/)，按页面提示开通模型服务；
2. 在 [API Key 管理](https://help.aliyun.com/zh/model-studio/get-api-key) 创建密钥；
3. 确认你的服务地域（北京、新加坡等）。API Key 与地域相关；
4. 把密钥只保存到环境变量。

PowerShell（当前会话）：

```powershell
$env:DASHSCOPE_API_KEY = "sk-你的密钥"
dounai-input providers activate dashscope
```

macOS / Linux：

```bash
export DASHSCOPE_API_KEY="sk-你的密钥"
dounai-input providers activate dashscope
```

持久化环境变量由你的 shell 或部署平台管理。也可以把密钥写进本机 `.env`（已被 Git 忽略）：

```bash
cp .env.example .env
# 编辑 .env，只填 DASHSCOPE_API_KEY
```

`.env` 仅由 CLI/服务启动时读取；**绝不要提交它**。

### 4.3 配置 Workspace 专属域名

默认使用：

```text
https://dashscope.aliyuncs.com/compatible-mode/v1
```

若控制台要求 Workspace 专属域名，在 `dounai.toml` 中替换：

```toml
[providers.dashscope]
base_url = "https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
asr_model = "qwen3-asr-flash"
```

把 `{WorkspaceId}` 换成自己的 ID。改完重启服务。

### 4.4 验证

```bash
dounai-input providers list
```

`dashscope` 应显示「可用」。然后在 Web 控制台上传一段中文语音，事件中的 `asr_text` 应出现转写文本；响应头 `X-Dounai-Provider` 为 `dashscope`。

若失败：

```bash
dounai-input doctor
```

常见原因是密钥未进入当前进程、地域不匹配、Workspace 域名错误或网络无法访问厂商。请求失败时输入层仍会生成语音事件，只是 `asr_text` 为空，并在响应头标记降级。

### 4.5 密钥安全

- 密钥只从 `api_key_env` 指向的环境变量读取，不写入 SQLite，不返回给 Web 前端，不打印到日志。
- 日志里只有厂商 ID、HTTP 状态码和错误类别。
- 前端只显示「未配置/可用」状态，不会回显密钥。
- 如果怀疑泄漏，立即在厂商控制台轮换密钥。输入层无需改代码，重启读取新值即可。

## 5. 模型切换与降级

在 Web 控制台顶部「当前模型」下拉框即可切换，无需重启。也可以：

```bash
dounai-input providers activate openai
```

- 选择会写入本地 SQLite，重启后保留。
- 会话上下文保存在本地事件表中。转写请求会携带最近几条文本/转写作为上下文（厂商支持时）。
- 主模型不可用、超时、限流、返回格式异常时，会按 `fallback` 顺序尝试。
- **隐私守卫**：默认 `allow_remote_fallback = false`。当主模型是本地模型时，不会自动把音频发到云端；当主模型是云端时，也不会自动再发给另一家云端。只有显式启用才允许云端降级。
- 全部失败时仍会返回事件，`asr_text` 为空，响应头 `X-Dounai-Degraded: true`。下游不会因为模型故障而收不到输入。

控制台的「降级」标记来自响应头，不写进事件，保证事件严格符合契约。

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
- **导出**：ZIP 包含清单、事件、媒体文件、授权记录、隐私操作记录、声纹模板（如有）和政策文本。导出文件包含生物特征，请妥善保管。
- **删除全部**：删除事件、媒体、声纹模板、授权记录和隐私审计记录。删除后媒体 URL 立即失效；之后会写入一条不含生物特征的操作记录，证明删除发生。
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
3. 云端看 `DASHSCOPE_API_KEY`/`OPENAI_API_KEY` 是否在当前进程环境中；
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

## 10. 卸载

```bash
# 停止服务后
dounai-input models remove small --yes
# 删除本地数据（不可恢复）
dounai-input data delete --yes
```

再删除虚拟环境与仓库目录即可。服务没有系统级守护进程或注册表项。
