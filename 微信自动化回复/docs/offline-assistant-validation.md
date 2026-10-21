# 离线助手集成验收

日期：2026-10-22。验证目录：仓库的 `微信自动化回复/`。全部测试使用合成消息或示例附件，真实微信发送次数为 0。

## 自动化回归

| 检查 | 实际结果 |
| --- | --- |
| `.venv/bin/python -m pytest --collect-only -q` | 收集 914 |
| `.venv/bin/python -m pytest -q` | 914 通过，5 个 subtests 通过，1 个既有 Pydantic 弃用警告 |
| `.venv/bin/ruff check .` | All checks passed |
| `.venv/bin/python smoke.py` | 原有全部冒烟和新增 assistant smoke 通过 |
| `git diff --check` | 通过 |

回归覆盖白名单、重复 ID、会话隔离、人工接管、摘要和显式记忆、附件边界、无效模型输出、一次重试、异常状态、重启去重、文件监听以及原 AgentLoop 接线。LLM 单测均使用 mock/fixture，不依赖真实模型服务。

## 本机模型与附件

实际使用 `qcwind/qwen3-8b-instruct-Q4-K-M:latest`，调用地址固定为本机 Ollama；本地 Whisper 权重完成语音转写，macOS Vision 完成图片及视频帧 OCR。Python 解析器使用 pypdf 6.10.0 和 openpyxl 3.1.5。

执行命令：

```bash
.venv/bin/python agent_loop.py --offline-assistant replay --config assistant.local.json --model local --db logs/assistant-integrated-text.sqlite3 --report logs/assistant-integrated-text.json --input examples/assistant/messages.jsonl
.venv/bin/python agent_loop.py --offline-assistant replay --config assistant.local.json --model local --db logs/assistant-integrated-media.sqlite3 --report logs/assistant-integrated-media.json --input examples/assistant/multimodal.jsonl
.venv/bin/python agent_loop.py --offline-assistant replay --config assistant.local.json --model fixture --db logs/assistant-integrated-text.sqlite3 --report logs/assistant-integrated-replay.json
```

| 验收项 | 实际结果 |
| --- | --- |
| 文本与记忆 | 7 条唯一消息：5 条模拟回复、2 条过滤；重复输入未新增回复 |
| 会话隔离 | Alice 的简短中文偏好已保存，Bob 查询偏好返回未记录 |
| TXT 与追问 | 会议时间、地点、性能报告负责人匹配示例原文，并附行号 |
| DOCX | 时间与负责人匹配段落原文 |
| 语音 | 实际转写“你好,请问青竹项目的评审几点开始。”，结合当前会话 DOCX 作答 |
| 图片 | 识别青竹评审、下午三点、小林准备报告，附 OCR 来源 |
| XLSX | 设备金额回答 100，引用预算 A2:B2 |
| 视频 | 读取 0.0、3.0、4.8 秒帧与音轨，回复说明不是完整视频分析 |
| PDF | `Media.extract` 实际提取英文文本层及页 1 来源；本轮未单独调用 LLM 问答 |
| 多媒体重放 | 5 条全部 simulated，errors 为空 |
| 重启重放 | 原文本数据库保持 5 条发件记录和 5 条模拟回执，没有重复发送 |

视频模型回复包含“最多包含6个片段”的额外描述，实际提供的是 4 个来源片段；这不是实测覆盖数量。结构和引用校验不能保证模型每句话准确，摘要仍需核对原文。示例验证不构成通用 OCR、语音识别或问答准确率评测。

## 边界核销

- 已完成：本地事件监听、可配置白名单、关键词过滤、LLM 回复、短期上下文、显式长期记忆、附件解析、模拟发件、持久去重与恢复命令。
- 已完成：接入原 `agent_loop.py` 和 `AgentLoop`，mock 验证历史基线、原始分钟时间、旁路记忆与零真实发送。
- 未实机验证：新增 `--wechat-native --assistant` 的真实微信收件；增强处理拒绝 live。
- 未实现：微信附件自动下载、真实微信多联系人监听、扫描 PDF OCR、通用图片或完整视频理解。
- 原有实机入口与记录继续保留；本轮未启动微信、未操作联系人、未发送消息。
- 本机模型、转写环境、OCR 二进制、运行数据库及个人配置不提交。仓库只包含可重建脚本和合成示例。
