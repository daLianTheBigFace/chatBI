# SQL Agent 分阶段开发计划

日期：2026-10-22

## 开发约定

- 每阶段独立实现、验证并汇报，阶段结束后供用户 review。
- 业务默认值集中放入 config.py，状态与错误标识集中放入 enums.py。
- 优先以 Protocol 定义可替换策略，使用构造函数注入具体实现。
- 固定流程使用普通方法，不为每个步骤创建策略或 Agent。
- 默认数据源为项目根目录下 data/chinook.db，后续仅以只读方式访问。
- 不修改既有演示笔记本，不提交本地 IDE 配置。
- HTTP 服务采用 FastAPI，在服务接口和工作流阶段接入；路由层不包含 Agent 和数据库实现细节。
- 模型配置在应用启动时统一从项目 .env 和环境变量加载，再通过构造函数注入。Agent 不自行读取环境变量，默认关闭 tracing。

## 阶段与验收

| 阶段 | 交付 | 验收 | 状态 |
| --- | --- | --- | --- |
| 1 | 集中配置、数据契约、错误类型、策略接口 | 不调用模型，验证请求、响应及配置边界 | 已完成，待 review |
| 2 | SQLite 适配器、schema 获取、数据库统一入口 | 在 Chinook 上读取元数据，验证只读连接、缺失文件及资源清理；完整校验前不对外暴露任意 SQL 执行接口 | 已完成，待 review |
| 3 | 组合校验策略与执行防护 | 覆盖合法查询、写操作、访问范围、截断和真正取消 | 已完成，待 review |
| 4 | 问题理解、表选择、SQL 生成 | 结构化输出，基于真实 schema 生成候选 SQL | 已完成，待 review |
| 5 | 工作流与错误修复 | 每个候选重新校验，修复次数和执行次数有上限 | 已完成，待 review |
| 6 | 结果处理、回答汇总、FastAPI 服务接口 | 返回成功、澄清及失败状态，支持汇总降级 | 已完成，待 review |
| 7 | 离线问题生成与评估集 | 独立参考 SQL 和结果比较，不将模型输出直接视为标准答案 | 待开发 |

## 第 1 阶段记录

已新增 sql_agent 模块的配置、枚举、数据模型、异常及策略协议。
SQL 查询使用绑定参数字典，查询结果使用列名列表与行数组，保留重复列名。
模型强制检查状态与响应内容的一致性，空结果仍允许作为成功响应。
字符串规范化仅用于用户问题和 SQL 文本，不修改数据库值和绑定参数。

策略协议当前包括 schema 选择、SQL 校验和修复决策。数据库适配接口与结果处理策略在对应阶段定义，避免提前固定尚未验证的接口。

Pydantic 已作为直接依赖声明并更新锁文件；本阶段未调用模型、未添加 SQLAlchemy，也未执行数据库查询。

后续各阶段在引入新能力时添加对应数据契约，而不是一次性堆砌全部状态和接口。

## 第 2 阶段记录

- 安装 SQLAlchemy 2.x Core，通过 DatabaseAdapter Protocol 注入数据库实现。
- DatabaseService 提供 get_schema、get_table_sample、close 和上下文管理。
- SQLiteAdapter 使用原生 mode=ro URI，叠加 query_only，禁用扩展加载。
- 查询连接使用 NullPool，连接在每次操作结束时关闭，服务关闭后拒绝继续访问。
- 不存在的数据库文件不会自动创建，损坏的数据库返回统一配置错误。
- 样例表名必须匹配已读取的 schema，并使用 SQLAlchemy 构造查询，不拼接用户表名。
- 样例读取受 sample_rows 和 max_rows 限制，通过多读取一行判断截断；有主键时按主键排序。
- 保留 NULL 和文本空格，二进制样例以 hex: 前缀的十六进制字符串表示。
- Chinook 实测包含 11 张表，artists 样例读取成功，读取前后数据库 SHA-256 一致。
- 修正 errors.py 对不存在的 types 模块的引用，统一使用 enums。

本阶段验收为 28 项测试通过，Ruff 检查通过，锁文件检查通过。
pytest 仍会提示历史 Windows 临时目录清理警告，不影响测试断言。

