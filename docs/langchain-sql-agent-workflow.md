# LangChain SQL Agent：工作流与前端接入

## 实现范围

LangChain 后端使用 `create_agent` 创建六个独立的结构化 Agent：理解、SQL 生成、
SQL 修复、结果汇总、数据权限审核、结果原值披露审核。父级 LangGraph `StateGraph`
控制执行顺序、分支、修复预算与人工中断，不再调用旧 `SQLAgentWorkflow`。
表结构筛选、SQL 校验和只读 SQLite 执行仍是确定性的程序节点。
OpenAI Agents 后端保留，仍是默认工厂。

这是显式编排的多 Agent 工作流，不是让单个 Agent 任意选择 SQL 工具。
每个专家有自己的 system prompt、原生 user/assistant messages 和 `ToolStrategy` 输出契约。
历史消息由前端传入，专家消息不会混入其他专家的私有对话。

## 两种授权的含义

1. `database_access`：当前问题需要额外表或字段。只允许申请服务端
   `approval_access_config` 明确列出的资源；没有该配置就不能申请数据库扩权。
   该配置中的 schema 名称可发送给权限审核模型，记录值不会在审批前被读取。
   授权按表选择，覆盖弹窗列出的该表可委托字段；未列出的字段不能取得。
2. `result_disclosure`：查询已经合法执行，但 LLM 只看到了脱敏结果。
   若必须使用原值才能回答，暂停请求用户同意。该许可允许原值进入有预算限制的
   汇总模型输入，以及最终结果和回答。拒绝后继续用脱敏结果回答。

数据库访问许可不等于结果披露许可。LLM 只能建议申请，真正的扩权由服务器在收到
当前 interrupt 的同意后执行。申请理由采用固定的公开说明，避免模型复制结果原值
到审批弹窗；弹窗包含申请的资源名称与本次运行范围。

权限仅对本次 run 生效；新问题使用新的 run，不继承许可。只读、函数白名单、行数、
查询时间、结果字节、执行次数与修复次数限制在授权后仍然生效。

## 启用与配置

```python
from functools import partial

from chatbi.api.app import create_app
from chatbi.sql_agent.config import AccessConfig
from chatbi.sql_agent_langchain import open_sql_agent_langchain

factory = partial(
    open_sql_agent_langchain,
    access_config=AccessConfig(allowed_tables=frozenset({"artists", "albums", "tracks"})),
    approval_access_config=AccessConfig(
        allowed_tables=frozenset({"customers"}),
        allowed_columns={"customers": frozenset({"CustomerId", "FirstName", "LastName"})},
    ),
)
app = create_app(service_factory=factory)
```

持久检查点默认写入 `.chatbi/langchain-checkpoints.sqlite`，可通过 `checkpoint_path`
修改。配置 `CHATBI_LANGCHAIN_CHECKPOINT_KEY` 为 Fernet 密钥，或通过工厂参数
`checkpoint_encryption_key` 注入。密钥必须在重启前后保持不变且单独保管。

生成密钥：`uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`。
工厂从进程环境读取此变量；若放进 `.env`，启动模块需显式加载它，或将值传给工厂参数。
未配置时使用进程内临时密钥，服务可工作，但重启后旧 run 无法恢复。

问题、历史、解析意图、SQL、原始结果和最终响应以加密字段进入检查点；schema、
脱敏投影、审批范围及控制元数据可见。不要开启会向外部发送私有输入的 tracing。
检查点保留本地历史，不向前端开放读取任意 checkpoint 的接口；24 小时后拒绝客户端
恢复，过期不是物理删除。实际清理与备份保留策略由部署方管理。

## 前端 API

| 方法 | 路径 | 请求与用途 |
| --- | --- | --- |
| POST | `/api/v1/langchain/runs` | `SQLAgentRequest`，运行到完成或首次审批暂停 |
| GET | `/api/v1/langchain/runs/{run_id}` | 查询公开状态与待审批范围 |
| POST | `/api/v1/langchain/runs/{run_id}/resume` | `{interrupt_id, approved}`，同意或拒绝并继续 |
| POST | `/api/v1/langchain/runs/{run_id}/cancel` | 取消运行，终态不能恢复 |

创建返回的 `run_token` 只发一次；后续请求在 `X-Run-Token` 头中携带。
它是当前本地单用户应用的 bearer capability，应当像凭证一样保管，不放在 URL、
日志或公开状态里。错误 token 与不存在 run 均为 404；过期 410；过时审批或终态恢复
409；缺失请求字段 422。默认 OpenAI 后端调用这些 LangChain 专用接口返回 409。

创建与恢复接口会等待执行到下一次暂停或终态，再返回 JSON，不是后台任务队列。
`run_token` 返回前，可通过取消原始 fetch 断开连接；获取令牌后可显式取消。
SQLite 检查点配合进程内运行锁，当前部署限定一个应用 worker；多进程部署应更换
共享持久化与跨进程锁，并绑定真正登录用户的身份，不能只依赖客户端传来的 user ID。

```javascript
const root = '/api/v1/langchain/runs';
const created = await fetch(root, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ question: '列出客户姓名', history: [] }),
});
if (!created.ok) throw new Error(`HTTP ${created.status}`);
let run = await created.json();
const token = run.run_token;

async function decide(approved) {
  const response = await fetch(`${root}/${run.run_id}/resume`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Run-Token': token },
    body: JSON.stringify({ interrupt_id: run.approval.interrupt_id, approved }),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  run = await response.json();
  // awaiting_approval：显示下一项申请，可能是第二阶段的结果披露。
  // success：展示 run.response；failed/cancelled：展示结束状态。
  return run;
}
```

前端收到 `awaiting_approval` 才呈现同意、拒绝与取消选项；不要默认自动批准。
显示 `approval.reason`、`tables`、`columns`、`scope`；不允许修改资源或提交任意 SQL。

## 原有查询与 SSE

`POST /api/v1/query` 在 LangChain 后端同样运行原生图，暂停时返回
`status=awaiting_approval` 与 `continuation={run_id, run_token, approval}`。
LangChain 版本的该接口也执行结果隐私控制，不再直接返回未批准的原始结果。
OpenAI 后端原有行为保留。

`POST /api/v1/chat/stream` 暂停时先发送 `approval.required`，其中包含 continuation，
再发送 `run.completed(status=awaiting_approval)` 关闭此次 HTTP 流；这表示本段连接结束，
不表示整个 run 已完成。前端随后调用 resume JSON 接口，可能再次收到审批申请。
现有 Streamlit 页面尚未添加审批按钮；自定义前端按上述 API 接入即可。

LangChain 汇总使用结构化 Agent 输出，当前发出完整的 `answer.completed`，不模拟 token
增量；阶段进度与 SQL 事件仍保留。OpenAI 后端原有正文 token 流不变。
SSE 结果卡片始终保持脱敏；批准后的原值从恢复接口的最终响应取得。
这不是 SSE 断线事件重放，也不提供 Last-Event-ID 回放。

## 验证

离线测试覆盖真实 LangGraph 路由、原生 create_agent/ToolStrategy、模拟 HTTP 模型传输、
跨 SQLite 检查点恢复、授权前模型和 API 不见原值、加密落盘、审批拒绝与重复恢复、
令牌、过期、取消、硬性拒绝、修复与预算。未在本次改动中调用真实外部 LLM。

官方设计参考：LangChain custom workflows、LangGraph interrupts 与 persistence。
