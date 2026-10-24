# MCP 接口规范

状态：已实现的本地 MCP 管理服务，接口版本 `0.1`。入口为 `src.core.assistant.mcp_server`，提供下表中的 7 个工具。已用官方 MCP 客户端完成 stdio 子进程联调；尚未在 Codex 或其他宿主中添加服务或进行宿主界面验收。

## 协议与传输

首版使用官方 Python SDK `mcp==1.30.0`，固定其受维护的 v1 系列，匹配本项目既定的 `2025-11-25` 兼容目标；不宣称采用最新主版本。初始化、能力协商、JSON-RPC、取消和 stdio 生命周期由 SDK 处理。该安装版本支持 `2024-11-05`、`2025-03-26`、`2025-06-18`、`2025-11-25`，本次官方客户端实际协商为 `2025-11-25`，其他版本未单独联调。

首版传输限定 stdio：由本地宿主拉起子进程，stdout 仅输出协议消息，structlog 日志输出 stderr。配置和数据库路径由启动参数提供，工具参数不得改写任意本地路径。无需公开端口，不复用联系人网页的 HTTP 路由作为 MCP 端点。服务不加载 `.env`，不调用模型，也不导入微信桌面适配器。

参考：[官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk)、[版本化工具规范](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)、[版本化传输规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)。

## 权限与身份

- 每次调用读取当前配置，按 `(conversation, sender)` 精确匹配白名单；显示名不能替代已核实身份，禁止通配符。
- 对外使用 `ContactBook` 产生的本地 `contact_id`；调用时再解析到身份对并检查是否仍被勾选，不接受客户端传入 SQL scope。
- 首版只开放联系人列表、短期上下文、显式长期记忆和附件元数据。没有微信收发、窗口操作、启动监听、执行命令、读取任意文件或改白名单工具。
- 联系人列表可以返回手动登记的显示名和勾选状态；未勾选联系人的正文、记忆和附件一律拒绝读取或修改。
- 所有会话操作与管理页沿用 `<database>.lock` 排他锁，同数据库离线监听运行时返回 `BUSY`。仅列联系人不访问数据库。每次调用还对配置的 `.lock` 文件持共享锁，正常配置写入期间返回 `BUSY`。绕过项目锁直接改文件不属于并发一致性保证范围。
- 当前真实发送仍只限微信 2 的 Ri 原会话。MCP 连接、页面勾选和短期记忆配置均不扩大这项授权。
- MCP 协议连接与业务会话不是同一概念。断开 MCP 不删除业务会话；每次工具调用必须带目标联系人 ID，不能沿用上一次工具调用的联系人。

## 工具契约

所有输入为 JSON object，拒绝未声明字段。`contact_id` 为 24 位小写十六进制；`key` 原始长度为 1–40 字符，`value` 原始长度为 1–300 字符，保存时去除首尾空白，纯空白值拒绝。`ttl_seconds` 可省略或为 null，表示长期保留；传整数时为 1–31536000 秒，不接受布尔值。

| 工具名 | 必填参数 | 可选参数 | 成功 data | 现有落点 |
| --- | --- | --- | --- | --- |
| `contacts_list` | 无 | 无 | `contacts: [{id, name, selected}]` | `ContactBook.state` |
| `session_context_get` | `contact_id` | 无 | `recent, older_excerpts` | `Management.short_term` |
| `session_context_reset` | `contact_id` | 无 | `reset: true` | `Management.reset_short_term` |
| `memory_list` | `contact_id` | 无 | `memory: [{key,value,source,expires}]` | `Management.memories` |
| `memory_set` | `contact_id, key, value` | `ttl_seconds` | `saved: true` | `Management.set_memory` |
| `memory_forget` | `contact_id, key` | 无 | `removed: 0或1` | `Management.forget_memory` |
| `documents_list` | `contact_id` | 无 | `documents: [{id,name,evidence}]` | `Management.documents` |

`contacts_list` 不返回原始配置、密钥、模型地址、数据库路径。`documents_list` 不返回文件绝对路径或全文。页面 API 的数据结构与本表不同，MCP 适配层显式转换；附件名称仅返回 basename。

读工具设置 `readOnlyHint: true`；写工具设置 `readOnlyHint: false`；全部设置 `openWorldHint: false`。重置短期上下文不删除审计行，设置与遗忘长期记忆会改变业务上下文，客户端应显示调用目标与结果。工具 annotations 仅作提示，服务端仍必须执行权限检查。

## 返回与失败

每次调用由服务生成 `msg_id`，日志只包含工具名、msg_id、耗时和固定错误码，不记录正文、记忆值或密钥。工具执行成功同时提供 `structuredContent` 和相同 JSON 的 `TextContent`；业务失败设置 `isError: true`。

成功 data 外层结构示例：

```json
{"msg_id":"request-001","ok":true,"data":{"recent":[],"older_excerpts":[]}}
```

失败外层结构示例：

```json
{"msg_id":"request-002","ok":false,"error":{"code":"NOT_ALLOWED","message":"该联系人未获授权"}}
```