第 2 阶段结束时仅支持本地受信任数据库的元数据与样例读取，不对 Agent 开放。
当时结果字节预算、底层执行取消、字段与函数访问策略尚未生效，已在第 3 阶段接入（见下文）。

在项目根目录可按以下方式调用（需要 uv run 环境）：

```python
from pathlib import Path

from chatbi.sql_agent.config import DatabaseConfig, ExecutionConfig
from chatbi.sql_agent.database import DatabaseService, SQLiteAdapter

adapter = SQLiteAdapter(
    config=DatabaseConfig(project_root=Path.cwd()),
    execution_config=ExecutionConfig(),
)
with DatabaseService(adapter) as database:
    schema = database.get_schema()
    sample = database.get_table_sample("artists")
```

## 第 3 阶段记录

### 能力与策略

- SQLGlot 按 SQLite 方言解析 SQL，并通过 ReadOnlyRule、SchemaRule、ParameterRule 组合校验。
- CompositeSQLValidationPolicy 支持注入规则，遇到首个失败立即拒绝。
- 允许单条只读查询，包括合法 CTE、JOIN、UNION、子查询和窗口函数。
- 禁止多语句、写操作、DDL、PRAGMA、ATTACH、外部 schema、内部表及表值函数。
- schema 校验检查来源表和列解析；只能使用完整匹配的命名 :parameter 参数，拒绝缺失、多余或位置参数。
- AccessConfig 通过表、字段、函数许可范围约束访问，名称按大小写不敏感匹配。
- 元数据、样例和执行使用同一许可范围；未许可字段不会通过样例接口返回。
- v1 不开放视图、虚拟表及其 shadow tables。至少需要支持 PRAGMA table_list 的 SQLite 3.37；当前开发环境已验证。

### 统一执行入口

DatabaseService.execute_query 是异步接口。SQLExecutor 把同步数据库工作放入线程执行。
SQLiteAdapter.execute_query 内部重新校验，调用方不需要也不能通过“已经校验”的标记跳过验证。
查询进入执行前复制 SQL 和参数，避免调用方后续修改同一对象。

静态 validate_query 只返回 AST、schema 和参数校验结果；SQLite 方言、函数许可及实际可执行性通过执行入口内部 EXPLAIN QUERY PLAN 和 authorizer 再检查。
静态校验通过不代表能够执行，更不代表业务统计正确。

SQLite authorizer 只许可读取批准表字段、SELECT/递归查询和批准函数，默认拒绝其余动作。
即便注入的校验策略错误放行，数据库层仍会拒绝访问未许可表。
mode=ro 和 query_only 保留为额外防护，SQLAlchemy 日志配置隐藏绑定参数。

### 预算、超时与取消

- 每次查询按 max_rows 限制返回行，读取额外一行判断 truncated，空结果与刚好达到上限不会误判。
- max_result_bytes 约束 UTF-8 紧凑 JSON 的 columns 和 rows 数据载荷，不包含耗时等响应元数据。
- 逐行计量字节，超过预算时返回已收集部分并标记 truncated；列头本身超过预算则返回预算错误。
- SQLite 单值大小、列数、表达式深度及 SQL 长度也设有限制，超大值可能在 SQLite 内部直接报预算错误，而不是返回部分值。
- 为兼容较小的结果预算，SQLite 单值限制最小为 64 KiB；这与返回载荷预算不同，不宣称是整个进程的内存上限。
- progress handler 检查真实执行时间及取消事件，超时中断 SQLite，而不是只中断等待协程。
- 协程取消后等待工作线程退出和连接清理再传播 CancelledError；锁等待受 SQLite busy timeout 限制，不能承诺瞬时取消。
- 当前 query_timeout_seconds 面向实际查询执行；元数据读取、校验、模型调用及排队的总请求预算在后续工作流阶段落实。
- 数字为无穷等无法安全序列化的结果返回受控错误，不输出非法 JSON。

### 调用示例

在项目根目录执行以下异步示例：

