# 白名单、记忆与附件助手

功能位于当前运行项目的 `src/core/assistant/`，通过现有 `agent_loop.py` 进入。完全离线入口不读取桌面、不加载 `.env`，不发送微信消息。所有发件记录标记为 `simulated`，不是投递回执。

本次执行结果见 [离线集成验收](offline-assistant-validation.md)。

## 启动

在 `微信自动化回复/` 目录执行：

```bash
.venv/bin/python -m pip install -e '.[dev,assistant]'
.venv/bin/python agent_loop.py --offline-assistant replay --report logs/assistant-report.json
.venv/bin/python agent_loop.py --offline-assistant inspect
```

默认使用确定性 `FixtureModel`，无需模型服务。重放同一输入、同一数据库不会重复回复。需要重新完整演示时指定新的 `--db`，不要删除生产去重库。

持续监听本地 JSONL 文件：

```bash
.venv/bin/python agent_loop.py --offline-assistant watch --input examples/assistant/messages.jsonl
```

追加完整 JSON 行后生成模拟回复；Ctrl+C 停止。文件轮换或截断后重新读取，持久消息 ID 防止重复回复；消息 ID 内容冲突、解析错误会报告错误。此入口的“监听”是本地消息事件监听，不是微信客户端监听。

## 本地模型与多媒体

复制 `assistant.example.json` 为被 Git 忽略的 `assistant.local.json`，填写本机已有资源：

- `local_model`：Ollama 已安装模型名称；接口固定为 `127.0.0.1:11434`，拒绝 cloud/remote 模型，不经代理。
- `media.asr_python`：安装了 `mlx_whisper` 的 Python 环境。
- `media.asr_model`：含 `config.json` 和 `weights.safetensors` 的本地 Whisper 模型目录；缺失时直接失败，不下载。
- `media.ffmpeg`、`media.ffprobe`：本机可执行程序路径。
- `media.ocr_binary`：运行 `bash scripts/build_assistant_ocr.sh` 生成的 `scripts/assistant-vision-ocr`。

```bash
.venv/bin/python agent_loop.py --offline-assistant doctor --config assistant.local.json
.venv/bin/python agent_loop.py --offline-assistant replay --config assistant.local.json --model local --db logs/local-text.sqlite3 --report logs/local-text.json
.venv/bin/python agent_loop.py --offline-assistant replay --config assistant.local.json --model local --input examples/assistant/multimodal.jsonl --db logs/local-media.sqlite3 --report logs/local-media.json
```

`doctor` 检查模型元数据和路径存在性，不代表转写准确率验证。真实调用本地模型仍需上述重放验证。

## 消息契约与处理

```json
{"id":"unique-001","conversation":"demo-alice","sender":"alice","kind":"file","attachment":"meeting.txt","text":"会议在哪里？"}
```

必填字段为 `id`、`conversation`、`sender`、`kind`。`kind` 接受 `text`、`file`、`voice`、`image`、`video`、`system`；文本不得为空且最多 4000 字符，附件路径相对配置的 `inbox`。`group:true` 拒绝处理，`direction:"self"` 触发人工接管。

1. `JsonlSource.poll` 读取完整行；`Engine.ingest` 校验 `(conversation,sender)` 白名单、群聊、系统消息、垃圾关键词和持久去重。
2. `Engine.process_one` 提取附件，读取当前会话历史与显式记忆，检索带来源的资料，再调用模型。
3. 校验意图、回复及引用来源；新消息或人工接管使过期草稿失效。
4. `OfflineSender` 仅写入 SQLite 模拟回执。异常不猜测成功，未知状态暂停且不自动重发。

白名单配置不接受通配匹配。不同会话与发送者的记忆隔离；白名单外正文仅保存哈希。垃圾过滤为配置关键词规则，不宣称识别所有垃圾信息。限流默认每分钟最多 10 条回复。

## 记忆与接管

