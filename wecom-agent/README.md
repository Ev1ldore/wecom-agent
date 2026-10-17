# wecom-agent

当前个人账号路径：Ri 外部微信单聊的聊天记录面板 → 作者与白名单过滤 → LLM → SQLite → 本人企微账号在原会话回复。默认 dry-run；显式 `--personal --live` 启用真实发送。要求 Python 3.11 或更高版本。

原 FastAPI 回调、Redis Stream 和自建应用主动发送接口继续保留，见后文兼容章节。

## 当前入口：Ri 个人账号自动回复

在企微中打开 `Ri @微信` 单聊，打开“查找聊天记录”，清空搜索条件并保持默认筛选。持续运行会关闭并重新打开已验证的聊天记录面板，获取新消息。会话或初始面板不匹配时拒绝该轮，不切换联系人。

```bash
source .venv/bin/activate
python agent_loop.py --personal
```

用户已授权真实自动回复，运行：

```bash
python -u agent_loop.py --personal --live
```

真实发送每次启动均跳过当前全部可见历史，只回复启动后新增文本，不补发 dry-run 草稿。发送前检查 Ri 原会话、空白可写输入框，写入并提交一次；只有新增本人出站气泡正文匹配且输入框清空，才记录 `sent`（仅本机提交证据，不代表对方已读或收到）。写入后超时、取消或回执不明记录 `send_unknown`，不自动重发。连续三次失败暂停 60 秒。停止在运行终端按 `Ctrl-C`。

2026-10-17 真实发送交付验证：`pytest -v` 收集 638 / 通过 638，ruff 与格式检查通过，`python smoke.py` 通过。实机调用个人通道一次，9.44 秒完成，在 Ri 原会话确认本人出站气泡「真实发送测试：已接通。」且输入框清空；未使用机器人或自建应用发送。对方实际接收及新输入驱动的首次自动回复仍待观察，不以本机气泡冒充远端回执。运行日志保存在 `logs/personal-live.log`，该目录已排除 Git：

```bash
tail -f logs/personal-live.log
```

查看来信正文与回复正文，可双击 `查看企微实时日志.command`，或在项目目录运行 `.venv/bin/python -u watch_logs.py`。查看器只读 SQLite 和运行日志，不启动、重启或发送消息；先显示最近一条历史处理记录，再显示新收到的消息及回复状态。`已提交` 表示本机出站气泡已确认，`发送结果未知` 不会显示为成功；空闲轮询不刷屏，配置密钥和终端控制字符会脱敏或转义。关闭此查看器不影响自动回复程序。

2026-10-17 后续修复：收件刷新动作中断后，原实现丢失面板重开依据，随后持续报 `HISTORY_CLOSED`。现保留已验证刷新动作的恢复标记，并在必要时跨轮保留；未知初始关闭仍拒绝操作。实机注入关闭后中断，首次返回 `REFRESH_INCOMPLETE`，第二次安全重开并读到 Ri 的 `10/17 17:27` 原始消息。该条「你好，今天怎么样」已通过真实模型、本人原会话发送、SQLite `sent` 记录完整链路，出站正文为「你好，今天还行，你呢？」；同条重放仅命中去重，额外发送 0 次。这里的 `sent` 仍仅表示本机提交，对方实际接收未核实。回归收集 650 / 通过 650，ruff、格式检查及 `python smoke.py` 通过。发送模型仍未严格遵守“不编造本人当前状态”的模板规则，这条回复的「今天还行」没有本人状态依据；不将收发链路通过等同于回复内容准确性通过。