```python
import asyncio
from pathlib import Path

from chatbi.sql_agent.config import AccessConfig, DatabaseConfig, ExecutionConfig
from chatbi.sql_agent.database import DatabaseService, SQLiteAdapter
from chatbi.sql_agent.models import SQLQuery

async def main():
    adapter = SQLiteAdapter(
        config=DatabaseConfig(project_root=Path.cwd()),
        execution_config=ExecutionConfig(),
        access_config=AccessConfig(allowed_tables=frozenset({"artists", "albums"})),
    )
    with DatabaseService(adapter) as database:
        result = await database.execute_query(SQLQuery(
            sql="SELECT Name FROM artists WHERE ArtistId = :artist_id",
            parameters={"artist_id": 1},
        ))
        print(result.model_dump())

asyncio.run(main())
```

### 验证结果

77 项测试通过，覆盖既有契约、Chinook 只读查询、独立 sqlite3 参考结果比较、安全 SQL、参数注入、访问范围、策略扩展、超时、取消、结果预算及异常数据。
Chinook 查询前后 SHA-256 一致；此次没有接入模型，也没有进入第 4 阶段。

当前边界仍为本地受信任 SQLite 文件，不将这些防护描述为任意恶意数据库文件或任意 Python 代码的沙箱。

## 第 4 阶段记录

### 配置与资源生命周期

`chatbi.settings.load_model_config(project_root)` 显式读取项目 .env，系统环境变量优先。
加载函数不会修改 os.environ，也不在模块 import 时加载配置、创建连接或发送请求。
应用启动时调用一次，后续把 ModelConfig 注入 AgentsSDKRunner 并复用；本阶段不新增全局单例。

支持已确认的 OPENAI_API_KEY、OPENAI_BASE_URL、OPENAI_MODEL，三项均要求有效。
.env.example 已补齐变量名，实际 .env 不修改、不提交、不在日志输出。
API Key 使用 SecretStr，排除在配置 repr 和默认 model_dump 之外。
配置错误和模型异常返回标准错误，不回传原始服务响应、密钥或完整配置内容。

AgentsSDKRunner 显式创建 AsyncOpenAI 和 OpenAIResponsesModel，不修改框架全局默认客户端。
每次运行设置 tracing_disabled=True、trace_include_sensitive_data=False。
不添加数据库工具或代码执行器，也不发送数据库样例内容。
通过异步上下文管理释放 HTTP 客户端；未来 FastAPI lifespan 负责统一创建和关闭客户端及数据库服务。

默认每次模型阶段最多 1 turn、墙钟预算 45 秒、自动重试 0 次，均定义在配置中。
不将单阶段预算误认为未来工作流的整体请求预算。

### 阶段接口与策略

- QueryUnderstandingStage.parse(question, schema, reference_date)：返回 ParsedQuery。
- ExplicitTableSelectionStrategy.select(parsed_query, schema)：按真实 schema 选择表，不采用模型生成的表定义。
- SQLGenerationStage.generate(question, parsed_query, selected_schema, reference_date)：返回 GeneratedSQL。
- StructuredAgentRunner：可注入的模型运行协议，生产使用 SDK runner，测试使用离线替身。

ParsedQuery 明确 ready 与 needs_clarification 状态，信息不足时不进入 SQL 生成。
表选择按名称大小写不敏感匹配，未知表被拒绝，输出 schema 深拷贝并过滤失效的关系引用。
SQL 生成仅接收选定 schema、原始问题、解析意图、参考日期及许可函数，不接收连接配置和密钥。

GeneratedSQL 使用参数列表满足严格 JSON Schema，再通过 as_query() 转换为数据库 SQLQuery。
参数名称须合法且唯一；结果是待校验 SQL，不代表 SQL 已经安全或业务正确。
版本化 Prompt 集中放在 prompts.py，明确输入数据不能覆盖系统规则，必要业务口径需要澄清。

SchemaSelectionStrategy 的接口随首个实现调整为接收 ParsedQuery，避免再次调用模型解析同一个问题。
目前表选择仅实现显式选择策略，没有扩展向量检索或表相关性评分。

### 验证与服务决策

离线测试不读取真实 API Key，不默认调用网络；覆盖配置优先级、密钥隐藏、严格输出 schema、
模型错误与超时、客户端释放、未知表、澄清停止、schema 隔离及绑定参数转换。

另做了一次真实服务最小验证：仅提供 artists 表结构和“统计 artists 表中的艺术家总数”问题，
问题理解返回 ready，生成 SQL 通过既有校验，SQLite 执行返回 275。
这验证了当前配置服务在该样例下支持 Responses 与结构化输出，不等于通过完整业务评估。
真实调用未写入常规测试，以免 pytest 意外产生费用或依赖外部服务。

