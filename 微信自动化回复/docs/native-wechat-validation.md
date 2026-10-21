# 微信 2 Ri 自动回复实机验收

日期：2026-10-21（Asia/Shanghai）。客户端：微信 2 4.1.15，路径 `/Applications/微信 2.app`，bundle ID `com.tencent.xinWeChat2`。

## 交付清单

| 项目 | 文件与入口 |
| --- | --- |
| 原生 UI 隔离进程及 Protocol | `adapters/wechat_native_worker.py`：`NativeSessionProtocol`、`NativeSession` |
| OCR、历史衔接、收件和单次发送及 Protocol | `adapters/wechat_native.py`：`NativeBackend`、`NativeChatProtocol`、`NativeRiChat` |
| 自动回复运行入口 | `agent_loop.py`：`run_wechat_native`、`_run_wechat_native_locked`；参数 `--wechat-native`、`--live` |
| 提示词 | `prompts/wechat_ocr_reply.txt`，从现有 config 模板名加载 |
| 手动离线冒烟 | `smoke/smoke_wechat_native.py`，已纳入 `smoke.py` |
| 单元测试 | `tests/test_wechat_native.py`、`tests/test_wechat_native_worker.py`、`tests/test_wechat_native_runtime.py`、`tests/test_smoke_wechat_native.py` |
| 启动脚本 | `启动微信自动回复.command` |

调用链：`run_wechat_native` → 全进程桌面锁 → `NativeRiChat.activate` → `PersonalTailInbox.baseline` → `AgentLoop.run_once` → `NativeRiChat.poll_unread` → SQLite claim → `HTTPReplyModel.reply` → `NativeRiChat.send_text` → 原生 Unicode 输入 → OCR 核对 → 点击发送一次 → 新增本人气泡核对 → SQLite 结果。

## 已完成的实机验证

1. 根据内置截图的联系人行坐标双击 Ri，原生窗口及 AX 均出现标题 `Ri`。
2. 内置截图可取得 Ri 独立窗口正文；本地 OCR 识别出启动前 2 条对方、3 条本人消息。原来的直接窗口采集白图问题仍存在，新路径使用内置截图覆盖层。
3. 手动输入并提交“自动回复功能测试，请忽略。”一次，截图确认新增本人绿色气泡和空白输入区。
4. 当前配置的模型 HTTP 接口连接测试成功；单元测试中的模型调用全部 mock。
5. 第一次监听跳过 5 条历史。18:53 的“你好”使原视口历史前缀不完整，程序暂停，未调用模型或自动发送。
6. 历史衔接修复后，从此前真实的 5 条快照重新核对完整前缀，恢复为 6 条；唯一新增为 18:53 的对方消息，没有把旧记录当新消息。
7. 第二次监听对 19:16 的“你是谁”调用模型并写入草稿；截图退出未完成，记录 `send_unknown`，没有自动重试。其未知记录和原回复保留在 SQLite。该草稿中包含内部账号名，提示词已修正为不透露内部配置、被问身份时说明使用自动回复；旧草稿未补发。
8. 在 Ri 输入框实测原生 Unicode 输入，完整匹配测试文字；清理本次测试草稿后，确认输入框显示客户端空白提示。输入路径不再读写剪贴板。
9. 修复后的 `NativeRiChat.send_text` 实机执行“自动回复链路测试，请忽略。”：原生输入、OCR 草稿核对、一次发送、新增本人气泡核对均完成，记录 `local_submit_confirmed`。此项为固定正文的通道验证，不计作模型自动回复验收。
10. 最终监听重新以当前 4 条可见历史建立基线；旧草稿和未知记录不补发。19:39 新消息触发真实模型回复并在原会话提交，完整核对结果见下方。

上述提交确认只表示本机观察到匹配的本人消息气泡，不表示对方已收到或已读。

## 持久化与运行