也可设置 `INBOX_BACKEND=personal` 后运行 `python agent_loop.py`。`PERSONAL_CONTACT_NAME` 当前仅接受 `Ri`。此路径不需要 CLI、Redis 或回调凭据，模型信息仍从本地 `.env` 的 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL` 读取。个人模板通过 `PERSONAL_LLM_TEMPLATE_NAME=personal_reply.txt` 从 `prompts/` 加载，用于自然聊天草稿，不主动作助手身份介绍；兼容 CLI 的 `LLM_TEMPLATE_NAME` 保持独立。

dry-run 首次启动将当前可见历史保存为 `baseline`；已有 SQLite 记录时，从当前历史中最后一个已保存指纹后恢复新消息。后续只接受严格尾部追加，不变快照不重复调用模型。缺少锚点、前插、重排或截断时暂停该轮。`--personal --once` 只处理一轮；持续运行不加 `--once`。

时间采用用户已接受的原始显示契约：例如 `10/16 12:11` 原样保存到 `time_display`，`time_precision="minute"`，`ts=NULL`。不补年份、不补秒，不用接收时间。作者、正文和时间必须来自同一历史行；系统提示及本人发送的行不进入模型。

个人会话标识 `desktop:wechat:Ri` 仅是本地命名空间，不是服务端 user_id。去重键由该标识、正文 SHA-256 和 `("display-minute", time_display)` 组成。相同正文在相同显示分钟内会合并；显示标签缺失年份时无法区分跨年的同标签。当前只接受可见记录中唯一文本正文及已观测的 `MM/DD HH:MM` 时间格式，不承诺服务端 unread、滚动历史补齐、非文本接入或稳定消息 ID；实机已确认历史面板不会自动刷新，当前通过受限开关操作刷新后读取。

已有秒级记录保留原时间桶与去重键。SQLite 首次打开时在事务中迁移可空时间及精度字段，保留已有状态、回复、错误和行序；失败不会重置去重记录。个人入口默认注入 `DisabledSender`；live 模式使用受限的 `PersonalDesktopReplyChannel`。

对现有固定消息“你好 / 10/16 12:11”做一次真实模型定点验证：

```bash
python -m smoke.smoke_personal
```

该命令只处理这条现有输入，重新打开 SQLite 并再次读取以验证去重，不发送企微消息。应先执行定点验证，再启动个人入口建立基线；已有 `baseline` 或失败 claim 不会被删除或覆盖。完整拟回复保存到配置的 SQLite；命令只输出精度、处理计数和零发送结果。

dry-run 每条草稿会核对该会话和唯一空白可写输入框，预检通过才输出并保存。Qt 不暴露 `AXEnabled`，该状态保留为未知；真实发送以新增出站气泡作为本机提交回执。

2026-10-17 先前 dry-run 验证（以下发送禁用结论仅适用于该阶段）：

| 检查项 | 实际结果 |
| --- | --- |
| Ri 原生读取 | 6.24 秒读到两条 Ri 作者记录；“你好”保留 `10/16 12:11`，`ts=NULL`、分钟精度，系统提示未转换为消息 |
| 固定消息真实 dry-run | 读取现有“你好”→真实模型 1 次→完整拟回复保存 SQLite→重新打开数据库并再次读取，重复数 1、额外模型调用 0、真实发送 0 |
| 主入口 | `agent_loop.py --personal --once` 退出码 0；启动基线新增 1 条，随后 2 条历史均命中去重，本轮新增模型调用 0、发送 0 |
| 个人草稿模板 | 后续新增独立 `personal_reply.txt`，单独对合成问候“你好”做真实模型预览，输出“你好，有什么事吗？”；未读取新私信、未发送、未改写原处理记录 |
| 自动测试与手动 smoke | pytest 收集 491 / 通过 491，ruff 和格式检查通过，`python smoke.py` 通过 |

固定消息的最初拟回复使用此前模板并主动介绍助手身份；该历史记录保持原样。新的个人模板用于后续新增草稿，重放不会为了模板变化重新生成旧回复。上述为先前历史消息验证；后续新消息实测见下方当前记录，仍不宣称已完全接管。


## 安装

在项目目录中创建并激活虚拟环境，然后安装运行依赖和开发依赖：

```bash
cd wecom-agent
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

真实 CLI 收消息还需要：

```bash
npm install -g @wecom/cli
```

本地存储使用 Python 标准库 `sqlite3`；本地 MVP 的 HTTP 与日志复用已有 `httpx`、`structlog`，未新增 Python 运行依赖。兼容回调使用 `wechatpy[cryptography]==1.8.18`，替换记录见文末。

## 先前 dry-run 持续运行验证（2026-10-17）

- Ri 实际发来 `接收测试-1017`。主聊天自动出现该消息，原历史面板保持旧数据；关闭/重新打开后读到同一行的 `10/17 16:30`，保留分钟精度，未构造 epoch。
- 持久锚点恢复、自动刷新、接收、真实模型和 SQLite 已执行。初次只读预检因 Qt 未提供 `AXEnabled` 失败，修复后复用已保存草稿完成预检和本地输出，没有再次调用模型，也没有删除去重键。
- 程序已持续轮询；相同快照不再次生成回复。当前所有实际发送均禁用，真实发送 0 次。停止可在运行终端按 `Ctrl-C`。
- 本次模型草稿将输入中的 `1017` 误写成 `1027`；原草稿保留用于审阅，未发送。这是已观察到的草稿质量问题，不视为语义准确性通过。
- CLI 对实际外部联系人标识的只读查询返回 `93006：无效的群聊ID`，未据此断言所有外部单聊均不受支持；当前不使用该标识调用发送接口。
- 本次 `pytest -v`：收集 611 / 通过 611；ruff 与格式检查通过；手动 `python smoke.py` 通过，包括新增消息和个人会话预检的离线链路。未新增依赖。

## 历史 Phase 0：CLI 与机器人接入探测

本节记录切换个人账号接入前的 CLI 与机器人探测结果，不代表这些路径已满足当前 Ri 个人会话目标。

2026-10-17 的确认记录：

