# TraceMemo 个人微信 Mac 接入源码核查

核查日期：2026-10-21。目标客户端：用户选择的“微信 2”，版本 4.1.15、build 270100、bundle ID `com.tencent.xinWeChat2`；本机 macOS 26.6.2、arm64。以上环境信息由主代理本次只读核实。本报告没有运行第三方代码、安装软件、读取私人聊天/数据库密钥、操作微信窗口或发送消息。

源码版本与核查快照：

- 自动回复 fork：[`dustPyrotechnic/wechat-auto-reply-fork@cdf5c43ce33cbc9a4d51e3a477193373d942a3e5`](https://github.com/dustPyrotechnic/wechat-auto-reply-fork/tree/cdf5c43ce33cbc9a4d51e3a477193373d942a3e5)，主分支最新提交 2026-08-27。
- TraceMemo：[`Wxw-Gu/TraceMemo@c366dd11871d75d5f511e843605b689731759622`](https://github.com/Wxw-Gu/TraceMemo/tree/c366dd11871d75d5f511e843605b689731759622)，2026-10-21 下载核查。后续 GitHub commits API 已确认该 commit 日期为 2026-10-15，提交标题为 Merge branch develop；对应 tree SHA 为 `b66110dab607250f01215c187843119f6fa441d2`。

证据等级：**源码核查**表示读取了上述 fork 固定提交或 TraceMemo 本次下载快照文件；**项目声明**表示仓库文档；**未验证**表示本次未完成实机或相应源码核查。

## 接入与客户端选择

| 项目 | 核查事实 | 来源 |
|---|---|---|
| 读取链路 | **源码核查**：`TraceMemoClient` 请求 `127.0.0.1:6131/api/v1` 的 `/health`、`/recent_chat`、`/chatlog`；Bearer Token；chatlog 30 秒超时，仅 ReadTimeout 重试一次 | [tracememo_poller.py](https://github.com/dustPyrotechnic/wechat-auto-reply-fork/blob/cdf5c43ce33cbc9a4d51e3a477193373d942a3e5/macos/tracememo_poller.py#L359) |
| 读取身份 | **源码核查**：`talker` 用于稳定会话白名单与去重，缺少明确消息方向的记录跳过；展示名另存 `chat_name` | [解析函数](https://github.com/dustPyrotechnic/wechat-auto-reply-fork/blob/cdf5c43ce33cbc9a4d51e3a477193373d942a3e5/macos/tracememo_poller.py#L284) |
| 发送链路 | **源码核查**：`Poller._send_reply()` 将 `message.chat_name` 传给 `WeChatSender.send()`；后者激活微信，优先当前会话/左侧列表，失败后搜索，OCR 校验右侧顶部完整显示标题，再清理输入区、粘贴和提交 | [发送调用](https://github.com/dustPyrotechnic/wechat-auto-reply-fork/blob/cdf5c43ce33cbc9a4d51e3a477193373d942a3e5/macos/tracememo_poller.py#L924)、[sender](https://github.com/dustPyrotechnic/wechat-auto-reply-fork/blob/cdf5c43ce33cbc9a4d51e3a477193373d942a3e5/macos/wechat_sender.py#L965) |
| 双开选择 | **源码核查**：AppleScript 硬编码 `process "WeChat"`，应用启动为 `open -a WeChat`，窗口取 `window 1`；未显式按目标 bundle ID、PID 或账号选择“微信 2” | [脚本常量](https://github.com/dustPyrotechnic/wechat-auto-reply-fork/blob/cdf5c43ce33cbc9a4d51e3a477193373d942a3e5/macos/wechat_sender.py#L59)、[启动函数](https://github.com/dustPyrotechnic/wechat-auto-reply-fork/blob/cdf5c43ce33cbc9a4d51e3a477193373d942a3e5/macos/wechat_sender.py#L186) |
| 身份边界 | **源码核查**：稳定 `talker` 不传给 GUI 发送器，显示标题匹配无法单独证明该窗口属于目标账号或同名联系人的唯一身份。**未验证**：双开时实际进程解析结果、微信 2 选中账号与 TraceMemo 数据账号一致性 | 同上 |
| 微信版本 | **项目声明**：TraceMemo 2.5.1 Mac 支持 arm64/x64，文档适配 4.1.13 系列，推荐 4.1.13.8、备选 4.1.8.100。**未验证**：4.1.15 build 270100 与改 bundle ID 的副本 | [首次使用](https://github.com/Wxw-Gu/TraceMemo/blob/c366dd11871d75d5f511e843605b689731759622/docs/user-guide/getting-started.md#L7)、[README](https://github.com/Wxw-Gu/TraceMemo/blob/c366dd11871d75d5f511e843605b689731759622/README.md) |

静态源码只能确认“没有显式绑定微信 2”，不能单凭进程名称断言实机必定不支持或必定选错。

## 发送确认、重试与人工接管

| 核查项 | 源码结果 |
|---|---|
| 发送前文本 | `_paste_and_confirm()` 先 OCR 确认目标标题，再点击候选输入位置，执行 Cmd+A/Delete，粘贴回复并 OCR 检查目标文本。纯 emoji 允许以剪贴板内容一致作为兜底，未证明输入框实际收到 emoji |
| 原草稿 | 输入前会清空内容；未以“原输入框必须为空”为门槛。已读发送路径没有检测人工输入或本人新回复后暂停任务的分支 |
| 最终动作 | `send()` 先 Return；0.55 秒后若 OCR 仍识别到草稿，继续点击发送按钮，因而可能产生两个最终提交动作 |
| 发送后 readback | 检查的是输入区是否仍含目标回复，未核验“新增本人出站气泡且正文匹配”；输入框清空不能证明对方收到 |
| 无法确认 | `_draft_present()` 返回 `None` 时，`send()` 只记 warning 并正常返回；poller 随即 `stats.sent += 1` 并记录“自动回复已发送”。没有将此分支持久化为 `send_unknown` |
| 异常不重发 | `SenderError(send_attempted=True)` 会清除待重试项，不自动重发；其他错误默认按未最终提交处理，最多安排三次重试、间隔三秒 |
| 启动历史 | 默认首轮用当前时间过滤历史；允许 `--replay-offline`。已存在会话的持久 `retry_state` 在新消息过滤之前执行，启动基线不会阻止这类旧草稿重试 |
| 去重持久化 | JSON 保存每会话最近 200 个 seen ID、会话集合、轮询时间和重试回复。批次调用引擎前先认领落盘 |
| 人工接管 | 提供服务停止/启动控制，退出控制 App 不会停止后台服务；未发现发送链路中的自动人工接管锁或全局用户输入优先门槛 |

来源：[粘贴核验](https://github.com/dustPyrotechnic/wechat-auto-reply-fork/blob/cdf5c43ce33cbc9a4d51e3a477193373d942a3e5/macos/wechat_sender.py#L804)、[发送后检测](https://github.com/dustPyrotechnic/wechat-auto-reply-fork/blob/cdf5c43ce33cbc9a4d51e3a477193373d942a3e5/macos/wechat_sender.py#L850)、[提交实现](https://github.com/dustPyrotechnic/wechat-auto-reply-fork/blob/cdf5c43ce33cbc9a4d51e3a477193373d942a3e5/macos/wechat_sender.py#L965)、[重试与启动](https://github.com/dustPyrotechnic/wechat-auto-reply-fork/blob/cdf5c43ce33cbc9a4d51e3a477193373d942a3e5/macos/tracememo_poller.py#L924)、[持久状态](https://github.com/dustPyrotechnic/wechat-auto-reply-fork/blob/cdf5c43ce33cbc9a4d51e3a477193373d942a3e5/macos/tracememo_poller.py#L458)、[控制 App](https://github.com/dustPyrotechnic/wechat-auto-reply-fork/blob/cdf5c43ce33cbc9a4d51e3a477193373d942a3e5/docs/macos-app.md)。

## 安装与运行副作用

- **源码核查**：`install-tracememo-autoreply.sh` 写入 `~/Library/LaunchAgents/com.wxauto.tracememo-autoreply.plist`，设置 `RunAtLoad`、`KeepAlive`，停止同名旧服务后立即 bootstrap。它并非仅创建文件。
- **源码核查**：真实发送 runner 固定加入 `--send --send-all`，由配置白名单约束会话；默认五秒轮询，读取 `var/replay-offline` 可启用历史追补。
- **源码核查**：`build-macos-helpers.sh` 调用 `swiftc` 生成 Vision OCR、鼠标点击、滚轮程序到 `.build/`；已读脚本未执行 sudo、SIP 设置或微信重签名。
- **源码核查**：发送器会激活窗口、改变会话/搜索/列表位置、覆盖剪贴板、清空目标输入内容，finally 尝试恢复剪贴板及先前前台应用。失败诊断可在 `var/wechat-sender-failures/` 保存窗口截图与 OCR 文本。
- **项目声明**：控制 App 保存配置会重启规则服务与自动回复服务；界面关闭不停止后台服务。

来源：[安装自动回复](https://github.com/dustPyrotechnic/wechat-auto-reply-fork/blob/cdf5c43ce33cbc9a4d51e3a477193373d942a3e5/scripts/install-tracememo-autoreply.sh)、[安装草稿模式](https://github.com/dustPyrotechnic/wechat-auto-reply-fork/blob/cdf5c43ce33cbc9a4d51e3a477193373d942a3e5/scripts/install-tracememo-poller.sh)、[真实发送 runner](https://github.com/dustPyrotechnic/wechat-auto-reply-fork/blob/cdf5c43ce33cbc9a4d51e3a477193373d942a3e5/scripts/run-tracememo-autoreply.sh)、[辅助程序构建](https://github.com/dustPyrotechnic/wechat-auto-reply-fork/blob/cdf5c43ce33cbc9a4d51e3a477193373d942a3e5/scripts/build-macos-helpers.sh)。

## TraceMemo 密钥与权限边界

**项目声明**：首次获取数据库密钥需退出微信账号、停留在未登录窗口，等待 TraceMemo 明确提示后再登录。文档要求仅在连接页面明确提示时处理 SIP，不能将关闭 SIP 写为所有 Mac 的固定前置条件。数据库读取权限错误与 SIP 不同；项目文档不建议以 sudo 启动应用或修改整个微信目录权限。

来源：[首次连接](https://github.com/Wxw-Gu/TraceMemo/blob/c366dd11871d75d5f511e843605b689731759622/docs/user-guide/getting-started.md#L44)、[macOS 数据访问权限](https://github.com/Wxw-Gu/TraceMemo/blob/c366dd11871d75d5f511e843605b689731759622/docs/platform/macos.md)、[SIP 说明](https://github.com/Wxw-Gu/TraceMemo/blob/c366dd11871d75d5f511e843605b689731759622/docs/mac-disable-sip.md)。

### 自动获取数据库密钥的源码事实

- **源码核查**：`KeyServiceMac.autoGetDbKey()` 在架构判断之前调用 `isSipEnabled()`；读取 `csrutil status` 判断为 enabled 时，直接返回 `SIP_ENABLED`。因此当前自动获取路径有 SIP 检查，不能写成“无需关闭 SIP”。读取 `csrutil` 出错时此检查返回 false；本次未执行该检查。手动粘贴已有密钥是另一路径。
- **源码核查**：arm64 路径用 `osascript` 的 `do shell script ... with administrator privileges` 运行 helper，会请求管理员授权。
- **源码核查**：`resolveXkeyHelperMode()` 只对 `4.1.13` 系列选择 `xkey_helper_4_1_13`；其他版本包括 `4.1.15` 都走 legacy `xkey_helper`。这不能证明 legacy 在 4.1.15 一定失败，也没有证据证明该组合可用。
- **源码核查**：`getWeChatPid()` 先 `pgrep -x WeChat`，再尝试匹配 `WeChat.app/Contents/MacOS/WeChat`，有多个 PID 时取最大值；未绑定 `com.tencent.xinWeChat2`、指定账号或数据目录对应的进程。
- **源码核查**：Intel 路径调用预编译 `intel_mac_key_helper`，状态字段包含 `sipDisabled`、`fridaAvailable`、`wechatAdhocSigned`。arm64 的 `xkey_helper` 与 `xkey_helper_4_1_13` 同样是仓库资源中的二进制。已读 TypeScript 只体现编排，不能证明原生工具内部无注入、调试或签名修改。
- **未验证**：是否实际需要重签目标微信、原生 helper 的全部系统调用、改 bundle ID 副本的兼容性、双开时进程与账号一致性。本次没有修改 SIP、请求管理员凭据或运行密钥工具。

来源：[key-service-mac.ts](https://github.com/Wxw-Gu/TraceMemo/blob/c366dd11871d75d5f511e843605b689731759622/src/main/key-service-mac.ts#L324)、[helper 版本选择](https://github.com/Wxw-Gu/TraceMemo/blob/c366dd11871d75d5f511e843605b689731759622/src/main/key-service-mac.ts#L28)、[对应单测文件，仅阅读未运行](https://github.com/Wxw-Gu/TraceMemo/blob/c366dd11871d75d5f511e843605b689731759622/tests/unit/key-service-mac.test.ts)。以上链接固定到本次确认的 commit；本次仅阅读测试文件，没有执行其测试。

## 未验证与检查结果

- 未验证本机微信 2 4.1.15 的数据库密钥获取、数据库解析、窗口账号选择、收件、发送和接管。
- 未运行任一安装/构建/启动脚本，未授予或变更系统权限，未修改 SIP、微信签名或微信文件。
- 未验证第三方打包应用和原生二进制内部行为。
- 两库根目录 `LICENSE` 的公开请求均返回 404；TraceMemo `package.json` 本次未发现 `license` 字段，tree 路径筛查未发现明显 license 文件。未确认可供再分发使用的完整许可证；这不等于对所有第三方文件许可作出结论。
- 纯源码调研，pytest、ruff、smoke.py 均未执行，不宣称测试通过。
