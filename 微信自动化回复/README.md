# macOS 微信自动回复

新增白名单、长短期记忆、语音及文件处理已集成到现有项目。完全离线运行入口为 `agent_loop.py --offline-assistant`；[使用方式与验证边界](docs/offline-assistant.md)。

当前个人微信入口为 `--wechat-native`：在微信 2 的 Ri 独立单聊中读取新文字消息，经模型生成回复，再以本人账号在原会话提交。默认 dry-run，显式添加 `--live` 才启用真实发送。

**运行状态：2026-10-21 本轮实机验收结束后，监听进程已按用户要求停止。** 下列命令仅在手动执行时启动程序。

## 联系人配置与身份边界

Ri 是人工指定并已完成本轮实机测试的联系人显示名。目前该目标被固定在代码中，程序只处理微信 2 的 Ri 独立单聊，不具备自动发现联系人、回复所有人或同时监听多个会话的能力。

| 项目 | 当前行为 |
| --- | --- |
| 联系人来源 | 人工指定测试联系人 Ri；需手动打开其独立窗口 |
| 会话定位 | 指定应用路径和 bundle ID，核对进程、唯一 Ri 窗口、窗口标题及几何位置 |
| 本地消息身份 | `chat_identity("Ri")` 生成 `desktop:wechat-ocr:Ri`，仅用于本地路由和去重，不是微信官方用户 ID |
| `WECHAT_OCR_CONTACT` | 原生入口只接受空值或 `Ri`；填写其他名字会拒绝启动，空值仍固定使用 Ri |
| `PERSONAL_CONTACT_NAME` | 属于企业微信兼容入口；不能用来改变原生入口的目标 |
| 同名联系人 | 未验证唯一账号身份区分能力；窗口标题不是官方身份凭据，不应仅凭同名窗口启动真实发送 |
| 其他联系人与群聊 | 不在当前实现与实机验收范围 |

固定限制的代码位置：

- `agent_loop.py` 的 `_run_wechat_native_locked`：限制配置名称，并把 `allowed_chat`、`allowed_sender` 固定为本地 Ri 身份。
- `adapters/wechat_native.py` 的 `IDENTITY`、`parse_native_frame`：固定 Ri 本地身份，核对 OCR 标题。
- `adapters/wechat_native_worker.py` 的 `NativeSession`：定位 Ri 窗口，核对原生窗口标题并执行受限操作。

**换联系人不能只改 `.env`，也不能只替换一个窗口标题。** 需要同步修改目标配置契约、原生窗口定位、OCR 标题、收发白名单和本地消息身份，并保留既有 SQLite 记录；新联系人的历史基线、去重、单次发送和未知结果不重发都需要重新验证。以上改造尚未实现。

## 环境与安装

- 已实机验收的客户端为 `/Applications/微信 2.app`，版本 4.1.15，bundle ID 为 `com.tencent.xinWeChat2`；其他版本未验证。
- macOS、Python 3.11 或更高版本，以及可访问的 OpenAI Chat Completions 兼容模型服务。
- 运行 Python 的宿主需具备屏幕录制、辅助功能权限；System Events 窗口操作还需对应自动化授权。更换终端或宿主后的权限链需重新核验。
- 微信 2 已登录，Ri 独立窗口已打开，输入区为空。运行期间保持该窗口前台，不人工操作鼠标键盘。

在本目录安装依赖；已有 `.env` 时保留原文件，不重复复制：

```bash
cd /Users/leecy/Desktop/wecom-ai-assistant/微信自动化回复
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

首次配置时复制 `.env.example` 为 `.env`，填写 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`，保持 `DRY_RUN=true`。密钥仅保存在本地，不提交到 Git。原生截图入口不要求填写 wx-cli 账号配置。

## 微信 2 内置截图自动回复（Ri）

入口为 `--wechat-native`，复用 `.env` 中已配置的模型地址、密钥、模型名和 `WECHAT_OCR_TEMPLATE_NAME`。只支持 `/Applications/微信 2.app`（`com.tencent.xinWeChat2`）已打开的 `Ri` 独立单聊。先在联系人列表双击 Ri 打开独立窗口。

```bash
cd /Users/leecy/Desktop/wecom-ai-assistant/微信自动化回复
DB_URL=sqlite:///./logs/wechat-native.db .venv/bin/python agent_loop.py --wechat-native --once
DB_URL=sqlite:///./logs/wechat-native.db .venv/bin/python agent_loop.py --wechat-native --live
```