| 检查项 | 结果与证据边界 |
| --- | --- |
| 企业人数 | 用户确认企业为 1 人；没有通过管理后台独立核验 |
| CLI 安装 | `npm install -g @wecom/cli` 成功；本机版本为 `wecom-cli 1.3.4`，提交 `f9b2815` |
| 命令发现 | `wecom-cli --help` 与 `wecom-cli chat messages list --help` 实际执行成功 |
| 账号授权 | 已启动 CLI 授权并由用户扫码确认；`wecom-cli auth show --status` 实测返回 `authorized` |
| 正文查询 | `chat messages list` 请求成功；发送固定输入后，15 分钟和 24 小时窗口均返回零条消息。尚不能确认正文读取链路可用 |
| 本地离线演示 | `python agent_loop.py --demo` 实际通过：1 次 dry-run、4 条过滤、1 条重复、SQLite 重启去重、0 次真实发送 |
| 机器人客户端身份 | 已在企微客户端“管理机器人”列表确认“李承云的机器人”为 API 模式，可见范围为李承云 |
| 固定输入 | 已从用户企微账号向“李承云的机器人”发送一条 `本地dry-run测试-1017`；单聊消息气泡已显示 |
| 机器人会话元数据 | `message aibot sessions list` 返回 1 个单聊，会话 ID 与配置目标一致，名称对应李承云，最后消息时间为 `2026-10-17 11:43:06` |
| 模型连通性 | 更新配置后，Anthropic 前缀与现有 Chat Completions 客户端不匹配，返回 HTTP 404；改为官方兼容基址后，真实请求通过，返回 151 字符非空回复 |
| 配置恢复 | 模型配置更新期间 `INBOX_CHAT_ID` 变为空，CLI 返回 `10003`；重新查询已确认的机器人单聊元数据后恢复该 ID。恢复后的正文查询成功，仍返回零条消息 |
| macOS 只读快照 | 已实现并实机验证 `AppleScriptSnapshotReader`：标题和选中 BOT 校验通过，约 5.1 秒读到唯一固定测试正文及显示时间 `11:43`。未接入生产消息循环 |
| 真实 CLI → LLM 链路 | 未通过；正文、原始发送者与消息时间字段尚未读到，未创建处理 claim，也未运行完整真实定点冒烟 |

由用户本人运行以下命令并完成企业微信扫码：

```bash
wecom-cli auth init
wecom-cli auth show --status
```

