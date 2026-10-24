# 会话短期记忆

## 当前范围

短期上下文由 `src/core/assistant/store.py:Store.context` 读取，供离线 `Engine.respond` 和桌面 `--wechat-native --assistant` dry-run 增强路径使用。普通 `--wechat-native --live` 仍未接入增强链，不宣称真实微信回复已具备本页记忆能力。

管理网页与离线 CLI 共用用户选择的配置、数据库。桌面增强入口使用自己的 `.assistant.db`，不会自动复用网页数据库。

## 配置

`assistant.example.json` 已提供以下参数，可复制到本地配置文件：

```json
{
  "history_window": 12,
  "short_term_ttl_seconds": 1800,
  "short_term_max_chars": 12000
}
```

- `history_window`：1–100，默认最近 12 条历史记录，包含用户与助手消息，不等同于 12 轮对话。
- `short_term_ttl_seconds`：60–86400，默认 1800 秒。按每条记录的本地入库时间滚动过滤；这是上下文生命周期时间，不替代微信原始显示时间。
- `short_term_max_chars`：1000–64000，默认 12000，限制 recent 与 older_excerpts 中的正文和摘录字符总和，不等同于模型 token 数，也不包括长期记忆和附件。

过期边界采用 `created > 当前时间 - TTL`；过期历史保留在审计表，但不进入短期 recent 或 older_excerpts。查询按会话读取最多 `history_window + 20` 条新近记录，避免加载全部历史。

优先保留最新消息，返回顺序为从旧到新。recent 每条最多 1000 字符，并提供 `truncated` 标记；更早有效历史最多 20 条，每条摘录最多 240 字符。总字符预算不足时截断或不纳入更早记录。摘录保留 `source`，不是模型生成摘要。

## 隔离与重置

会话身份为序列化的 `(conversation, sender)`，不同联系人互不共享历史。读取与重置均检查当前白名单。重置在 `context_resets` 表记录当前会话的历史序号边界，后续上下文只读取边界之后的记录；该边界跨进程重启保留。

重置不会删除历史审计、显式长期记忆、附件或发送去重记录。重置增加会话 revision，使重置前已开始生成的草稿失效；它不恢复暂停会话，不补发历史消息，也不启动微信监听。

## 使用

在运行项目目录中执行，使用实际本地配置和对应身份替换示例值：

```bash
.venv/bin/python agent_loop.py --offline-assistant session-show --config assistant.local.json --db logs/assistant.sqlite3 --conversation demo-alice --sender alice
.venv/bin/python agent_loop.py --offline-assistant session-reset --config assistant.local.json --db logs/assistant.sqlite3 --conversation demo-alice --sender alice
```

操作前先停止使用相同数据库的离线监听；数据库被占用时拒绝操作。`session-show` 不调用模型，`session-reset` 不发送微信消息。

管理网页的“记忆”页显示所选联系人的近期上下文，并提供“重置短期记忆”按钮。页面只展示 recent；更早有效摘录可通过 CLI 查看。对应 API 为：

- `GET /api/contacts/{cid}/details` 的 `short_term` 字段。
- `POST /api/contacts/{cid}/session/reset`，需要当前页面 CSRF token，沿用 Origin、白名单和数据库锁检查。

## 验证边界

对应单测为 `tests/test_assistant_short_term.py` 与 `tests/test_assistant_web.py`，覆盖 TTL、字符预算、身份隔离、重启后重置边界、长期记忆与审计保留、生成中草稿失效、CLI、页面 API 权限与 CSRF。`smoke.py` 包含短期上下文重置检查，全部离线，不发送真实消息。

本次未在浏览器中完成新增页面控件的点击与视觉验收，未重新启动微信真实发送，记忆模块新增依赖为 0。后续 MCP 服务已完成官方 stdio 客户端联调，具体依赖与验证见 [MCP 规范](mcp-spec.md)。

本次离线执行结果：pytest 收集 997 / 通过 997；ruff 通过；smoke.py 通过；前端 JavaScript 语法检查通过。pytest 有两条既有依赖弃用告警。
