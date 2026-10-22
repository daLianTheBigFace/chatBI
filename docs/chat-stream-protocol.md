# Chatbot SSE 协议 v1.0

## 接口与请求

`POST /api/v1/chat/stream`，请求体为 Pydantic `ChatRequest`：

```json
{
  "question": "统计艺术家总数",
  "history": [],
  "conversation_id": null,
  "user_message_id": "570ddcb0-f965-4126-b4f2-de7b88247ca7"
}
```

question 去掉首尾空格后不能为空，最大 8000 字符。
conversation_id 可省略，服务会生成 UUID；后续同一对话可继续提交这个值。
user_message_id 可由前端生成 UUID，用于关联已显示的用户消息；省略则由服务生成。
history 为可选历史问答数组，role 仅允许 user/assistant，content 为非空文本。
最多 20 条消息、每条 16000 字符、合计 48000 字符；不接受客户端 system 消息。
下一轮请求可以包含上一轮问答，question 填“那专辑呢？”。历史仅帮助理解追问，
不是已验证事实或可执行指令，仍需基于 schema 生成并执行查询。
未知字段或非法 UUID 在连接流之前返回 HTTP 422。
同一 user_message_id 的重复提交不会去重，每次请求都是新的 run 和助手消息。
现有 session_id 不用于此接口，避免两套会话标识混用。

HTTP 200 的 Content-Type 是 text/event-stream，禁用缓存与 Nginx 响应缓冲。
约 15 秒无业务事件时发送 SSE 注释 `: keep-alive`，心跳不占用业务 sequence。
POST 请使用 fetch 的 ReadableStream；浏览器原生 EventSource 不支持该 POST 请求体。

## 公共信封

每个业务事件都是 Pydantic `ChatEvent`，payload 使用 type 判别联合：

```text
id: <event_id>
event: <payload.type>
data: {"protocol_version":"1.0","event_id":"...","sequence":1,"conversation_id":"...","user_message_id":"...","message_id":"...","role":"assistant","run_id":"...","created_at":"...Z","payload":{"type":"run.started","tag":"lifecycle","user_message_id":"..."}}

```

| 字段 | 含义 |
| --- | --- |
| protocol_version | 当前固定 1.0；协议不兼容变化需升版 |
| event_id | 每个事件独立 UUID，等于 SSE id |
| sequence | 当前 run 内从 1 连续递增；前端可检查漏帧和去除重复事件 |
| conversation_id | 对话标识；同一请求所有事件一致 |
| user_message_id | 本次用户消息标识 |
| message_id | 本次助手消息标识，含过程卡片与正文 |
| role | 固定 assistant |
| run_id | 一次执行标识，内部等于 request_id；重试整个请求产生新值 |
| created_at | 事件产生时的 UTC 时间 |
| payload.type | 事件路由的唯一依据，不从文本猜测类型 |
| payload.tag | 推荐展示组件类别，具体组件名称由前端自行定义 |

`GET /api/v1/chat/events/schema` 返回完整 JSON Schema。
静态快照见 `docs/chat-stream-event.schema.json`，由模型生成。
请求 schema 同时包含在 FastAPI `/openapi.json` 的 ChatRequest 中。

## 事件与组件

| type | tag | 含义与主要字段 |
| --- | --- | --- |
| run.started | lifecycle | 开始请求，返回所有标识，可结束前端的连接等待态 |
| progress | progress | stage、text；公开的阶段说明，可渲染折叠的过程面板 |
| sql.started | sql | tool_call_id、attempt、query、phase=validation；创建 SQL 卡片，尚未执行 |
| sql.executing | sql | tool_call_id；校验已通过，更新卡片为执行中 |
| sql.completed | table | tool_call_id、result、redacted；将该次结果表挂到对应 SQL 卡片 |
| sql.failed | sql | tool_call_id、phase、code、text；该候选校验或执行失败 |
| answer.started | answer | block_id、format=plain_text；创建正文块 |
| answer.delta | answer | block_id、delta；按顺序追加正文片段 |
| answer.completed | answer | block_id、text、replace=true；用最终正文替换临时块内容 |
| warning | warning | text；假设、截断或汇总降级等提示 |
| run.error | error | code、text；本次整体失败的公开错误，不含私有异常细节 |
| run.completed | lifecycle | status、clarification_questions、metadata；唯一正常终止事件 |