第一条为不发送的单轮预检；第二条启用本人原会话自动回复。也可双击 `启动微信自动回复.command`，启动真实自动回复，轮次之间等待 5 秒。在启动终端按 Control+C 停止；跨进程锁阻止两个 wx2 桌面执行器同时运行。

启动时将 Ri 独立窗口设为 598×640 并居中，读取当前可见消息作为基线，不补发旧消息或旧草稿。后续只处理严格尾部追加的对方文字，忽略本人消息；保留原始显示时间，不补造年份和秒数。所有入站 claim、回复和结果持久化到指定 SQLite，重复消息和未知发送不会重发。

采集使用 Control+Command+A 打开微信内置截图覆盖层，只采集 Ri 窗口矩形，随后退出截图。OCR 在本机运行。运行中保持 Ri 独立窗口在前台且位置不变，不要同时操作它；切换应用、切换窗口、移动窗口、出现人工草稿或消息前缀不再完整时，该轮不发送。窗口截图会短暂显示截图选区界面。

采用原生 Unicode 按键输入，不读写系统剪贴板。发送依次校验精确进程/窗口、Ri 标题、空白输入区、完整草稿、历史未变和唯一发送按钮，再点击发送一次。只有新增本人绿色气泡与草稿匹配且输入区清空，才记录 `sent`（仅表示本机提交确认）。文字写入开始后的超时、取消和不确定结果记录 `send_unknown`，不自动重发。

支持范围为已验证布局中的可辨认文字。图片、语音、视频、表情、群聊、后台/最小化和其他联系人不在本次验收范围；OCR 歧义与缺少时间分隔时停止该轮。可见历史因正常滚动不完整时，程序最多向上滚动 10 次，重新核对完整已保存前缀与唯一重叠，再返回底部；无法恢复完整前缀时暂停，不把残缺视口直接当作新历史。

2026-10-21 已实机确认：Ri 独立窗口可打开、正文可采集，识别出 2 条对方及 3 条本人消息；固定测试文字已在本机提交并确认新增气泡。19:39 的新消息“你是谁”触发真实模型回复并在原会话提交；首次回执因头像和截断行 OCR 误判而记录未知，修复后只读核对确认完整 5 条前缀、新增 1 条匹配本人气泡和空白输入区，更新为 `sent`，没有重发。完整事实、旧未知记录及未验证范围见 `docs/native-wechat-validation.md`。


## 兼容入口与证据边界

| 入口 | 接入方式 | 本次状态 |
| --- | --- | --- |
| `--wechat-native` | 微信 2 内置截图、本地 OCR、原生输入与单次发送 | 本次实机跑通；最终回执经过修复后只读核对 |
| `--wechat-ocr` | wx-cli 历史变化检测与窗口 OCR | 账号初始化阻塞，未完成本次实机闭环 |
| `--personal` | 企业微信 Ri 外部单聊的已打开聊天记录面板 | 保留兼容入口，不作为微信 2 的启动前提 |
| `--wechat` / `--wechat-login` | 官方 iLink 机器人通道及登录 | 保留兼容入口；不作为本人原会话发送的验收证据 |
| 默认 CLI、FastAPI、Redis Stream、自建应用 | 原有 CLI 与服务端兼容模块 | 本次微信 2 路径不依赖这些模块 |

2026-10-21 实测 wx-cli 0.3.0 的 `init` 与 `init --force` 均返回“未能自动检测到微信数据目录”，未生成 wx2 密钥文件，`WX_CLI_ACCOUNT_VERIFIED` 继续保持 `false`。旧 OCR 路径要求实际核实 `config.json`、wx2 数据目录、密钥、账号与历史元信息；不能通过改成 `true` 绕过核验，也不会回退读取另一账号。

企业微信兼容入口需先打开 `Ri @微信` 单聊中的“查找聊天记录”，清空搜索框并保持默认筛选：

```bash
.venv/bin/python agent_loop.py --personal
```

上式默认为 dry-run。该路径的独立授权 live 入口是 `--personal --live`，不与 `--wechat-native` 混用；群聊和其他联系人不在授权范围。企微日志查看器为 `.venv/bin/python -u watch_logs.py`，只读日志和数据库。

## 实现入口

