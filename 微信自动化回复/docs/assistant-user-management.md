# 用户手动管理白名单、记忆与附件

这些命令运行离线助手：不读取微信桌面、不发送微信消息。默认使用确定性 `FixtureModel`，输出为模拟回复。实际本地模型需安装并配置 Ollama，再给处理命令添加 `--model local`。

## 1. 初始化个人配置

在 `微信自动化回复/` 目录执行：

```bash
.venv/bin/python -m pip install -e '.[dev,assistant]'
cp -n assistant.user.example.json assistant.local.json
mkdir -p inbox logs
```

`cp -n` 保留已有配置。新模板的 `whitelist` 是空数组，表示所有会话均不处理；原演示模板不变。`assistant.local.json`、`inbox/` 和 `logs/` 已被 Git 忽略。模型和本机媒体工具配置见[离线助手说明](offline-assistant.md)。

下文使用同一个 shell 函数固定配置和数据库：

```bash
assistant() {
  .venv/bin/python agent_loop.py --offline-assistant "$@" \
    --config assistant.local.json --db logs/assistant.sqlite3
}
```

`conversation` 和 `sender` 是本地事件中的精确身份字段，不是昵称搜索，也不自动换算为微信官方 ID。下文 `my-chat`、`my-peer` 是演示值，实际导入时必须与自己的消息事件完全一致。

## 2. 手动设置白名单

```bash
assistant whitelist-list
assistant whitelist-add --conversation my-chat --sender my-peer
assistant whitelist-remove --conversation my-chat --sender my-peer
```

添加重复项不重复保存，删除不存在的项不影响其他项。不接受空身份、首尾空白和通配符 `*`。也可以在停止监听后手动编辑 `assistant.local.json` 的 `whitelist`：

```json
{
  "whitelist": [
    {"conversation": "my-chat", "sender": "my-peer"}
  ]
}
```

以上 JSON 仅展示白名单字段；编辑时保留原文件的 `inbox`、`media` 和模型配置，不用这段覆盖整个配置文件。

命令修改使用文件锁和原子替换，保留其他配置。离线 `watch` 周期性重新读取白名单；在下一次成功加载后生效。移除会话会取消尚未处理的消息，使正在生成的草稿失效，并暂停该会话。已经产生的模拟回复不能撤回。配置损坏或读取失败时监听停止，不扩大处理范围。

重新加入曾移除的会话后，先停止同库监听，再恢复：

```bash
assistant whitelist-add --conversation my-chat --sender my-peer
assistant resume --conversation my-chat --sender my-peer
```

恢复不补发旧积压。未知发送仍需要先人工核查，不因重新添加白名单而自动重试。重启时用更小白名单加载旧库，也会取消未授权的待处理消息。

## 3. 记忆管理

先确保目标在白名单中。同一数据库的离线监听需先按 Control+C 停止，以释放数据库进程锁。

```bash
assistant memory-set --conversation my-chat --sender my-peer \
  --key 回复风格 --value 简短中文
assistant memory-set --conversation my-chat --sender my-peer \
  --key 当前项目 --value 周五评审 --ttl-seconds 86400
assistant memory-list --conversation my-chat --sender my-peer
assistant memory-forget --conversation my-chat --sender my-peer --key 当前项目
```

不传 `--ttl-seconds` 表示长期保留；有效期为 1 至 31536000 秒。列表包含 `expires`，为本地记录的到期 Unix 时间；`null` 表示无到期时间。过期条目不进入回复上下文；到期并不等同于物理擦除数据库行。记忆名称最多 40 字符，值最多 300 字符。

本地入站消息中的 `记住：回复风格=简短中文`、`忘记：回复风格` 继续有效。连续发送普通问题和记忆指令时，两者分别处理，不再把前一条问题合并丢失。

`memory-forget` 只删除显式记忆，不清除原始消息、历史回复或审计记录。需要清除整个会话内容时：

```bash
assistant clear --conversation my-chat --sender my-peer
```

`clear` 清除会话正文、记忆和资料并暂停会话，保留去重墓碑及发送状态；原始附件文件不删除。恢复后仅处理之后的新消息。

## 4. 手动提交和管理附件

将附件放入项目 `inbox/`，路径相对配置中的 inbox。以下命令使用你已放入的 `inbox/会议纪要.txt`：

```bash
assistant attachment-add --conversation my-chat --sender my-peer \
  --kind file --attachment 会议纪要.txt --text '会议时间和待办是什么？' \
  --message-id meeting-import-001
assistant attachment-list --conversation my-chat --sender my-peer
```

`--message-id` 是本地事件 ID。同一会话用同一 ID 重试同一内容不重复回复；同一 ID 对应不同内容会拒绝。省略时每次生成新 ID，反复导入会被视为新事件。输出中的 `message_id` 是数据库文档 ID，可用于移除；它与原始事件 ID 不同。

不提供问题时，附件完成解析后生成追问。`attachment-add` 返回当前事件状态、模拟回复、引用证据；解析失败返回非零退出码。`attachment-list` 返回资料 ID、文件名和解析边界，不输出完整资料正文。

其他类型的本地导入命令：

```bash
assistant attachment-add --conversation my-chat --sender my-peer \
  --kind image --attachment 截图.png --text '提取其中的时间和地点'
assistant attachment-add --conversation my-chat --sender my-peer \
  --kind voice --attachment 录音.wav
assistant attachment-add --conversation my-chat --sender my-peer \
  --kind video --attachment 片段.mp4 --text '有哪些可读文字？'
```

语音转写也保存为带来源的资料，可在后续问题中检索，并使用相同附件管理命令移除。

图片要求已编译本机 Vision OCR；语音要求本地 Whisper 权重及转写环境；视频还需要 ffmpeg、ffprobe。未安装时拒绝解析，不自动下载。图片仅做文字识别；视频仅做抽帧 OCR 与音轨转写。TXT、MD、JSON、CSV、DOCX、XLSX、PDF 的具体覆盖范围见[附件能力表](offline-assistant.md#附件范围)。

从 `attachment-list` 复制目标 ID，替换下方的 `DOCUMENT_ID`：

```bash
assistant attachment-forget --conversation my-chat --sender my-peer \
  --message-id DOCUMENT_ID
```

移除后，该资料不再出现在后续附件检索中。操作保留磁盘原文件和历史消息；历史回复可能包含原资料摘录。需要清除历史时使用会话 `clear`，需要删除原文件时另行手动处理。

附件不得越出 inbox，符号链接越界也会拒绝。单文件最多 25 MiB，音视频最多 180 秒，解析文字最多 60000 字符。检索最多最近 10 份资料的 6 个片段，不声称完整分析整份文档。

## 5. 持续监听与实际微信边界

```bash
assistant watch --input examples/assistant/messages.jsonl
```

这是监听本地 JSONL 文件，文件中的事件必须匹配用户配置的白名单。演示文件的 `demo-alice/alice` 不会自动匹配 `my-chat/my-peer`。白名单命令可在另一个终端使用相同配置执行；记忆、附件、清除和恢复命令需先停止同数据库监听。

真实微信仍限定微信 2 的 Ri 独立单聊。以上白名单不会切换客户端、定位新联系人或放开真实发送。桌面 `--wechat-native --assistant` 仍为单独的 dry-run 文字路径，不自动使用本地附件目录或这份离线配置。任何多联系人真实接入及增强能力的真实发送均未完成实机验收。

## 6. 使用图形界面

不想通过命令逐项添加白名单时，可使用[联系人勾选页面](contact-ui.md)。页面与 CLI 共用同一份本地配置，支持版本冲突检测。