授权及命令参数以[官方 CLI 参考（f9b2815）](https://github.com/WecomTeam/wecom-cli/blob/f9b2815/docs/cli-reference.md)和本机帮助为准。官方仓库的 [#102](https://github.com/WecomTeam/wecom-cli/issues/102) 记录过会话消息能力的版本变化，[#132](https://github.com/WecomTeam/wecom-cli/issues/132) 是有关企业能力限制的提问；这些记录不构成当前账号已开通能力的证明，也不能据企业人数直接断言当前是否存在“大于 10 人”的限制。

切换个人接入前，CLI 接收适配器已实现，但真实正文接收未验收。当时未启用桌面收件后端。空结果实测形态为 `messages_count=0`、`has_more=false`，并省略 `messages/next_cursor`；接收适配器对该形态有独立回归测试。请求成功、授权人 ID 可解析和会话元数据存在，都不能单独证明该接口返回了机器人单聊正文。空结果原因尚未确认，未据此归因为企业人数限制。其他历史消息内容及凭据没有打印到 Phase 0 探测日志中。

当前 `.env` 已补齐 dry-run 配置，并填入已确认的授权人单聊目标。已通过通讯录查询确认该原始 ID 对应李承云，并通过机器人会话列表核对单聊及最后互动时间；CLI 正文中的 `userid/send_time` 仍未读到，因此 `INBOX_SENDER_ID` 保持空值，`INBOX_VERIFIED_SINGLE_CHAT=false`。主入口继续拒绝未经验证的真实收件映射。此次已授权的人工账号输入实际发送 1 条，程序自动真实回复 0 条；没有重复发送该输入。模型已单独通过真实连通性验证；该结果不代表 CLI 收件、处理、SQLite 落盘及重放去重的真实闭环已通过。

DeepSeek 当前配置使用 `LLM_BASE_URL=https://api.deepseek.com`，与现有 Chat Completions 客户端一致；原 API Key、用户指定的模型名均保持不变。[DeepSeek 官方文档](https://api-docs.deepseek.com/) 分别列出 OpenAI 兼容基址和 `/anthropic` 基址。此次仅修正本地配置，没有改写业务路由、消息字段或错误响应契约。

当时机器人快照只观测到分钟级 `11:43`，未接入处理循环。用户现已接受原始显示时间并标注分钟精度，当前个人路径采用文首的可空 `ts` 契约；仍不补造日期或秒数，也不以会话最后消息时间配给每条正文。

官方 [Node SDK 消息类型](https://github.com/WecomTeam/aibot-node-sdk/blob/main/src/types/message.ts) 将普通消息的 `create_time` 声明为可选字段，注释为事件产生的时间戳。尚未核实它在当前账号的文本回包中是否存在、单位是什么、是否为原始发送时间；未将 WebSocket 宣称为已满足时间契约的替代接入，未切换到该通道。

后续官方 Python SDK 实测：认证曾成功，唯一获授权输入 `sdk-time-probe-1017-d9abfb46` 已从本人账号发送并在气泡中确认。但监听已在发送前结束，未收到该输入对应的 SDK 消息帧；`create_time` 是否存在、单位及原始发送时间语义均未验证。后续认证返回 `errcode=853000` 后停止，未保留该次 errmsg，具体原因未知。个人账号接入不依赖此机器人通道，未启用机器人回复。

## 兼容路径：机器人只读快照

`adapters/wecom_desktop_reader.py` 提供 `SnapshotReader` Protocol、`AppleScriptSnapshotReader` 和快照数据类型；`adapters/wecom_snapshot.applescript` 随包分发，使用 macOS 自带 System Events 和 Foundation，没有新增 Python 依赖。

- 只读取已经打开的指定机器人单聊。读取前后均核对主面板直属标题和侧栏唯一选中行的 BOT 标记；当前窗口层级、两张表或身份标记不匹配时明确报错。
- 不激活应用、不切换会话、不滚动、不点击输入框、不模拟发送。窗口关闭、权限不足或结构变化时安全失败，用户需要自行保持目标窗口可见。
- 返回正文、原始静态时间标签及几何位置，保留中文、换行和制表符；不推断日期、秒数、作者或 user_id，不构造 `Msg`。该兼容快照不用于个人收件；个人路径使用独立的 `wecom_personal.py`。
- 每次脚本读取预算为 10 秒，失败重试 1 次；取消或超时会终止并回收自己创建的进程组。日志只有安全错误码，不打印原始脚本诊断或消息正文。
- 实机发现并修复 AppleScript 变量名 `records` 与内置类型冲突的问题；新增不读取 UI 的原生运行时回归测试。标题校验使用本机实际层级，整窗表发现只扫描一次；当前单条测试窗口实测约 5.1 秒，不代表其他布局或消息量下的性能保证。

这层有 45 个独立测试，包括固定协议、标题/BOT 拒绝、原文保留、截断与非法输出、超时、取消、私有子进程组回收和原生序列化。真实读取成功不代表已取得原始发送时间、已验证发送者或已完成 LLM/SQLite 闭环。

## 本地 MVP 配置

仅在尚无 `.env` 时创建：

```bash
cp .env.example .env
```

已有 `.env` 时直接编辑现有文件，保留已有配置。`src/desktop_config.py` 单独加载本地 MVP 配置，不要求回调 Secret、Token、EncodingAESKey 或 Redis。

| 配置项 | 用法 |
| --- | --- |
| `DRY_RUN=true` | 默认只打印草稿；Ri 个人入口可用 `--live` 显式开启发送，兼容 CLI 仍强制 dry-run |
| `INBOX_BACKEND=cli` | 保留的 CLI 默认配置；个人接入设为 `personal` 或使用 `--personal`；离线演示用 `--demo` |
| `INBOX_CHAT_ID` | 填写实际核实过的 CLI 单聊原始会话 ID，不能从显示名称猜测 |
| `INBOX_SENDER_ID` | 填写该会话中实际核实过、对应目标用户的 CLI 密文 userid |
| `INBOX_VERIFIED_SINGLE_CHAT=true` | 仅在确认上述映射确实为指定单聊后设置；禁止将群聊标记为单聊 |
| `INBOX_TIMEZONE=Asia/Shanghai` | 显式指定如何解释 CLI 无时区的 `send_time` 字符串；默认值仍需与实际账号行为核对 |
| `ALLOWED_USER_ID=LiChengYun` | 本项目使用的明确、区分大小写的目标用户白名单 |
| `WECOM_AGENT_ID` | 填写对应自建应用 ID，用于本项目规范化会话标识；不是 CLI Bot ID |
| `LLM_BASE_URL` | 填写实际模型兼容 API 前缀；已有 `/v1` 时保留该路径，统一拼接 `/chat/completions` |
| `LLM_API_KEY` | 填写模型 API Key，仅本地保存；不在日志中打印 |
| `LLM_MODEL` | 填写供应商实际支持的模型名称 |
| `LLM_TEMPLATE_NAME=desktop_reply.txt` | 仅加载 `prompts/` 包中的 `.txt` 模板文件名，拒绝路径穿越 |
| `DB_URL=sqlite:///./agent.db` | 本地 SQLite 文件；保存去重 claim、完整输入、完整回复、状态和安全错误 |
| `POLL_INTERVAL_SECONDS=2` | 默认两秒轮询间隔；无恢复记录的 `--once` 首轮另等待约两秒，以形成启动后的查询窗口 |
| `DEDUP_BUCKET_SECONDS=5` | 本地消息去重的时间桶大小，默认五秒 |

CLI 密文 userid 与原主动发送 API 使用的明文 `user_id` 是不同标识。本项目只在配置的原始 `chat_id/userid` 均匹配时，将其显式映射为 `app:{WECOM_AGENT_ID}:{ALLOWED_USER_ID}` 和 `ALLOWED_USER_ID`。映射依赖实际核验，代码不猜测 ID，也不从 CLI 响应缺少类型字段推断单聊。

CLI 机器人与旧自建应用的凭证、身份和会话来源不同。读取某个普通聊天不代表能通过自建应用在该聊天原路回复；未来启用真实发送时，仍需单独核验原 `send_text` 所对应的自建应用身份、成员和会话边界。

同一 `.env` 可以保留后续阶段的额外字段，本地 `DesktopSettings` 和兼容回调 `Settings` 均忽略各自未定义的配置，仍校验已定义字段。旧字段 `WHITELIST_MODE=off` 不会关闭当前强制白名单；当前入口始终按已配置的会话、发送者、单聊类型和方向过滤。

## 兼容路径：CLI 入口

先运行完全离线的演示，不读取 `.env`、真实私信或真实模型：

```bash
python agent_loop.py --demo
```

演示使用 fake inbox、`HTTPReplyModel` 加 `httpx.MockTransport` 和临时真实 SQLite，输出一条固定测试回复的 `[DRY-RUN]` 及 `desktop smoke: PASS`。真实发送次数为 0。

完成本人 CLI 授权、核实单聊映射并填写模型配置后，再启动真实收消息与真实 LLM 的 dry-run：

```bash
python agent_loop.py
```

只执行一轮时使用：

```bash
python agent_loop.py --once
```

`--once` 在没有历史恢复记录时先等待约两秒，再查询启动后的消息窗口；已有恢复记录时直接查询。缺少 CLI、未授权、没有验证过的单聊映射或缺少模型配置时，入口报告明确错误；不会自动认证、猜测数据源或开启发送。运行期间按 `Ctrl-C` 退出。

`agent_loop.py` 注入 `DisabledSender`，并拒绝 `DRY_RUN=false`。`AgentLoop` 保留 `WecomClientProtocol` 注入接口及发送分支的独立测试，本版主入口未启用该分支。

单聊身份、真实消息及其精确时间戳核实后，可单独验证这条固定输入：

```bash
python -m smoke.smoke_local_cli --message-ts <已核实的消息秒级时间戳>
```

该脚本只处理 `本地dry-run测试-1017` 与指定时间戳同时匹配的唯一消息，复用生产过滤、LLM、SQLite claim 和 dry-run 流程。它重新建立接收适配器、重新打开 SQLite 验证去重，并通过拒绝发送的 sender 断言真实发送次数为 0。已有成功记录只报告复用结果与本轮模型调用 0 次；已有失败 claim 不删除、不改键重试。此命令尚未执行真实验收，独立单测使用 fake inbox、MockTransport 和临时 SQLite。

## 本地处理、恢复与失败记录

- `adapters/wecom_cli.py` 只执行 `wecom-cli chat messages list`，处理已配置原始会话与发送者的文本。`poll_unread()` 表示本地待处理的时间窗口，不代表服务端 unread 状态，也不据此确认服务端已读状态。
- 初次启动默认从启动时间之后读取，不扫描整个历史。成功轮询使用五秒 overlap；重启时依据 SQLite 中当前规范化会话和用户的最新已 claim 消息时间恢复，再向前重叠默认五秒。
- CLI 消息查询只覆盖最近七天；重启恢复超出范围时截断并记录 `cli_history_limited_to_seven_days`。超过范围的停机缺口不能由当前实现补齐。
- `src/core/desktop_loop.py` 在调用模型前过滤应用自身回复、其他发送者、群聊、不匹配的应用会话和空文本。桌面登录用户是请求者时不因“本机发出”被误判为应用自身消息。
- 默认去重键严格采用 `(chat, sha256(原始 content), ts // 5)`，不裁剪原文。相同会话、相同内容、同一五秒桶会被视为重复；这不是 CLI 提供的稳定消息 ID。
- `src/core/desktop_store.py` 在模型调用前提交 SQLite claim，记录 `chat/sender/content/ts/chat_type/is_self`，再保存完整 `reply/status/error`。`dry_run` 只打印拟回复并记录结果，不调用 sender。
- LLM HTTP 每次 timeout 为 10 秒，失败重试一次；循环层另设 25 秒模型预算，不重复增加模型重试。模型错误保存为 `llm_failed`，终端输出错误保存为 `emit_failed`，后续消息继续处理。
- 已 claim 的失败、取消或中断消息不会自动重放，重启也保留去重。若输出已经发生但 `finish` 持久化失败，会记录 `desktop_outcome_persistence_failed` 及对应结果边界，并向上报错，不将该轮宣称为完成。
- 保留的发送分支连续失败三次后冷却 60 秒；同进程未处理的 pending 消息保留在内存中，冷却后优先处理，避免新轮询覆盖。该 pending 本身不是独立持久化队列，进程退出后的补读仍受上述 SQLite 恢复窗口和七天限制约束。

结构化日志由 `src/core/logging.py` 提供，处理中的模型、循环和发送日志通过 `msg_id` 关联；不输出模型 API Key、请求正文、响应正文或原始外部异常。Prompt 正文只放在 `prompts/`，通过配置中的模板名加载。

## 兼容路径：原回调服务配置与启动

以下保留原自建应用回调及 API 能力，不是本地 dry-run 的启动前提。编辑 `.env` 中 `WECOM_CORPID/WECOM_SECRET/WECOM_TOKEN/WECOM_AES_KEY/WECOM_AGENT_ID`，并启动 `REDIS_URL` 对应的 Redis。回调 EncodingAESKey 必须是有效的 43 字符密钥；占位符需要替换。

```bash
uvicorn src.main:app --port 8000
```

也可以使用读取 `APP_PORT` 的直接启动方式：

```bash
python -m src.main
```

```bash
python src/main.py
```

使用 Uvicorn CLI 启动时，端口由 `--port` 参数控制。

## 兼容路径：健康检查

服务启动后，在另一个终端执行：

```bash
curl --fail http://127.0.0.1:8000/health
```

返回：

```json
{"status":"ok"}
```

在已激活虚拟环境的终端执行静态检查：

```bash
ruff check src tests adapters prompts agent_loop.py smoke smoke.py
```

## 兼容路径：消息模型与 Redis Stream 队列

`src/core/models.py` 提供 `WecomMessage` 和 `AgentReply`。事件消息缺失或传入空的 `msg_id` 时，使用 `from_user:ts` 生成去重键；`ts` 对应消息创建时间。本 Phase 统一使用 `chat_type="single"`，非文本消息的 `content` 统一设为 `None`。非事件消息必须提供 `msg_id`。

`src/core/queue.py` 提供异步 `MessageQueue`，将消息 JSON 存入 Stream 的 `payload` 字段。首次消费从 ID `0` 创建消费组，包含创建组之前发布的消息；已有消费组继续使用原进度。

一个队列实例只绑定一个消费组，调用 `consume` 开始迭代后才能调用 `ack`。自动 ACK 在生成器恢复执行或显式关闭时发生，首次取得消息时它仍是 pending。提前退出消费时使用 `contextlib.aclosing` 关闭生成器，使用结束后调用 `await queue.aclose()` 释放连接。损坏 payload 会记录不含消息正文的日志并 ACK，随后继续消费。消费仅读取新消息，不自动恢复中断消费者留下的 pending 消息。

运行 3 个队列测试（开发依赖包含 `fakeredis`，无需启动 Redis 服务）：

```bash
pytest tests/test_queue.py
```

## 兼容路径：企业微信回调

`src/main.py` 已挂载 `/wecom/callback`，并通过 lifespan 创建共享队列和去重 Redis 客户端，在关闭时释放连接。客户端惰性连接，GET URL 验证不访问 Redis；`/health` 仍返回 `{"status":"ok"}`。

- `GET /wecom/callback`：将查询参数传给 `VerifyURL`，以 `text/plain` 原样返回解密后的明文字节，不添加引号或换行。验证失败返回 HTTP 400 空串并记录 error 日志。
- `POST /wecom/callback`：执行 `DecryptMsg`、ElementTree XML 解析、`WecomMessage` 映射、Redis 去重和 `await queue.publish()`，然后返回 HTTP 200 空响应体。不调用 LLM，不执行业务处理，不使用后台入队任务。
- 字段映射：`MsgId`、`MsgType`、`FromUserName`、`Content`、`CreateTime` 分别对应 `msg_id`、`msg_type`、`from_user`、`content`、`ts`。本接入层缺少 MsgId 时显式生成 `FromUserName-CreateTime`，覆盖直接构造模型时的默认 ID 格式。`raw` 保留原始字段和嵌套结构。
- `voice/image/file/event` 也入队，`content=None`。生产回调只负责入队；主动发送接口见下节。临时冒烟消费者对非文本消息发送“暂不支持该消息类型，请发送文本”，该消费者只在冒烟流程中启用。
- 去重使用一条 `SET key 1 NX EX 300`，键格式为 `{queue_key}:dedup:{corpid}:{msg_id}`。重复请求直接返回 200，不再次入队，不刷新过期时间。
- GET 和 POST 的本地处理预算分别为 0.8 秒、4 秒；同步验签、解密和 XML 解析在线程中执行。超时后线程可能继续完成计算，但线程中没有 Redis 写入或入队操作。
- 按当前 POST 错误响应政策，解密、解析、Redis 或入队失败及超时均记录 error 并返回 200 空串。日志不输出密钥、签名或消息正文。

去重与入队是两个独立操作：SET 成功后入队失败或进程退出，300 秒内的重试可能被当作重复确认；并发重复请求也可能先于原请求入队完成而得到 200。XADD 超时属于结果未知。本实现保留去重键直到过期，不承诺无丢失或恰好一次投递；POST 的 200 不能单独作为已经入队的证明。

## 兼容路径：主动发送文本消息

`src/gateway/wecom_client.py` 提供 `WecomClientProtocol`、`WecomClient` 和 `WeComAPIError`。使用 `async with WecomClient(corpid, secret, agent_id) as client` 管理连接，再调用 `await client.send_text(user_id, content)`。注入的 `httpx.AsyncClient` 仍由调用者负责关闭。

- 使用 `httpx.AsyncClient` 请求 `gettoken` 和 `message/send`。文本请求包含 `touser`、`msgtype="text"`、`agentid`、`text.content` 和 `safe=0`。
- access_token 保存在同一客户端实例的内存中，按 `expires_in` 计算失效时间，未返回该字段时使用 7200 秒，并提前 300 秒刷新。`asyncio.Lock` 保证并发调用只有一个协程刷新；复用同一实例才能共享缓存和锁。
- 每次 HTTP 请求使用 10 秒 timeout；网络、HTTP 状态或响应解析失败重试 1 次，耗尽后抛出 `WeComAPIError(-1, errmsg)`。企微业务错误保留 `errcode` 和 `errmsg`。
- 发送返回 `42001` 或 `40014` 时强制刷新 token 后重试 1 次；再次失败则抛出 `WeComAPIError`。其他非零业务错误码直接抛出，不改写错误码或绕过企微配置限制。
- 消费者通过 `structlog.contextvars.bound_contextvars(msg_id=message.msg_id)` 关联发送日志；直接调用未绑定 `msg_id` 时生成 `outbound-` 开头的关联 ID。

新增运行依赖 `structlog` 用于结构化 JSON 日志及跨异步调用的 `msg_id` 追踪。HTTP 单测复用已有 `httpx.MockTransport`，未新增 `respx`；Redis 单测使用开发依赖 `fakeredis`，不需要真实 Redis。

## 自动测试与离线冒烟

在项目目录、已激活虚拟环境的终端执行：

```bash
pytest
ruff check src tests adapters prompts agent_loop.py smoke smoke.py
python smoke.py
```

本轮本地验证记录：`pytest -v` 收集 491 / 通过 491，0 failed；`ruff check src tests adapters prompts agent_loop.py smoke smoke.py` 通过，格式检查通过；手动运行 `python smoke.py` 的三部分均通过，真实发送次数为 0。个人桌面读取模块有 101 个独立测试，涵盖原生纯逻辑、协议解析、身份与时间拒绝、超时和取消；真实读取及定点 dry-run 已通过，结果见文首。`Settings.Config` 仍产生 1 条原有 Pydantic 弃用警告。此前验证过 editable 安装、离线入口、兼容快照 wheel 打包及 `.env` 排除；这些打包检查未在本轮重复执行。本人账号真实发送和新消息实机刷新仍未验收。

`smoke.py` 先运行桌面离线检查：fake inbox、mock LLM HTTP、临时真实 SQLite、白名单过滤、完整 dry-run 回复及重启去重；再运行兼容回调检查：测试配置、真实 SDK 加解密、FastAPI ASGI 路由、fakeredis Stream、`httpx.MockTransport`、文本 echo、图片固定话术、重复回调去重、token 过期刷新与缓存。最后运行个人聊天离线检查：fake history、分钟时间契约、mock LLM、临时 SQLite 和重新读取去重。三部分均不需要 `.env` 或真实 Redis，不发送真实企微消息；输出 `desktop smoke: PASS`、`smoke.py: PASS` 和 `personal smoke: PASS`。

## 兼容历史：真实 API 与本地回调链路冒烟

本节保留此前联调记录。公网回调方案已停止作为本轮 MVP 接入路径，当前阶段不执行下列真实发送命令；仅在用户另行确认启用真实发送后使用。本地 dry-run 不要求补齐回调密钥或公网配置。

先填写真实 `.env`，启动该配置对应的 Redis，并确保本地端口 8000 空闲。企业微信应用需允许向实际成员 `LiChengYun` 发送消息，包括企业可信 IP、应用可见范围和凭证配置。

```bash
python -m smoke.smoke_api --user-id LiChengYun
python -m smoke.smoke_loop --user-id LiChengYun
```

两个命令成功完成时共发送 3 条真实消息：`smoke_api` 发送 `smoke-test-1`；`smoke_loop` 发送文本原文 echo 和图片固定话术各 1 条。失败重试遇到网络结果未知时，不能保证真实投递数量仍为 3 条。

`smoke_api` 先检查 `gettoken`，再发送文本，错误输出保留 `errcode` 并脱敏 `errmsg`；对 `60020` 提示可信 IP 并查询出口公网 IP，对 `81013` 提示成员可见范围或大小写，对 `40001/42001` 提示凭证或票据问题。

`smoke_loop` 启动临时本地 Uvicorn，使用现有 SDK 生成有效加密文本与图片回调，复用生产回调路由和 Redis Stream，再重放同一文本回调检查去重。本次消费组先在 Stream 尾部创建，避免扫描历史消息。临时消费者只处理本次生成的两个白名单 `msg_id` 且发送者为 `LiChengYun` 的消息；其他消息不会触发发送。临时消费者只在该脚本的应用工厂中挂载，`uvicorn src.main:app --port 8000` 的生产启动行为不增加消费者。脚本结束时关闭临时服务并清理本次消费组。

发送 API 返回成功及 `action="reply_done"` 日志只确认企微 API 接受，不代表客户端已经收到；实际收信需人工确认。真实 API、本地回调链路和客户端实收的结果按实际运行分别确认，本说明不将其标记为通过。

2026-10-17 真实环境执行记录：

- 环境配置已读取，真实 Redis 返回 `PONG`。
- `python -m smoke.smoke_api --user-id LiChengYun`：gettoken 失败，退出码 1。服务端返回 `errcode=40001`，`errmsg=invalid credential, hint: [1792201223445412785268306], from ip: 64.32.27.9, more info at https://open.work.weixin.qq.com/devtool/query?e=40001`。未进入 `send_text`。
- `python -m smoke.smoke_loop --user-id LiChengYun`：退出码 1，`stage=encrypt`、`errcode=None`。配置中的 EncodingAESKey 未满足 SDK 的 43 字符且 Base64 解码后 32 字节要求；未启动本轮服务，echo、图片回复和真实 Redis 去重验收均未执行。
- 该次真实发送消息数为 0。随后方案已调整为本地 dry-run；当前不继续此回调联调。未来启用官方应用发送时仍需单独核验企业 ID 与应用 Secret。

本轮修复包含独立单测：共享 `.env` 额外字段兼容、Python 3.11 下 Redis 消费循环取消请求的传播、临时消费组从 Stream 尾部启动、子进程退出竞争的清理，以及 SDK 密钥配置异常的安全提示。未修改企微路由、字段映射、响应行为或凭证值，未绕过服务端错误码。

## 兼容路径：回调失败路径检查

服务启动后，用伪造参数检查失败路径：

```bash
curl --silent --show-error --include --get http://127.0.0.1:8000/wecom/callback \
  --data-urlencode 'msg_signature=fake' \
  --data-urlencode 'timestamp=1700000000' \
  --data-urlencode 'nonce=fake' \
  --data-urlencode 'echostr=fake'
```

```bash
curl --silent --show-error --include \
  'http://127.0.0.1:8000/wecom/callback?msg_signature=fake&timestamp=1700000000&nonce=fake' \
  --header 'Content-Type: application/xml' \
  --data-binary '<xml><ToUserName>fake</ToUserName><Encrypt>fake</Encrypt></xml>'
```

伪造 GET 预期返回 400 空响应体，伪造 POST 预期返回 200 空响应体，均不返回 500。成功验证需要企业微信签名或使用测试配置生成的有效加密请求。

## 依赖替换记录（2026-10-17）

- 更换原因：原指定包 `WXBizMsgCrypt3` 的[官方 PyPI 查询接口](https://pypi.org/pypi/WXBizMsgCrypt3/json) 返回 HTTP 404，原安装命令报 `No matching distribution found for WXBizMsgCrypt3`。
- 第一级核查：未查到该包的更名记录或官方推荐的 PyPI 替代包；企业微信官方文档页本次未能读取，官方推荐关系未确认。
- 最终选择：按第二级选用第三方 SDK `wechatpy[cryptography]==1.8.18`，通过其 `cryptography` extra 安装加解密后端。[PyPI](https://pypi.org/project/wechatpy/) 提供稳定版，项目[上游仓库](https://github.com/wechatpy/wechatpy)约有 4,300 stars，[提交记录](https://github.com/wechatpy/wechatpy/commits/master/)包含 2026-03-07 的更新。稳定版发布时间为 2021-11-11；上游仓库更新与稳定版发布是两项独立记录。
- 接口适配：`src/gateway/wecom_crypto.py` 提供 `WXBizMsgCrypt(token, aes_key, corpid)`、`VerifyURL` 和 `DecryptMsg`。ReceiveId 使用 corpid，成功返回 `(0, bytes)`，失败保留第三方 SDK 异常；回调层同时处理非零返回码。签名校验和加解密全部委托 `wechatpy`，未自研算法。
- 导入差异：实际从 `src.gateway.wecom_crypto` 导入适配器，没有使用不可安装的 `WXBizMsgCrypt3` 包，也没有创建冒充该第三方包的模块。`GET /health` 的响应格式保持不变。
- 验证命令：安装运行 `pip install -e ".[dev]"`；静态检查运行 `ruff check src`；启动服务后运行 `curl --fail http://127.0.0.1:8000/health`。加解密依赖的验证命令见下方。

在已激活虚拟环境的终端运行以下命令。稳定版的企业微信加解密入口为 `wechatpy.enterprise.crypto.WeChatCrypto`，此处直接调用第三方库，使用的密钥与企业 ID 均为测试数据。

```bash
python - <<'PY'
import base64
from xml.etree import ElementTree

from wechatpy.enterprise.crypto import WeChatCrypto
from wechatpy.enterprise.exceptions import InvalidCorpIdException
from wechatpy.exceptions import InvalidSignatureException

key = base64.b64encode(bytes(range(32))).decode().rstrip("=")
crypto = WeChatCrypto("test-token", key, "ww-test-corp")
other_corp = WeChatCrypto("test-token", key, "ww-other-corp")
nonce, timestamp = "test-nonce", "1700000000"

for text in ("hello", "企业微信消息", "x" * 1024):
    plaintext = f"<xml><Content><![CDATA[{text}]]></Content></xml>"
    envelope = crypto.encrypt_message(plaintext, nonce, timestamp)
    root = ElementTree.fromstring(envelope)
    signature = root.findtext("MsgSignature")
    encrypted = root.findtext("Encrypt")
    assert crypto.decrypt_message(envelope, signature, timestamp, nonce) == plaintext
    assert crypto.check_signature(signature, timestamp, nonce, encrypted) == plaintext

    bad_signature = ("0" if signature[0] != "0" else "1") + signature[1:]
    try:
        crypto.decrypt_message(envelope, bad_signature, timestamp, nonce)
    except InvalidSignatureException:
        pass
    else:
        raise AssertionError("Invalid signature was accepted")

    try:
        other_corp.decrypt_message(envelope, signature, timestamp, nonce)
    except InvalidCorpIdException:
        pass
    else:
        raise AssertionError("Wrong corp_id was accepted")

print("PASS: round-trip, URL verification, invalid signature and wrong corp_id")
PY
```

本次验收结果（Python 3.11）：editable 安装成功；`ruff check src` 通过；`pip check` 无依赖冲突；Uvicorn 启动后 `/health` 返回 HTTP 200 和 `{"status":"ok"}`；上述加解密脚本通过。

上述依赖验证覆盖本地加解密兼容性和本地 HTTP/Redis 回调链路，不等同于真实企业微信后台 URL 配置或客户端收信确认。主动发送客户端已实现，真实环境的验收方式见“真实 API 与本地回调链路冒烟”。
