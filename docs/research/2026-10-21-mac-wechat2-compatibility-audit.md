# 微信 2 4.1.15：Mac 自动回复候选兼容核查

核查日期：2026-10-21。本文记录环境、源码行为和验证缺项，不包含安装或发送结果。

## 本机环境

本次使用系统版本信息、应用 `Contents/Info.plist` 与 Computer Use 应用清单进行只读核查：

| 项目 | 本次结果 |
| --- | --- |
| macOS / 架构 | 26.6.2 / arm64 |
| 用户选定客户端 | `/Applications/微信 2.app`，4.1.15，build 270100 |
| 目标 Bundle ID | `com.tencent.xinWeChat2` |
| 目标名称 / 可执行文件 | `CFBundleName`、`CFBundleDisplayName` 均为“微信 2”；`CFBundleExecutable` 为 `WeChat` |
| 目标运行状态 | Computer Use 应用清单显示运行中 |
| 另一客户端 | `/Applications/WeChat.app`，4.1.13，build 269602，`com.tencent.xinWeChat`；清单显示未运行 |
| 实际 UI 读取 | `cua.getApp("/Applications/微信 2.app")` 返回 `Computer Use was not approved to use 微信 2`；未获取 AX 树或聊天内容 |

工具访问未授权不能用于判断微信 AX 树是否存在、是否能读消息。静态源码的名称筛选也不能单独证明本机运行时必定失败或必定选错账号。

## 源码范围