本阶段结束时全量 108 项测试通过，Ruff 和锁文件检查通过；.env 已确认被 Git 忽略。
pytest 仍有历史 Windows 临时目录清理警告，不影响测试结果。

FastAPI 是已确认的 HTTP 服务框架；第 4 阶段未安装或实现路由。整体编排、错误修复、
总请求预算在第 5 阶段，回答汇总和 FastAPI 接口在第 6 阶段，继续按阶段推进。

SDK 集成参考：[OpenAI 官方 Agents SDK 文档](https://developers.openai.com/api/docs/guides/agents/sdk)。

## 第 5 阶段记录

### 固定流程与状态隔离

新增 SQLAgentWorkflow：读取许可 schema → 问题理解 → 澄清或表选择 → SQL 生成 →
选定 schema 内静态校验 → 数据库统一执行 → 返回结构化结果。
每个候选在工作流和执行入口分别校验，不能通过模型标记“已校验”绕过检查。

SQLAgentState 保存每次请求的阶段、参考日期、当前 SQL、结果、假设及计数；
QueryAttempt 保存各候选的 SQL 快照、校验、受控错误和执行结果。
状态在 ask 内创建，不保存在共享服务或 Agent 实例中，不默认写入日志或接口响应。
响应仅增加 execution_count、repair_count、elapsed_ms 元数据，不暴露所有中间内容。

### 受限修复

新增 SQLRepairStage 及版本化修复 Prompt；输入原始问题、原始意图、选定 schema、
候选 SQL、参考日期和受控错误。输出仍采用 GeneratedSQL，不执行模型生成的代码。
Prompt 要求保留用户的条件和口径，不用删除筛选条件来获得非空结果。
代码层能强制访问范围和预算，业务语义是否保持仍须后续基准集验证，不能仅凭 Prompt 保证。

BudgetedSQLRepairPolicy 通过策略协议注入，默认仅对标记为可修复的 INVALID_SQL、
EXECUTION 错误进行修复。权限拒绝、数据库配置问题、模型异常、超时、预算错误均不修复。
即使自定义策略要求放行，工作流也不会重试权限拒绝或超时。
空结果正常返回，修复后的 SQL 从完整校验重新开始，不能扩大选定表范围。

默认最多 2 次修复、3 次执行调用；校验失败不计执行次数，每次修复调用前先占用修复预算。
max_repairs=0 禁止自动修复。任一预算耗尽返回 BUDGET_EXHAUSTED，
状态内保留原始失败原因，避免无限循环或在执行预算耗尽后继续请求模型。
响应中的 SQL 和结果来自同一次成功执行，失败候选的假设不会混入最终响应。

### Python 服务入口与生命周期

SQLAgentService.ask(SQLAgentRequest) 提供统一异步接口，返回 success、needs_clarification 或 failed。
request_id 由服务生成，每次请求快照输入，并获取可注入的参考日期（默认服务器本地日期）。
session_id 仍为预留字段，不加载历史会话；澄清后需重新提交包含完整信息的问题。

open_sql_agent(project_root, ...) 是应用组装入口：启动时统一加载模型配置，构造数据库、
共享模型 runner、各阶段与策略，并在退出时关闭数据库及 HTTP 客户端。
允许显式注入模型、执行、工作流和访问配置，不在 import 时连接网络。
此上下文管理器可在后续 FastAPI lifespan 中直接复用。

第 5 阶段结束时 success 响应包含 query、result、warnings 和 metadata，answer 为 None；
自然语言结果汇总及 FastAPI 路由仍按第 6 阶段实现。

### 整体请求预算与取消

一次 ask 使用一个 request_timeout_seconds（默认 90 秒）预算，覆盖 schema、理解、选择、
生成、校验、执行及全部修复，不为每次修复重置计时。模型和数据库原有单次预算同时生效。
同步元数据和校验放到工作线程，取消时等待线程退出，不遗留后台操作。
SQL 取消沿用 SQLite progress handler。达到整体预算时返回 TIMEOUT；
调用方主动取消则传播 CancelledError，不伪装成普通失败或启动修复。

为确保资源清理，响应可能晚于预算边界才返回，尤其是 SQLite 锁等待或不可中断的同步准备过程。
整体预算表示停止后续工作并启动取消，不能宣称任何平台上都能精确按时硬终止线程。
未预期异常映射为统一 INTERNAL 错误，不向响应输出异常堆栈或私有细节。

### 使用示例

在项目根目录的 uv 环境中运行：

```python
import asyncio
from pathlib import Path

from chatbi.sql_agent.bootstrap import open_sql_agent
from chatbi.sql_agent.models import SQLAgentRequest

async def main():
    async with open_sql_agent(Path.cwd()) as service:
        response = await service.ask(SQLAgentRequest(question="统计艺术家总数"))
        print(response.model_dump(mode="json"))

asyncio.run(main())
```

### 验证

全量 130 项离线测试通过，新增覆盖校验修复、SQLite 执行报错修复、修复再次校验、
独立预算、零修复预算、自定义策略、空结果、单一总期限、取消、资源释放和并发状态隔离。
测试使用真实临时 SQLite 数据库与替身模型，不依赖真实密钥。
pytest 仍存在历史 Windows 临时目录清理警告，不影响断言。

真实服务最小验证通过 open_sql_agent → SQLAgentService.ask 完成：状态 success，
Chinook 艺术家总数 275，execution_count=1、repair_count=0。
这仅验证当前端点下该样例的端到端连通，不作为整体准确率或性能基准。

## 第 6 阶段记录

### 结果处理与回答策略

新增 AnswerSynthesisStrategy，通过 SQLAgentService 构造函数注入。
默认 Python 服务可使用基础说明；open_sql_agent 组装时注入 ModelAnswerSynthesisStrategy，
沿用共享的 Responses 模型 runner、结构化输出及 tracing 关闭配置。
基础说明覆盖标量、NULL、空结果、多行结果与截断，不额外执行统计 SQL。

SummaryConfig 默认最多发送 20 行、32 KiB UTF-8 JSON 输入。
字节预算包含问题、列头、行数据及覆盖信息；超限时逐行减少，不能容纳输入时直接使用基础说明。
数值不预先舍入，不将原始结果行改成字典，保留重复列名和字段顺序。
模型仅接收问题、列名、受限行和返回范围，不接收连接信息或模型密钥。
提示词将问题、字段名和单元格作为不可信数据，禁止根据样本推断全量总计、排名与因果。
行采样和 SQL 结果截断使用不同标识，并在回答中明确提示范围。
这些约束降低误导风险，但不构成模型答案正确性的证明；业务准确性评估仍在第 7 阶段。

服务先保存成功查询的基础说明，再在原有总请求期限内尝试模型汇总。
模型报错、无效输出或汇总耗尽期限时保留 success、SQL 和完整返回数据，附加汇总降级告警。
查询本身未完成时超时仍为 failed；调用方主动取消继续传播 CancelledError。
传入汇总策略的数据是深拷贝，策略修改不会污染原始查询结果。

### HTTP 接口与生命周期

新增 chatbi.api.app.create_app 应用工厂。模块导入和创建 app 不读取 .env 或发起网络调用。
lifespan 启动时调用 open_sql_agent，统一加载配置和创建客户端，退出时清理所有资源。
默认项目根目录基于源码位置定位，显式 create_app(project_root) 可覆盖。
默认启动命令适用于当前 uv 项目环境；若安装到独立部署目录，需要显式指定项目根目录。
测试可通过 service_factory 注入替身，不需要真实密钥。

| 接口 | 约定 |
| --- | --- |
| POST /api/v1/query | SQLAgentRequest → SQLAgentResponse；三种业务状态均为 HTTP 200，调用方检查 status |
| GET /health | 应用完成启动，不代表模型端点实时健康；不产生模型调用 |
| GET /docs | Swagger，可直接提交查询 |
| 请求校验失败 | HTTP 422，拒绝空问题和未知字段 |

请求不接受数据库路径、任意 SQL、模型配置或访问许可范围。
session_id 仍是预留字段，不持久化对话。此阶段仅面向本地单用户，没有认证与任务队列。
README 已补充 uv 同步、127.0.0.1 启动、PowerShell 请求示例和返回语义。

### 验证

全量 145 项离线测试通过，覆盖输入行数和字节预算、数值精度、空结果与 NULL、
采样提示、模型失败和无效输出降级、汇总总期限、取消传播、结果拷贝、HTTP 三种业务状态、
参数拒绝、lifespan 启停和启动失败。Ruff、格式及 uv 锁文件检查通过。
历史 Windows 临时目录清理仍有 pytest 警告，不影响测试断言。

真实模型通过 FastAPI TestClient 和 lifespan 完成端到端验证：HTTP 200、status=success、
artists 计数 275，自然语言回答包含 275，未使用汇总降级，执行 1 次、修复 0 次。
业务假设可能正常出现在 warnings；warnings 非空本身不表示汇总失败。
该样例仅验证当前模型端点连通与流程，不代表整体准确率。

第 7 阶段离线问题生成、参考 SQL 和评估集尚未实现，等待本阶段 review 后继续。

## Chatbot 展示优化：SSE 与交互契约

按用户新增需求，在第 7 阶段之前先实现面向聊天前端的展示能力。
新增 POST /api/v1/chat/stream，保留原有 JSON 查询接口。
ChatRequest 和 ChatEvent 均由 Pydantic 定义，payload 为 type 判别联合，拒绝额外字段。
公共字段覆盖协议版本、对话 ID、用户/助手消息 ID、执行 ID、事件 ID、UTC 时间和递增序号。
type 用于事件路由，tag 用于推荐组件类别，block_id 和 tool_call_id 关联正文与 SQL 卡片。

固定工作流通过 request-local WorkflowObserver 发出阶段事件及每次候选 SQL 的事件。
校验与真正执行分开标识；修复候选有独立调用 ID，每次成功执行返回独立结果表。
常规错误、总期限与执行失败会关闭未完成的 SQL 卡片，不暴露原始数据库/模型异常。
公开过程面板只显示固定的简短阶段说明，不转发模型内部推理。

汇总通过 Agents SDK Runner.run_streamed 读取 output_text.delta，其他模型步骤仍保持严格结构化输出。
为避免跨 chunk 敏感值漏出，展示文本按行缓冲脱敏后发送；短单行回答可能在结束时才出现。
最终 answer.completed 必须替换临时正文，因此汇总失败后的降级内容不会与半截答案拼接。
对模型内外部策略统一限制回答总字符数，沿用原有整体请求期限。

DisplayRedactionStrategy 独立于数据库权限，通过服务构造函数的 display_factory 注入。
默认隐藏 SQL 字面量/参数值和文本结果，按常见敏感字段规则隐藏部分数值，再清理展示文本。
脱敏状态按请求隔离；SSE 汇总模型接收脱敏数据，不修改实际查询与原始结果。
此策略偏保守且无法识别所有任意别名的敏感字段；完整行为与边界见 chat-stream-protocol.md。
原有 /api/v1/query 保留原始数据响应，不适用于需要此次脱敏策略的聊天展示。

SSE 使用 32 项有界队列形成背压，空闲约 15 秒发送注释心跳。
断开连接会关闭迭代器并取消 producer、模型和当前执行；兼容 ASGI 2.3 断连及 2.4 send 异常。
正常 run 只有一个结束事件；客户端断开时无法保证收到结束事件。
未实现事件持久化、幂等提交、自动重连回放和多轮历史，不将展示用 conversation_id 解释为持久会话。

事件 JSON Schema 可通过 GET /api/v1/chat/events/schema 获取，docs 中同步保存静态快照。
chat-stream-protocol.md 包含完整事件表、生命周期、隐私边界和支持网络分块的 fetch 示例。
README 补充流式入口。当前没有实现实际聊天前端。

全量 170 项测试通过；新增 25 项覆盖事件契约、顺序与标识、独立重试结果、执行/校验失败、
澄清、脱敏、原始数据不变、跨 chunk 邮箱遮盖、汇总降级、总期限、原生流实时增量、
心跳、并发隔离、有界背压、SSE framing、HTTP 参数和 Schema、断连清理以及 SDK 推理事件过滤。
Ruff、格式、uv 锁文件检查通过；历史 Windows pytest 临时目录清理警告仍存在。
真实模型 SSE 端到端验证返回 artists=275，有 answer.delta、answer.completed 与唯一 run.completed，
status=success，执行 1 次、修复 0 次，无汇总降级。
