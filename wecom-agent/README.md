# wecom-agent

本地优先的企业微信个人单聊自动回复 MVP：从已验证的桌面会话读取新消息，经 LLM 生成回复，再由本人企微账号在原会话中发送；默认仅 dry-run，不会真实发送消息。

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![Status](https://img.shields.io/badge/Status-Local%20MVP-2563EB)

## 特性

- 仅处理已验证的 Ri 外部单聊，过滤自身消息、系统消息和不在白名单内的消息。
- 读取企微原始显示时间，按分钟精度保存，不伪造秒级时间或接收时间。
- 以 SQLite 持久化消息去重、LLM 草稿和发送结果，避免重启后重复处理。
- 默认 dry-run，只记录拟发送内容；`--live` 才启用受限的本人原会话发送。
- 发送前核对目标会话和输入框；只有本机新增出站气泡正文匹配时才标记为 `sent`。
- 对发送超时、取消或本机回执不明记录 `send_unknown`，不自动重发。
- 连续三次发送失败后暂停 60 秒；异步 HTTP 和 LLM 调用均带 timeout、一次重试与异常兜底。
- 保留 FastAPI 回调、Redis Stream 和企微自建应用主动发送的兼容模块，但它们不是本地个人账号路径的启动前提。

## 环境要求

- macOS，已登录企业微信桌面客户端，并已在目标 Ri 单聊中打开“查找聊天记录”。
- Python 3.11 或更高版本。
- 企业微信的辅助功能权限，用于受限读取和真实发送模式。
- 可访问的 OpenAI Chat Completions 兼容模型服务。

## 安装

```bash
git clone https://github.com/Ev1ldore/wecom-agent.git
cd wecom-agent
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

在 `.env` 中填写 `LLM_BASE_URL`、`LLM_API_KEY` 和 `LLM_MODEL`。不要将真实密钥提交到 Git。

## 快速开始

先在企业微信中打开 `Ri @微信` 单聊，打开“查找聊天记录”，清空搜索框并保持默认筛选。

```bash
source .venv/bin/activate
python agent_loop.py --personal
```

默认启动 dry-run：程序会读取、过滤、生成并持久化草稿，但不发送企微消息。持续运行期间按 `Ctrl-C` 停止。

用户明确授权真实发送后，再运行：

```bash
python -u agent_loop.py --personal --live
```

live 模式启动时会将当前可见历史记录为基线，只处理之后新增的消息；不会补发历史草稿。`sent` 仅表示本机出站气泡已确认，不代表对方已读或收到。

查看实时处理记录：

```bash
.venv/bin/python -u watch_logs.py
```

也可以在 Finder 中双击 `查看企微实时日志.command`。查看器只读 SQLite 和日志，不会启动、停止或发送消息。

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
| `WECOM_CORPID` 等回调变量 | 仅兼容 FastAPI 回调与自建应用路径需要 | 无 |

## 测试

```bash
source .venv/bin/activate
pytest -v
ruff check src tests adapters prompts agent_loop.py watch_logs.py smoke smoke.py
ruff format --check src tests adapters prompts agent_loop.py watch_logs.py smoke smoke.py
python smoke.py
```

`smoke.py` 默认只执行离线验证，不会向企微发送消息。

## 贡献

欢迎提交 Issue 或 Pull Request。请保持依赖最小化，为新模块补充 Protocol 与独立单测，并在提交前运行测试、Ruff 和 `smoke.py`。

## License

本仓库目前未声明许可证。