- 短期：默认最近 30 分钟内的 `history_window` 条历史及更早有效摘录，总正文上限 12000 字符；支持按会话重置。详见[会话短期记忆](session-memory.md)。
- 长期：发送 `记住：回复风格=简短中文`；删除使用 `忘记：回复风格`。
- 人工消息暂停同一会话；恢复后仅处理新消息，旧积压不补发。
- `clear` 清除正文、记忆及资料，保留去重墓碑与发送状态；数据库含本地明文对话，应按个人数据管理。

```bash
.venv/bin/python agent_loop.py --offline-assistant resume --conversation demo-alice --sender alice
.venv/bin/python agent_loop.py --offline-assistant clear --conversation demo-alice --sender alice
.venv/bin/python agent_loop.py --offline-assistant resolve --message-id MESSAGE_HASH --resolution not_sent
```

`resolve` 只记录人工核查结果，不重发。配置文件和数据库必须与原运行实例保持一致；同一数据库使用进程锁。

## 附件范围

| 类型 | 实现 | 边界 |
| --- | --- | --- |
| TXT/MD/JSON/CSV | UTF-8 内容、行号 | CSV 不推断统计口径 |
| DOCX | 正文与表格段落 | 不解析页眉、批注、嵌入对象 |
| XLSX | 单元格、公式原文 | 不执行公式、宏、外部链接；每表最多 20000 单元格 |
| PDF | 文本层、页号 | 最多 100 页；扫描页不自动 OCR；加密文件拒绝 |
| 语音 | 本地 Whisper 转写 | 人名、金额、时间需核对；微信原始语音编码需接入层转换 |
| 图片 | macOS Vision OCR | 只识别文字，不推断人物和物体 |
| 视频 | 0%、50%、80% 抽帧 OCR 与音轨转写 | 非全视频理解；不可读部分显式标注 |

所有附件限制在 inbox 内，拒绝符号链接越界，单文件最多 25 MiB；音视频最多 180 秒；解析文字最多 60000 字符，超限拒绝而非静默截断。检索不等于全文总结；回复包含来源。附件是输入数据，不具有执行指令的权限。

新增可选依赖仅 `pypdf==6.10.0`（PDF 文本层）和 `openpyxl==3.1.5`（XLSX 读取），未替换现有依赖。ASR 与 OCR 为可选本机资源。

## 现有桌面调用链

`agent_loop.py --wechat-native --assistant` → `PersonalTailInbox` → `AgentLoop` → `DesktopAssistant` → 同一 `Engine` → 原 `HTTPReplyModel` → 原 dry-run 输出。

此命令会操作真实桌面，但只生成草稿，本轮未执行。新增增强处理拒绝 `--live` 和 `DRY_RUN=false`。原 `desktop_messages` 去重键、原始分钟时间、历史基线和尾部追加规则继续由原流程管理，助手记忆位于 `.assistant.db` 旁路数据库。模型使用 `assistant_reply.txt` 模板；其服务地址仍由原桌面配置决定，所以桌面入口不等同于完全离线模型入口。

桌面增强当前只接文字，不自动下载微信语音或文件。多联系人白名单、附件、本地模型已在离线事件链验证；新增强处理链的真实微信收件、真实联系人身份与投递尚未实机验证。旧桌面流程的历史验收记录不作为新增功能的实机证明。

## 手动配置与管理

详见[用户操作说明](assistant-user-management.md)。首次使用可复制 `assistant.user.example.json` 到 `assistant.local.json`，默认空白名单，随后使用 `whitelist-add` 明确添加会话。原 `assistant.example.json` 继续用于演示与回归测试。

`watch` 运行期间可使用 `whitelist-add`、`whitelist-remove`、`whitelist-list`；其余记忆和附件管理需要先停止占用同一数据库的监听。只对下一次成功加载生效，不撤回已经输出的模拟回复。配置错误或读取失败时停止离线监听；移除会话会取消排队消息并暂停会话，重新加入不恢复旧积压。