| 错误码 | 条件 | 调用方处理 |
| --- | --- | --- |
| `INVALID_ARGUMENT` | 类型、长度或额外字段不合法 | 修改参数后重新调用 |
| `NOT_FOUND` | 联系人不存在 | 刷新列表 |
| `NOT_ALLOWED` | 联系人未勾选或已撤销授权 | 用户手动修改配置后重新调用 |
| `BUSY` | 同库已有离线监听或管理操作 | 停止占用实例后重新调用 |
| `STORE_UNAVAILABLE` | 本地数据库或配置不可用 | 检查目录及权限，不返回原始异常 |
| `TIMEOUT` | 工具超过 10 秒 | 写入结果未知时先读取核对，不盲目重放 |

协议格式或未知方法由官方 SDK 处理；未知工具、参数类型及额外字段错误返回工具失败 `INVALID_ARGUMENT`，不包含原始异常或参数。首版管理工具没有 HTTP、LLM 或真实消息发送调用，不存在外部调用重试。后续若引入外部只读调用，必须有 timeout、最多一次重试及固定错误兜底；发送、写入和结果未知操作禁止自动重试。

## 验收清单

已完成：官方客户端初始化与 tools/list/tools/call 联调、输入校验和响应 schema 校验、stdout 纯协议输出、每次调用撤权检查、跨联系人隔离、配置和数据库锁冲突、超时与取消后的工作进程清理、异常脱敏、重启后记忆读取。短期重置边界跨重启由 Store 单测验证。真实发送次数为 0。

对应单测为 `tests/test_assistant_mcp_tools.py` 与 `tests/test_assistant_mcp_server.py`。安装 MCP 可选依赖后，`smoke.py` 也执行官方 stdio 客户端冒烟；未安装时明确输出 MCP SKIP，不表示 MCP 已通过。

协议层工具输出声明通用 envelope schema，成功 data 的业务字段由本节表格和回归测试约束。当前未做 Codex 宿主内连接验收、跨版本协议联调或公网 MCP 部署。

## 实现与调用链

`mcp_server.create_server` 注册工具和 schema → `ProcessTools.call` 校验参数并拉起单次 worker → `mcp_tools.LocalTools.call` 获取配置共享锁、检查联系人及白名单、获取数据库排他锁 → `Management` → `Store`。工作进程不提供其他操作。

每次工具执行最多 10 秒，包含工作进程启动；超时或客户端取消时结束工作进程并回收，不自动重试。写入可能已在终止前提交，此时必须先用读取工具核对，不能假定回滚。重置或写入不会启动真实监听。SQLite 审计与去重规则保持现有项目边界。

## 安装与启动

在 `微信自动化回复/` 运行项目目录执行：

```bash
.venv/bin/python -m pip install -e '.[mcp]'
.venv/bin/wecom-agent-mcp --config assistant.local.json --db logs/assistant.sqlite3
```

也可以直接使用模块入口：

```bash
.venv/bin/python -m src.core.assistant.mcp_server --config assistant.local.json --db logs/assistant.sqlite3
```

先用联系人页面或 CLI 配置并保存实际联系人，使用与管理页相同的配置和数据库。配置文件不存在时不自动创建或替换，工具返回 `STORE_UNAVAILABLE`。需要新的空配置时，可保存以下 JSON 为 `assistant.local.json`；已有文件请保留：

```json
{"contacts":[],"whitelist":[]}
```

这是 stdio 服务，正常启动后等待宿主的 JSON-RPC 输入，终端不会显示网页地址或提示符。由 MCP 宿主拉起时使用绝对路径；宿主关闭 stdin 后退出，终端手动启动可按 Control+C。不要以无人提供 stdin 的普通后台 HTTP 服务方式运行。

## 客户端配置

通用 `mcpServers` 结构见 `examples/assistant/mcp-client.example.json`，将 `/absolute/path/wecom-ai-assistant/微信自动化回复` 替换为实际运行项目路径。不同宿主的配置文件位置和格式以对应宿主为准；本例不自动写入全局配置，不授予宿主新增权限。

```json
{
  "mcpServers": {
    "wechat-assistant-management": {
      "command": "/absolute/path/wecom-ai-assistant/微信自动化回复/.venv/bin/wecom-agent-mcp",
      "args": [
        "--config", "/absolute/path/wecom-ai-assistant/微信自动化回复/assistant.local.json",
        "--db", "/absolute/path/wecom-ai-assistant/微信自动化回复/logs/assistant.sqlite3"
      ]
    }
  }
}
```

调用顺序示例：`contacts_list` 获取已勾选 ID → `session_context_get` 或 `memory_list` 读取 → 按用户要求调用 `memory_set` 或 `session_context_reset` → 再读取核对。用户撤销勾选后，旧 ID 不能继续访问记忆。

## 依赖与本次验证

新增一个直接可选依赖 `mcp==1.30.0`，用于官方协议、stdio 服务端及客户端，不自研 JSON-RPC。SDK 自带的 jsonschema 依赖用于输出 envelope 校验。安装成功，无依赖替换。`pip check` 通过；`pip install --no-deps -e .` 安装入口成功；`wecom-agent-mcp --help` 执行成功。

SDK v1 维护说明见[官方 v1 README](https://github.com/modelcontextprotocol/python-sdk/blob/v1.x/README.md)。v2 是另一主版本，本项目此次没有进行 SDK 主版本迁移。

本次全量执行结果：pytest 收集 1017 / 通过 1017；ruff 通过；smoke.py 通过，包含官方 MCP stdio 联调，真实发送数为 0。pytest 存在两条既有依赖弃用告警。