| 文件 | 职责 |
| --- | --- |
| `agent_loop.py` | `run_wechat_native`：运行锁、模型配置、启动基线和消息循环 |
| `adapters/wechat_native.py` | `NativeRiChat`：OCR 快照、历史衔接、收件和发送回执 |
| `adapters/wechat_native_worker.py` | `NativeSession`：隔离的原生窗口、截图、滚动与键鼠操作 |
| `adapters/personal_tail.py` | `PersonalTailInbox`：历史基线与严格尾部追加 |
| `prompts/wechat_ocr_reply.txt` | 原生截图路径的回复提示词，由配置模板名加载 |
| `tests/test_wechat_native*.py` | 原生路径的单元测试与运行入口测试 |
| `smoke/smoke_wechat_native.py` | 模拟截图、mock 模型、真实 SQLite 与模拟发送的离线闭环 |

```mermaid
flowchart LR
    A[Ri 独立窗口] --> B[内置截图与本地 OCR]
    B --> C[历史基线与新增消息校验]
    C --> D[SQLite 去重与 claim]
    D --> E[模型生成回复]
    E --> F{显式 live}
    F -->|否| G[记录草稿]
    F -->|是| H[核对窗口与空白输入区]
    H --> I[原生输入与草稿核对]
    I --> J[点击发送一次]
    J --> K[新增本人气泡与空白输入区核对]
    K --> L[记录 sent 或 send_unknown]
```

## 运行记录与验收

| 文件 | 内容 |
| --- | --- |
| `logs/wechat-native.db` | 入站基线、去重、模型回复与发送结果 |
| `logs/wechat-native.log` | 本轮直接启动进程的运行日志；手动前台命令默认输出到终端 |
| `logs/wechat-native-process.json` | 本轮直接启动进程的 PID 与状态；不是所有启动方式的统一进程管理器 |
| `logs/native-channel-manual-test.json` | 固定文字发送通道的实机记录 |
| `logs/native-auto-reconciliation.json` | 自动回复首次未知状态及后续只读核对记录 |

[完整实机验收记录](docs/native-wechat-validation.md)保留测试经过、历史未知发送和未验证项。2026-10-21 的实现检查记录为 pytest 收集 835 / 通过 835、ruff 通过、离线冒烟通过；这是该次检查结果，后续修改需重新执行。

双击 `启动微信自动回复.command` 会请求运行 live，但 Finder → Terminal 的双击启动与权限链尚未完成实机验收。该脚本只通过了语法检查；已验证的真实运行使用本会话直接启动的进程。在启动终端按 Control+C 停止后，需确认进程退出。停止不会删除 SQLite 去重和未知发送记录。

## 配置

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `DRY_RUN` | 是否只生成草稿。个人入口以 `--live` 显式开启发送 | `true` |
| `INBOX_BACKEND` | 接收后端；个人桌面接入设为 `personal`，或使用 `--personal` | `cli` |
| `PERSONAL_CONTACT_NAME` | 已验证的个人单聊显示名 | `Ri` |
| `LLM_BASE_URL` | OpenAI Chat Completions 兼容 API 地址 | 无 |
| `LLM_API_KEY` | 模型服务密钥，仅保存在本地 `.env` | 无 |
| `LLM_MODEL` | 模型名称 | 无 |
| `PERSONAL_LLM_TEMPLATE_NAME` | `prompts/` 中的个人回复模板文件名 | `personal_reply.txt` |
| `DB_URL` | SQLite 处理记录数据库 | `sqlite:///./agent.db` |
| `POLL_INTERVAL_SECONDS` | 轮询间隔（秒） | `2` |
| `WECHAT_ILINK_ALLOWED_USER` 等 iLink 变量 | 仅官方 iLink Bot API 路径（`--wechat`）需要 | 见 `.env.example` |
| `WECHAT_OCR_CONTACT` | 旧 OCR 路径的联系人配置；原生入口只接受空值或 `Ri`，不支持任意联系人 | 无 |
| `WECHAT_OCR_TEMPLATE_NAME` | `prompts/` 中的 OCR 回复模板文件名 | `wechat_ocr_reply.txt` |
| `WX_CLI_WORKDIR` | 已核实的 wx2 CLI 配置目录 | `.` |
| `WX_CLI_ACCOUNT_VERIFIED` | wx2 数据来源和当前账号已实际核实 | `false` |

## 测试

```bash
source .venv/bin/activate
pytest -v
ruff check .
python smoke.py
```

`smoke.py` 默认只执行离线验证，不会操作真实微信或企业微信，不会发送真实消息。


## 自动回复流程图

[打开交互式流程图](docs/diagrams/ri-auto-reply.html)（支持缩放、明暗主题和导出）。源文件与 HTML 均保存在项目 `docs/diagrams/` 中。
