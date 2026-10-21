# 个人微信自动回复项目调研

核查日期：2026-10-21。范围：个人微信桌面客户端，优先 macOS；本次只读公开仓库、固定版本源码和 GitHub API，未安装项目、未运行第三方程序、未读取私人聊天、未发送消息。

证据标记：**仓库声明**表示项目文档；**源码核查**表示已读取指定提交的代码；**实机未验证**表示未在本机完成收件和发送闭环。提交日期取主分支 commit 的 committer 日期，不使用搜索抓取日期或仓库 pushed_at 代替。

## Mac 项目核查

| 项目 | 最近提交及发布 | 接入与平台 | 已知边界 |
|---|---|---|---|
| [JunxiBao/WeChatAutoReply](https://github.com/JunxiBao/WeChatAutoReply) | 最新提交 2026-07-31；[1.3.2](https://github.com/JunxiBao/WeChatAutoReply/releases/tag/1.3.2) 同日发布，API 确认包含 `WeChatAutoReply.dmg` | 仓库声明 macOS 14+、Apple Silicon；Swift 菜单栏应用；AX 读取手动打开的当前聊天，DeepSeek 生成回复 | 未给出精确微信版本兼容矩阵；本机实机未验证 |
| [taotao-river/wechat-auto-reply](https://github.com/taotao-river/wechat-auto-reply) | 最新提交 2026-08-21；[latest Release](https://github.com/taotao-river/wechat-auto-reply/releases/tag/latest) 正文标注 APK 构建于 2026-08-21 15:08（北京时间），Mac 需源码 | 仓库声明 macOS 12+、Python 3.9+；AppleScript/System Events 辅助功能收发；另有 Android 端 | README 明确说明 Mac 控件路径、Android 通知收发、真实模型凭据均未实机验证；不能据此认定开箱可用 |
| [wq19901103wq/wechat-mac-rpa](https://github.com/wq19901103wq/wechat-mac-rpa) | 最新提交 2026-10-16 为自动更新 star chart；最近触及 `src/` 的提交为 2026-08-13；[无 Release](https://github.com/wq19901103wq/wechat-mac-rpa/releases) | 仓库声明 macOS 12+、Python 3.10+；截图、OCR/视觉模型感知，AppleScript 发送；可选 WeFlow 历史导入 | 需屏幕录制、辅助功能、自动化权限及可见微信窗口；历史状态文档写微信 4.1.8；本机实机未验证 |

以上平台要求分别来自各仓库 README。维护日期还经以下官方 API 核查：[Junxi 提交](https://api.github.com/repos/JunxiBao/WeChatAutoReply/commits?per_page=1)、[Junxi Release](https://api.github.com/repos/JunxiBao/WeChatAutoReply/releases?per_page=1)、[taotao 提交](https://api.github.com/repos/taotao-river/wechat-auto-reply/commits?per_page=1)、[RPA 提交](https://api.github.com/repos/wq19901103wq/wechat-mac-rpa/commits?per_page=1)、[RPA src 提交](https://api.github.com/repos/wq19901103wq/wechat-mac-rpa/commits?path=src&per_page=1)。

## 固定源码事实

### JunxiBao/WeChatAutoReply

固定提交：[`e3b0b96284a6cf863c8243249c7bcd7593956229`](https://github.com/JunxiBao/WeChatAutoReply/commit/e3b0b96284a6cf863c8243249c7bcd7593956229)。

- **源码核查**：`WeChatBridge.getCurrentChatMessages()` 搜索消息列表并解析可见文字；存在将右侧静态文字视为入站消息的降级分支。`getWeChatApp()` 按运行应用名称包含 `WeChat` 选第一个进程，未按指定账号或 bundle ID 选择。
- **源码核查**：`sendMessageHumanLike()` 激活该微信进程，寻找输入框，执行全选删除、逐字符键入与 Return；发送函数返回 `true` 前未核验新增本人出站气泡。输入框原有草稿会被清空。
- **源码核查**：`AutoReplyEngine.start()` 将当前可见消息设为基线；切换聊天会清空记忆并重建基线；消息无重叠时降级取最后一条。上述行为不等于持久去重或严格尾部追加保护。

来源：[WeChatBridge.swift](https://github.com/JunxiBao/WeChatAutoReply/blob/e3b0b96284a6cf863c8243249c7bcd7593956229/Sources/WeChatBridge.swift)、[AutoReplyEngine.swift](https://github.com/JunxiBao/WeChatAutoReply/blob/e3b0b96284a6cf863c8243249c7bcd7593956229/Sources/AutoReplyEngine.swift)。许可证：MIT（仓库声明）。

### taotao-river/wechat-auto-reply

固定提交：[`15a400d67b769513df8a5b4ac8bcafc75f6878d8`](https://github.com/taotao-river/wechat-auto-reply/commit/15a400d67b769513df8a5b4ac8bcafc75f6878d8)。

- **源码核查**：`_LIST_UNREAD` 依赖固定 AX 表格路径及“未读/unread”描述；`_OPEN_AND_READ` 选中显示名对应行并读取最后一行文本。
- **源码核查**：`_SEND` 发前重新按显示名选中会话，随后键入回复、按 Return 并返回 `OK`；未核验出站气泡。输入框定位异常被忽略，之后仍继续 `keystroke`。
- **仓库声明**：Mac 的 AX 控件路径尚未真机验证；Release 仅 Android APK；Mac 安装脚本会创建虚拟环境、配置访问令牌并注册 launchd。未确认该仓库许可证，本次不将“公开源码”视为已获任何再分发授权。

来源：[wechat_mac_bot.py](https://github.com/taotao-river/wechat-auto-reply/blob/15a400d67b769513df8a5b4ac8bcafc75f6878d8/macos/wechat_mac_bot.py)、[README 已知限制](https://github.com/taotao-river/wechat-auto-reply#已知限制)、[Release 产物与构建日期](https://github.com/taotao-river/wechat-auto-reply/releases/tag/latest)。

### wq19901103wq/wechat-mac-rpa

固定提交：[`7624ad8fdedf01c2b0a7d843e4b5d37c00899a9e`](https://github.com/wq19901103wq/wechat-mac-rpa/commit/7624ad8fdedf01c2b0a7d843e4b5d37c00899a9e)。

- **仓库声明**：默认截图/OCR 路径不读数据库；可选 WeFlow 模式用于导入历史记忆。
- **文档核查**：`PROJECT_STATUS.md` 明确主体为 2026-05 历史记录，其中列微信 4.1.8 运行、OCR 识别与消息发送正常；它不能证明本机较新微信版本兼容。
- **源码核查**：已读取 `src/capture/window_capture.py` 与 `src/action/message_sender.py`，存在窗口截图与 AppleScript 发送实现；本次未运行收发闭环。
- **未验证**：Apple Silicon/Intel 分别的版本矩阵、微信 4.1.13/4.1.15 兼容、最小化/锁屏可用性、多人并发漏读率、误发率。许可证：MIT（仓库声明）。

来源：[README](https://github.com/wq19901103wq/wechat-mac-rpa/blob/7624ad8fdedf01c2b0a7d843e4b5d37c00899a9e/README.md)、[历史状态文档](https://github.com/wq19901103wq/wechat-mac-rpa/blob/7624ad8fdedf01c2b0a7d843e4b5d37c00899a9e/docs/03-guides/PROJECT_STATUS.md)、[窗口截图](https://github.com/wq19901103wq/wechat-mac-rpa/blob/7624ad8fdedf01c2b0a7d843e4b5d37c00899a9e/src/capture/window_capture.py)、[发送实现](https://github.com/wq19901103wq/wechat-mac-rpa/blob/7624ad8fdedf01c2b0a7d843e4b5d37c00899a9e/src/action/message_sender.py)。

## 核查范围与未完成项

已核查三个 Mac 项目的仓库说明、实际提交日期、发布产物、关键收发源码和版本边界。Junxi API 返回 Issues 空列表；taotao 返回一条关闭的 PR，不能作为用户实机兼容证明。RPA 前十条 Issues API 数据均为 star chart PR；扩大查询时响应截断，未完成全面兼容问题筛查。

本次没有安装、运行或修改第三方依赖，没有进行本机微信收发验收；pytest、ruff、smoke.py 均未执行，纯调研文档不代表项目测试通过。未验证各项目的账号风控概率，也没有“不封号”保证的证据。

## 其他三个主要候选

| 项目 | 已核实维护信息 | 项目方声明与验证边界 |
| --- | --- | --- |
| [dustPyrotechnic/wechat-auto-reply-fork](https://github.com/dustPyrotechnic/wechat-auto-reply-fork) | [2026-08-27，cdf5c43](https://github.com/dustPyrotechnic/wechat-auto-reply-fork/commit/cdf5c43ce33cbc9a4d51e3a477193373d942a3e5) 合并 Mac 服务生命周期修复 | Mac 控制 App 要求 macOS 13+、Python 3.9+、Swift/Xcode 工具和 TraceMemo；需源码构建。已补充[详细源码审计](2026-10-21-tracememo-mac-source-audit.md)，未实机收发 |
| [zqaini002/weix](https://github.com/zqaini002/weix) | [2026-10-16，d0d12c8](https://github.com/zqaini002/weix/commit/d0d12c8ffd77e97e70dc38bee0937757c133363b) 修复压缩消息正文解码 | 当前 README 记录 Windows 微信 4.1.15.13 本机数据库监听、视觉定位、发送回读及两会话六条交替测试；原始报告未公开，本轮未复现。Mac 明确待实机验收。当前自有部分限定非商业用途，不保证账号不受限制 |
| [FreeWisdom/wxauto-4.0](https://github.com/FreeWisdom/wxauto-4.0) | [2026-06-18，bd7c523](https://github.com/FreeWisdom/wxauto-4.0/commit/bd7c5233e79c0a185638a325bf6d30607244dfa8) 搜索、引用回复和 Bug 修复 | README 限定 Windows 微信 4.0.5；提供 Python 收发/监听接口，需要自行编写自动回复业务；窗口最小化/关闭时监听有停止响应限制，本轮未运行 |

说明来源：[fork README](https://github.com/dustPyrotechnic/wechat-auto-reply-fork/blob/cdf5c43ce33cbc9a4d51e3a477193373d942a3e5/README.md)、[Weix README](https://github.com/zqaini002/weix/blob/d0d12c8ffd77e97e70dc38bee0937757c133363b/README.md)、[FreeWisdom README](https://github.com/FreeWisdom/wxauto-4.0/blob/bd7c5233e79c0a185638a325bf6d30607244dfa8/README.md)。

## 旧项目与渠道边界

- [cluic/wxauto4](https://github.com/cluic/wxauto4/commit/d0ae14d3e435807b4dead393af9d014529bbac78) 最新提交日期为 2025-10-27，标题为停止更新；API archived 为 false，停止维护与归档应分开记录。
- [lich0821/WeChatFerry](https://github.com/lich0821/WeChatFerry) API archived 为 true；[默认分支最新提交](https://github.com/lich0821/WeChatFerry/commit/0f5c60a034fcac234cabd000b49c9200defa7f7d) 为 2026-03-21。pushed_at 为 7 月 10 日，不能当作源码更新日。其各分支当前兼容情况未逐个验证。
- [byteD-x/wechat-bot 当前 README](https://github.com/byteD-x/wechat-bot/blob/f83ed3b909121a7ff609c71569ffc99d7a196e6c/README.md) 默认后端声明为 wcferry，只支持 Windows 微信 3.9.12.51，不支持微信 4.x 或 Mac 直接自动化；不能只按简介中的 wxauto 推断新版兼容。
- [Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin) 最新提交为 [2026-10-13](https://github.com/Tencent/openclaw-weixin/commit/874637a0dbc24050f72d1385add4fc6170a2d726)，README 描述扫码授权、长轮询、多账号和多媒体收发。本次没有取得它能接管已有好友会话并以本人身份回复的证据，未登录验证。Bot 通道与原会话自动回复不能混为同一验收目标。

## 本机 Mac 追加核查

用户随后选定“微信 2”4.1.15。具体环境、三个候选的进程选择、消息方向、输入保护、发送回执与权限缺项，见[微信 2 兼容核查](2026-10-21-mac-wechat2-compatibility-audit.md)。该记录不代表任何候选已完成实机收发。