progress.stage 包括 schema、understanding、selection、generation、validation、execution、
repair、synthesis。展示的“思考”是服务器定义的简短进度说明，不是模型内部推理文本。
SDK 的原始 reasoning、模型事件、Prompt 和私有异常不向前端转发。

sql.started.attempt 从 1 递增，每个修复候选使用新的 tool_call_id。
校验失败的候选会直接 sql.failed，不出现 sql.executing，也不计入 execution_count。
真正执行过的候选会依次出现 sql.executing 和 sql.completed/sql.failed。
修复过程中 sql.failed 不代表整体失败，应等待 run.completed.status。

result 包括 columns、rows、truncated、execution_time_ms。
rows 是行数组，按 columns 的位置对应，支持重复列名。
columns/rows 与已有执行预算一致，不是任意大小的数据库导出；一个结果作为单个 SSE JSON 发送。
不同 SQL 的结果独立展示，不以最终结果覆盖之前的卡片。

正常成功示例：

```text
run.started
progress × 若干
sql.started → progress(validation) → progress(execution) → sql.executing → sql.completed
progress(synthesis) → answer.started → answer.delta × 若干 → answer.completed
warning × 零或多个
run.completed(status=success)
```

澄清请求不执行 SQL，也没有 answer 块；在 run.completed 的 clarification_questions
生成问题卡片，然后用户重新提交完整问题。失败请求出现 run.error 后以 failed 结束。
汇总报错或超时保留成功的数据，最终正文替换为基础说明，warning 提示降级，status 仍为 success。

## 回答流式与隐私边界

汇总调用 Agents SDK Runner.run_streamed，消费 Responses 的 output_text.delta。
不是把完整答案预先拆字后假装模型流式输出。理解、SQL 生成和修复保持结构化输出，
只有正文汇总走纯文本流；前端不会收到半截 JSON。

默认展示策略为 ConservativeDisplayRedaction，每次请求独立创建：

- SQL 的字符串、数值、二进制字面量替换为占位符，去掉注释；参数只展示名称。
- SQL 解析失败时隐藏整段 SQL，不以原文回退。
- 结果所有文本单元格默认隐藏；常见姓名、联系方式、密钥及客户/员工 ID 字段的数值也隐藏。
- NULL 保留；常见计数、金额等非敏感数值保留；结果列名仍保留以便解释结构。
- 从参数、字面量和结果中观察到的私有值，以及常见邮箱、手机号、密钥模式，
  在答案、告警和澄清问题中再次遮盖。
- SSE 汇总模型使用脱敏后的结果；SQL 工作流仍使用原始值执行，展示脱敏不改变查询语义。

answer.delta 使用展示策略的 stream_prefix 增量脱敏，不再等待换行：
每次模型 chunk 到达后立即发送已确认可展示的前缀，只保留可能继续构成邮箱、
手机号、密钥或已知私有值的后缀。不能对每个 chunk 独立正则替换，否则跨片段敏感值会漏出。
由于邮箱规则支持 Unicode，尚未结束的连续中文词句也可能保留到标点后再发送；
英文通常按词边界释放，未结束的敏感 token 可能持续缓冲。不承诺逐 token 或逐字展示。
汇总完成后发送剩余后缀；失败降级时丢弃后缀，由最终 answer.completed 替换正文。
前端收到 delta 立即更新，未添加模拟打字延迟。事件调试展示正文分片数和后端首段正文耗时；
这是后端事件生成时间，不包含网络传输和浏览器绘制耗时，历史重放不重现逐片段输出。
输出最多 16000 字符；临时正文可能因失败被最终基础说明替换，前端必须执行 replace。