- 数据库：`logs/wechat-native.db`。沿用 `desktop:wechat-ocr:Ri` 命名空间和既有分钟去重键；没有删除或改写旧去重记录。
- 本轮后台进程记录：`logs/wechat-native-process.json`；运行日志：`logs/wechat-native.log`。
- 固定正文通道验证：`logs/native-channel-manual-test.json`。
- `.env` 未改为默认 live；普通入口仍为 dry-run，只有 `--live` 明确启用真实发送。
- 双击启动脚本在其终端运行；在该终端按 Control+C 停止。当前会话直接启动的后台 PID 另记于进程记录文件，可请求停止当前自动回复。
- 启动脚本通过 `zsh -n`。本次 `open -a Terminal` 返回 0，但未观察到该次启动产生持续进程；脚本的双击启动与终端权限链尚未完成实机验收。本轮实机运行使用本会话直接启动的后台进程，现已按用户要求停止。

## 约束与未验证项

- 只允许微信 2 的 Ri 独立单聊；启动时设为 598×640 并居中。后台、最小化、其他联系人和群聊未纳入验收。
- 运行中要求 Ri 在前台且位置不变。切换应用、切换窗口和人工草稿会阻止发送；程序不会持续抢回焦点。
- 只支持可确认的文字布局。图片、语音、视频和表情的识别与回复未验证。
- 只使用实际可见时间分隔标签，保留原始字符串，不补造年份、秒数或 epoch。无时间锚点时暂停。
- 视口不完整时，最多向上滚动 10 次核对完整保存前缀，再以唯一重叠衔接底部。缺失锚点、重复锚点、重排和无法恢复完整前缀时暂停。
- 同一正文、同一显示分钟沿用同一去重键，会合并；此限制未改变。
- 原生写入不重试。写入后取消、超时、草稿无法完整匹配或回执不确定保留 `send_unknown`。
- 依赖未新增、未替换；使用环境中已有的 PyObjC、RapidOCR、Pillow、HTTPX、SQLite 与 structlog。
- 未修改客户端文件、签名或 SIP，未读取客户端私有数据库或调用机器人、自建应用发送接口；未发布代码。
- 对照本轮基线，客户端 `Info.plist`、主可执行文件 `WeChat`、签名资源文件 `CodeResources` 三个文件 SHA-256 均未改变；未对全目录逐文件校验。

## 本次检查

- `.venv/bin/python -m pytest -q`：收集 835 / 通过 835；1 条既有 Pydantic class Config 弃用告警。
- `.venv/bin/ruff check .`：通过。
- `.venv/bin/python smoke.py`：通过，含新增 native wx2 闭环；全部离线模拟发送，真实发送数为 0。
- `.venv/bin/python -m pytest` 的旧 OCR 入口单测已把桌面进程锁隔离到临时目录；此前曾因单测与本机真实运行程序争用锁而失败，隔离后全量通过，生产锁未绕过。

## 自动回复实机结果

1. Ri 在原会话显示时间 `19:39` 发来“你是谁”。该入站按原始显示分钟生成去重键，SQLite 为 rowid 5。
2. 自动监听识别该新消息，真实调用当前配置的模型，生成“这是自动回复，我不是机主本人，也不是客服或工作人员。”，完成原生文字输入和一次发送。
3. 原发送后回执因头像被识别成“面”、以及滚屏时半行文字被识别成“4”而触发 `AMBIGUOUS_LAYOUT`，初始记录为 `send_unknown`；没有自动重发。
4. 已增加头像列和截断行过滤，保留对应计数；新增两条回归测试。可见正文、身份和草稿的置信度校验保持启用。
5. 使用修复后的读取与历史衔接重新只读核对：完整 5 条既有前缀保持一致，仅新增 1 条本人气泡，正文与模型回复匹配，输入区为空。核对过程发送尝试数为 0。
6. rowid 5 已由只读核对更新为 `sent`，`error` 保留 `read_only_reconciled_after_avatar_and_clipped_ocr`；原未知状态另存 `logs/native-auto-reconciliation.json`，日志保留原事件。19:16 的旧 `send_unknown` 不变，不补发。
7. 包含上述修复的后台监听曾以 PID 92386 运行，随后按用户要求发送 SIGINT 并确认退出。自动回复已停止；之后手动重启 live 时以当时可见消息建立新基线，不重复回复。

实机结果：新消息收件、真实模型生成、本人原会话提交已跑通；本次新增气泡的最终确认采用修复后只读核对。只确认本机提交，不确认对方收到或已读。