| 候选 | 固定版本 | 本轮阅读范围 |
| --- | --- | --- |
| [WeChatAutoReply](https://github.com/JunxiBao/WeChatAutoReply/tree/e3b0b96284a6cf863c8243249c7bcd7593956229) | e3b0b96，最新提交 2026-07-31；最近触及 Sources 的提交为 2026-07-30 | WeChatBridge、AutoReplyEngine、DeepSeekClient、SettingsView、build.sh |
| [wechat-mac-rpa](https://github.com/wq19901103wq/wechat-mac-rpa/tree/7624ad8fdedf01c2b0a7d843e4b5d37c00899a9e) | 7624ad8，最新提交 2026-10-16 为图表；最近触及 src 的提交为 2026-08-13 | WindowCapture、SystemAutomation、MessageSender、ChatListClicker、WeChatBot、感知管道文件、requirements、LICENSE |
| [TraceMemo 自动回复 fork](https://github.com/dustPyrotechnic/wechat-auto-reply-fork/tree/cdf5c43ce33cbc9a4d51e3a477193373d942a3e5) | cdf5c43，最新提交 2026-08-27 | poller、sender、安装/启动/构建脚本与 Mac 文档；详见独立审计 |

提交日期来源：[Junxi Sources 提交](https://api.github.com/repos/JunxiBao/WeChatAutoReply/commits?path=Sources&per_page=1)、[RPA src 提交](https://api.github.com/repos/wq19901103wq/wechat-mac-rpa/commits?path=src&per_page=1)。触及源码目录不等于完成收发兼容修复。

## WeChatAutoReply

**源码事实**：

1. `WeChatBridge.getWeChatApp()` 和发送路径均从 `NSWorkspace.runningApplications` 中按 `localizedName` 包含 `WeChat` 取第一个应用，未提供目标 Bundle ID/PID 配置。本机目标显示名为“微信 2”，运行时名称解析结果未验证。
2. `parseMessageList()` 在描述无法判断方向时默认入站；另一降级分支将右侧文本视为入站。本人手动回复可能进入此路径，源码注释也明确指出该问题。
3. `findInputField()` 选择面积最大的非搜索 AXTextArea，没有唯一、空白输入框门槛。`sendMessageHumanLike()` 会全选删除原输入内容。
4. `AutoReplyEngine.processMessage()` 生成后等待，再调用发送；已读路径未在等待结束后重新核对原会话身份。
5. 发送函数按回车后直接返回 `true`，未核对新增本人出站气泡。消息基线存在于进程内，未见该路径使用持久发送账本或 unknown 状态。
6. DeepSeek 请求设置 30 秒 timeout，API Key 保存在 UserDefaults；不能将此存储描述为 Keychain。未调用真实模型。
7. README 写 macOS 14+，实际 `build.sh` 编译目标为 `arm64-apple-macos15.0`。本机系统高于该目标，但这不能替代构建、安装及运行验证。

来源：[桥接与发送](https://github.com/JunxiBao/WeChatAutoReply/blob/e3b0b96284a6cf863c8243249c7bcd7593956229/Sources/WeChatBridge.swift)、[生成与等待](https://github.com/JunxiBao/WeChatAutoReply/blob/e3b0b96284a6cf863c8243249c7bcd7593956229/Sources/AutoReplyEngine.swift)、[模型客户端](https://github.com/JunxiBao/WeChatAutoReply/blob/e3b0b96284a6cf863c8243249c7bcd7593956229/Sources/DeepSeekClient.swift)、[构建脚本](https://github.com/JunxiBao/WeChatAutoReply/blob/e3b0b96284a6cf863c8243249c7bcd7593956229/build.sh)。

## wechat-mac-rpa

**源码事实**：

1. `WindowCapture` 的 owner 名称候选写定为 `WeChat`、`微信`；从满足尺寸的窗口选面积最大者，未按目标 Bundle ID/PID 绑定。
2. `SystemAutomation` 的调用方传入 `WeChat`；激活和窗口查询使用应用/进程名，`send_keys()` 也硬编码 `process "WeChat"`。名称匹配后的窗口与选定微信 2 的一致性未验证。
3. `get_frontmost_app()` 会先将指定进程置前，再检查前台名称；该方法包含 UI 操作，不能作为纯只读探针运行。
4. `WeChatMessageSender.send()` 用锁串行化剪贴板、聚焦、粘贴和输入回读；最终回车脚本返回成功后，发送器即返回 `ActionResult(success=True)`。
5. `WeChatBot` 随即记录发送成功并将待回复消息标记为已处理。它会另存 `pending_self_messages` 等待后续感知，但发送成功记录和已处理标记没有等待新增气泡确认。
6. 首条回复返回失败时，bot 保留未处理并在下轮重试；已读链路没有把“已提交但无确认”独立持久化为 unknown。
7. 静默模式由 `WECHAT_SILENT_MODE=1` 开启；`SILENT_WHITELIST` 中会话即使在静默模式仍真实发送。不能只凭“静默模式”名称认定绝无发送。
8. 默认 OCR/视觉感知不要求获取数据库密钥；WeFlow 历史导入为可选路径。历史状态文档记有微信 4.1.8，未取得本机 4.1.15 的实机结果。

来源：[窗口筛选](https://github.com/wq19901103wq/wechat-mac-rpa/blob/7624ad8fdedf01c2b0a7d843e4b5d37c00899a9e/src/capture/window_capture.py)、[系统操作](https://github.com/wq19901103wq/wechat-mac-rpa/blob/7624ad8fdedf01c2b0a7d843e4b5d37c00899a9e/src/action/system_automation.py)、[发送器](https://github.com/wq19901103wq/wechat-mac-rpa/blob/7624ad8fdedf01c2b0a7d843e4b5d37c00899a9e/src/action/message_sender.py)、[主流程](https://github.com/wq19901103wq/wechat-mac-rpa/blob/7624ad8fdedf01c2b0a7d843e4b5d37c00899a9e/src/bot/wechat_bot.py)。

## TraceMemo fork

详细事实与来源见 [TraceMemo Mac 源码核查](2026-10-21-tracememo-mac-source-audit.md)。

- 读取依赖本机 TraceMemo API；GUI 发送以显示名确认，未按 `com.tencent.xinWeChat2` 绑定。
- TraceMemo 文档适配 4.1.13 系列；`resolveXkeyHelperMode()` 只为 4.1.13 选择专用 helper，4.1.15 走 legacy。
- `autoGetDbKey()` 检测到 SIP 开启即返回 `SIP_ENABLED`；arm64 自动取钥请求管理员运行 helper。已有密钥的手动输入和后续只读数据库路径应单独区分。
- 发送状态无法确认时仍可能计为已发送；会清空原草稿；已有重试项可先于启动新消息过滤执行。
- 真实发送安装脚本会立即 bootstrap 后台服务；runner 包含 `--send --send-all`，由配置白名单限制。
- 本次没有运行 helper、修改 SIP、请求管理员凭据或修改微信签名。

## 新补充的 4.1.15 工具证据

[acmerfight 的 macOS 微信本地工具 Gist](https://gist.github.com/acmerfight/0a01249ef72970d07a2603dbb629e80f) 记录 Apple Silicon、macOS 15.6.1、官方微信 4.1.15 的数据库读取及 UI 发送核验。它是工具组件，不是现成 AI 自动回复应用。作者明确将纯 OCR 增量事件标为 `safe_to_autoreply: false`；首次取钥流程涉及临时副本重签与调试。以上为作者记录，本轮未复现，也未确认适用于改名、改 Bundle ID 的微信 2 或 macOS 26.6.2。Gist API 读取返回 IncompleteRead，本轮仅核对可访问的公开说明，没有完成源码审计。

## 后续验收条件（本次未执行）

| 项目 | 通过条件 |
| --- | --- |
| 应用绑定 | 从目标 Bundle ID 解析实际 PID，截图、AX、点击、键入均绑定同一应用；不靠显示名或最大窗口选取 |
| 收件 | 测试会话内核对正文、作者、时间和方向；证明只处理启动后新增消息 |
| 单会话授权 | 接入白名单的稳定身份；确认 GUI 标题、侧栏选择和对应账号一致 |
| 人工接管 | 发现原草稿、本人新回复、会话/窗口切换或用户暂停后取消本次拟发送 |
| 本机提交确认 | 一次提交后读取新增本人出站气泡且全文匹配；不能以输入框清空或回车脚本成功代替 |
| 不确定状态 | 提交后无回执、超时或取消保留 unknown 与去重记录，不自动重发 |

## 本次检查结果

环境和源码核查已完成，UI 可读性因工具访问未授权而未完成。没有安装候选项目、运行其构建/启动脚本、调用真实模型或发送消息。

pytest：未执行；ruff：未执行；smoke.py：未执行。此次只新增、补充调研文档，不声明候选项目测试通过。