默认策略偏保守，会隐藏商品名、艺术家名等普通文本；数值字段若用了完全不含语义的别名，
启发式规则也无法证明其不敏感。业务需要精确字段规则时，通过 SQLAgentService 的
display_factory 注入 DisplayRedactionStrategy，不要依赖正则表达式提供完整隐私保证。
表名与字段名默认可见；若名称本身也敏感，应替换展示策略。
SQL 生成模型仍会接收原始问题和获准的 schema；这不是模型输入全流程匿名化。
当前 Chinook 的 open_sql_agent 组装时显式注入 PublicCatalogDisplayRedaction：
公开目录为 tracks、albums、artists、genres、media_types、playlists、playlist_track。
只有 SQL 所有表来源均在此名单内时才允许展示目录文本；一旦连接、子查询或 UNION
涉及 customers、employees 等非目录表，恢复保守脱敏，不能通过列别名绕过。
SQL 字面量和参数仍隐藏，已知私有值与常见邮箱/密钥模式也继续遮盖。
无法确认来源的 SQL（例如没有来源表或 CTE 别名）保守处理。
该策略是对当前本地 Chinook 示例库的公开数据分类配置，不应直接复用于含私有内容的其他库。
通用 SQLAgentService 的默认策略仍为 ConservativeDisplayRedaction。
原有 `/api/v1/query` 保留原始 Python 服务响应，未应用此次 SSE 展示脱敏，
面向用户的聊天前端应使用新接口，不能将旧接口作为敏感数据受控展示入口。

## 取消、结束与重连

前端停止按钮用 AbortController.abort() 中断 fetch。
断开连接时关闭事件生成器、取消本次工作流/模型调用，数据库沿用已有线程取消与清理机制。
不保证不可中断的 SQLite 锁等待在瞬间结束。
事件队列最多缓存 32 个业务事件；慢客户端形成背压，不在内存无限积累事件。
模型 SDK 内部缓冲不等于此队列限制。总请求期限也覆盖队列等待和汇总。

正常结束只有一个 run.completed；断网/主动取消可能收不到终止事件。
前端若遇到 EOF 且未看到 run.completed，应标记“连接中断”，不能当成成功。
当前没有事件持久化、Last-Event-ID 回放、自动恢复或请求幂等；不要盲目自动重新提交 POST。
conversation_id 关联当前对话；后端不按 ID 存储历史消息。
多轮上下文由客户端通过 history 传入，本次不提供持久会话查询接口。

## 前端消费示例

```javascript
const controller = new AbortController();
const response = await fetch('/api/v1/chat/stream', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
  body: JSON.stringify({ question, conversation_id: conversationId ?? null,
                         user_message_id: crypto.randomUUID() }),
  signal: controller.signal,
});
if (!response.ok) throw new Error(`HTTP ${response.status}`);
const reader = response.body.getReader();
const decoder = new TextDecoder();
let buffer = '', completed = false;
while (true) {
  const { value, done } = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, { stream: true });
  let end;
  while ((end = buffer.indexOf('\n\n')) >= 0) {
    const frame = buffer.slice(0, end);
    buffer = buffer.slice(end + 2);
    const data = frame.split('\n').filter(line => line.startsWith('data: '))
                      .map(line => line.slice(6)).join('\n');
    if (!data) continue; // 注释心跳
    const event = JSON.parse(data);
    conversationId = event.conversation_id;
    // 由 payload.type 更新对应的过程、SQL、表格、正文或告警组件。
    // answer.delta 追加；answer.completed.text 替换正文。
    onChatEvent(event);
    if (event.payload.type === 'run.completed') completed = true;
  }
}
if (!completed) throw new Error('连接中断，未收到终止事件');
// 停止按钮调用 controller.abort()。
```

示例消费本服务固定的 LF 帧格式，并处理 UTF-8 与网络分块，不假设一个网络 chunk 对应一个事件。
组件请使用文本渲染（例如 textContent）；不要把 SQL、单元格或回答直接作为 HTML 插入。
Streamlit 测试页在页面会话内保留最近 10 轮问答，并自动构造预算内的 history。
失败或未完成的请求不进入模型上下文，澄清问题保留供下一轮补充。
页面重跑时重绘已收到的结果，不重新执行历史 SQL；新对话按钮清空内存和 ID。
浏览器会话重建后不恢复历史，不写入数据库。启动方式见 README。
它用于验证后端展示能力，不是完整的聊天产品界面。

OpenAI SDK 参考：[官方 Agents SDK 文档](https://developers.openai.com/api/docs/guides/agents/sdk)。
