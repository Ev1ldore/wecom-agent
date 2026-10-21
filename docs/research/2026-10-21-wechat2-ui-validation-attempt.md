# 微信 2 无客户端改动的界面操作验证记录

日期：2026-10-21。目标：用户已选的 `/Applications/微信 2.app`，微信 4.1.15，Bundle ID `com.tencent.xinWeChat2`。

## 本轮实机结果

调用 `cua.getApp("/Applications/微信 2.app")` 返回：

```text
Computer Use was not approved to use 微信 2
```

未取得 AX 树或窗口图像，未执行联系人点击、会话切换、输入框写入或消息提交。该错误仅证明工具访问审批未通过，不能据此认定微信不暴露 AX 或无法截图。

## 权限入口核查

[OpenAI 官方 Computer Use 文档](https://learn.chatgpt.com/docs/computer-use) 区分应用访问审批与 macOS 屏幕录制、辅助功能权限。应用访问入口为 Settings > Computer use。文档明确说明 Computer Use 不能自动化 ChatGPT 自身，不能代为批准安全及隐私权限提示。

用户已要求代理设置；本轮未代改权限存储文件或通过其他技术绕过应用访问审批。需要用户在应用访问请求或设置中实际允许微信 2，之后才能继续原验证。

## 已读现有适配代码

- `微信自动化回复/adapters/wechat_ocr.py`：目标路径与 Bundle ID 已写定为微信 2；窗口图像交给本地 OCR 解析。这是代码能力，不是本轮实机读取结果。
- `微信自动化回复/adapters/wechat_ocr_send.py`：`WeChatOCRReplyChannel.select_contact()` 支持唯一侧栏 Ri 行，找不到时搜索并重新核对；受 live 开关和 Ri 范围限制。它不是联系人通用管理接口，本轮没有调用。
- 同文件 `prepare_reply()` 执行当前会话只读预检；`send_text()` 另含粘贴和提交。本轮没有调用两者，不以启动 live 通道代替导航验证。
- README 记录此前窗口采集为空白、真实接入尚未通过；此记录没有在本轮被复现或推翻。

## 本轮实际运行的离线检查

工作目录：`/Users/leecy/Desktop/wecom-ai-assistant/微信自动化回复`。

| 命令 | 实际结果 |
| --- | --- |
| `.venv/bin/python -m pytest -q` | 收集 779 / 通过 779，8.15 秒，1 条 Pydantic class-based Config 弃用警告 |
| `.venv/bin/ruff check .` | 通过，All checks passed |
| `.venv/bin/python smoke.py` | 退出码 0；desktop、offline、personal、realtime、wx2 冒烟通过，真实发送 0 |

这些检查使用合成窗口/OCR、模拟原生操作与 mock LLM，不能证明本机联系人操作或实际截图成功。

## 尚待实机验证

1. 获取目标微信 2 的 AX 状态；如果正文不可见，再检查获准工具的截图能力。
2. 从当前真实界面确认联系人入口、候选行、聊天标题和输入区域。
3. 在用户指定联系人范围内打开会话并核对标题；名称不唯一时停止。
4. 仅在空白输入框进行明确授权的可撤销草稿验证，读回后清理；本次不提交消息。
5. 记录应用版本、目标窗口、操作前后证据与限制；没有本轮证据的项目保持未验证。

本轮没有修改客户端、签名、SIP、系统授权或业务代码。只新增本记录；联系人和对话框的实机验证尚未完成。
