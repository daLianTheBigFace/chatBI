"""Exercise the real LangGraph, SQLite enforcement and durable interrupts offline."""

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager

import pytest
from cryptography.fernet import Fernet
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from chatbi.sql_agent.agent_models import GeneratedSQL, ParsedQuery
from chatbi.sql_agent.config import AccessConfig, DatabaseConfig, ExecutionConfig, WorkflowConfig
from chatbi.sql_agent.database import DatabaseService, SQLiteAdapter
from chatbi.sql_agent.models import SQLAgentRequest
from chatbi.sql_agent.summary import SummaryAnswer
from chatbi.sql_agent_langchain.contracts import PrivacyDecision, ResumeRequest
from chatbi.sql_agent_langchain.graph import SQLAgentGraph
from chatbi.sql_agent_langchain.service import LangChainSQLAgentService, RunAccessError


class ScriptedAgents:
    def __init__(self, *, access=False, disclosure=True, sql=None, repair_sql=None):
        self.access = access
        self.disclosure = disclosure
        self.sql = sql or "SELECT name FROM customers"
        self.repair_sql = repair_sql
        self.calls = []

    async def invoke(self, name, payload, history):
        self.calls.append((name, payload, history))
        if name in {"access_review", "disclosure_review"}:
            return PrivacyDecision(
                request_permission=self.access if name == "access_review" else self.disclosure,
                reason="需要原始姓名用于回答；将发送给模型并返回前端。",
                tables=["customers"] if name == "access_review" and self.access else [],
            )
        if name == "understanding":
            return ParsedQuery(
                status="ready",
                selected_tables=["customers"],
                metrics=[],
                dimensions=[],
                filters=[],
                time_range=None,
                ordering=[],
                top_n=None,
                assumptions=[],
                clarification_questions=[],
            )
        if name in {"generation", "repair"}:
            return GeneratedSQL(
                sql=self.repair_sql if name == "repair" else self.sql, parameters=[], assumptions=[]
            )
        if name == "summary":
            return SummaryAnswer(answer=json.dumps(payload["result"]["rows"], ensure_ascii=False))
        raise AssertionError(name)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "data").mkdir()
    with sqlite3.connect(tmp_path / "data" / "chinook.db") as connection:
        connection.executescript("""
            CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, secret TEXT);
            INSERT INTO customers VALUES (1, 'Alice-private', 'never-grant');
            CREATE TABLE artists (id INTEGER PRIMARY KEY, name TEXT);
            INSERT INTO artists VALUES (1, 'Public artist');
        """)
    return tmp_path


@asynccontextmanager
async def service_for(project, agents, *, access=None, approval=None, config=None):
    access = access or AccessConfig()
    config = config or WorkflowConfig()

    def factory(policy):
        return DatabaseService(
            SQLiteAdapter(DatabaseConfig(project_root=project), ExecutionConfig(), policy)
        )

    key_path = project / "test.key"
    if not key_path.exists():
        key_path.write_bytes(Fernet.generate_key())
    with factory(access) as database:
        async with AsyncSqliteSaver.from_conn_string(str(project / "checkpoints.sqlite")) as saver:
            await saver.setup()
            workflow = SQLAgentGraph(
                agents, database, factory, access, approval, config, saver, key_path.read_bytes()
            )
            yield LangChainSQLAgentService(workflow, config)


def test_disclosure_pause_restart_approve_no_repeated_query(project):
    async def scenario():
        agents = ScriptedAgents()
        async with service_for(project, agents) as service:
            view = await service.start(SQLAgentRequest(question="客户姓名"))
            assert view.status == "awaiting_approval", view
            assert view.approval.kind == "result_disclosure"
            assert "Alice-private" not in view.model_dump_json()
            assert "Alice-private" not in json.dumps(agents.calls)
            run_id, token, interrupt_id = view.run_id, view.run_token, view.approval.interrupt_id
        # A new service and SQLite saver recover the same pending native interrupt.
        resumed_agents = ScriptedAgents()
        async with service_for(project, resumed_agents) as service:
            recovered = await service.get_run(run_id, token)
            assert recovered.approval.interrupt_id == interrupt_id
            view = await service.resume(
                run_id, token, ResumeRequest(interrupt_id=interrupt_id, approved=True)
            )
            assert view.status == "success", view
            assert view.response.result.rows == [["Alice-private"]]
            assert view.response.metadata.execution_count == 1
            assert [c[0] for c in resumed_agents.calls] == ["summary"]
            assert "Alice-private" in json.dumps(resumed_agents.calls)
            with pytest.raises(RunAccessError) as error:
                await service.resume(
                    run_id, token, ResumeRequest(interrupt_id=interrupt_id, approved=True)
                )
            assert error.value.status_code == 409

    asyncio.run(scenario())


@pytest.mark.parametrize("approval", [False, None])
def test_decline_or_no_need_keeps_values_out_of_model_and_response(project, approval):
    async def scenario():
        agents = ScriptedAgents(disclosure=approval is not None)
        async with service_for(project, agents) as service:
            view = await service.start(SQLAgentRequest(question="查询客户"))
            if approval is not None:
                view = await service.resume(
                    view.run_id,
                    view.run_token,
                    ResumeRequest(interrupt_id=view.approval.interrupt_id, approved=False),
                )
            assert view.status == "success", view
            assert "Alice-private" not in view.model_dump_json()
            assert "never-grant" not in view.model_dump_json()
            assert "Alice-private" not in json.dumps(agents.calls)

    asyncio.run(scenario())


def test_database_grant_is_scoped_and_disclosure_is_separate(project):
    async def scenario():
        agents = ScriptedAgents(access=True)
        access = AccessConfig(allowed_tables=frozenset({"artists"}))
        ceiling = AccessConfig(
            allowed_tables=frozenset({"customers"}),
            allowed_columns={"customers": frozenset({"id", "name"})},
        )
        async with service_for(project, agents, access=access, approval=ceiling) as service:
            view = await service.start(SQLAgentRequest(question="客户姓名"))
            assert view.approval.kind == "database_access", view
            assert view.approval.columns == {"customers": ["id", "name"]}
            assert [c[0] for c in agents.calls] == ["access_review"]
            token = view.run_token
            view = await service.resume(
                view.run_id,
                token,
                ResumeRequest(interrupt_id=view.approval.interrupt_id, approved=True),
            )
            assert view.approval.kind == "result_disclosure", view
            assert "Alice-private" not in json.dumps(agents.calls)
            assert "secret" not in json.dumps(agents.calls)
            completed = await service.resume(
                view.run_id,
                token,
                ResumeRequest(interrupt_id=view.approval.interrupt_id, approved=True),
            )
            assert completed.status == "success", completed
            other = await service.start(SQLAgentRequest(question="客户姓名"))
            assert other.approval.kind == "database_access"

    asyncio.run(scenario())


def test_database_grant_rejected_without_execution(project):
    async def scenario():
        agents = ScriptedAgents(access=True)
        async with service_for(
            project,
            agents,
            access=AccessConfig(allowed_tables=frozenset({"artists"})),
            approval=AccessConfig(allowed_tables=frozenset({"customers"})),
        ) as svc:
            view = await svc.start(SQLAgentRequest(question="客户姓名"))
            view = await svc.resume(
                view.run_id,
                view.run_token,
                ResumeRequest(interrupt_id=view.approval.interrupt_id, approved=False),
            )
            assert view.status == "failed"
            assert view.response.error.code == "access_denied"
            assert view.response.metadata.execution_count == 0
            assert [c[0] for c in agents.calls] == ["access_review"]

    asyncio.run(scenario())


def test_hard_denial_cannot_be_overridden_by_model(project):
    async def scenario():
        agents = ScriptedAgents(access=True)
        async with service_for(
            project, agents, access=AccessConfig(allowed_tables=frozenset({"artists"}))
        ) as svc:
            view = await svc.start(SQLAgentRequest(question="客户姓名"))
            assert view.status == "failed", view
            assert view.response.metadata.execution_count == 0
            assert "access_review" not in [c[0] for c in agents.calls]

    asyncio.run(scenario())


def test_tokens_stale_decisions_cancel_and_expiry(project):
    async def scenario():
        async with service_for(project, ScriptedAgents()) as svc:
            view = await svc.start(SQLAgentRequest(question="客户姓名"))
            with pytest.raises(RunAccessError) as error:
                await svc.get_run(view.run_id, "x" * 43)
            assert error.value.status_code == 404
            with pytest.raises(RunAccessError) as error:
                await svc.resume(
                    view.run_id, view.run_token, ResumeRequest(interrupt_id="stale", approved=True)
                )
            assert error.value.status_code == 409
            cancelled = await svc.cancel(view.run_id, view.run_token)
            assert cancelled.status == "cancelled"
            with pytest.raises(RunAccessError):
                await svc.resume(
                    view.run_id,
                    view.run_token,
                    ResumeRequest(interrupt_id=view.approval.interrupt_id, approved=True),
                )
            another = await svc.start(SQLAgentRequest(question="客户姓名"))
            await svc.workflow.graph.aupdate_state(svc.config(another.run_id), {"expires_at": 0})
            with pytest.raises(RunAccessError) as error:
                await svc.get_run(another.run_id, another.run_token)
            assert error.value.status_code == 410

    asyncio.run(scenario())


def test_repair_routes_and_budget(project):
    async def scenario():
        agents = ScriptedAgents(
            sql="SELECT missing FROM customers", repair_sql="SELECT COUNT(*) FROM customers"
        )
        async with service_for(project, agents) as svc:
            view = await svc.start(SQLAgentRequest(question="客户数"))
            assert view.status == "success", view
            assert view.response.result.rows == [[1]]
            assert view.response.metadata.repair_count == 1
            assert "repair" in [c[0] for c in agents.calls]
        agents = ScriptedAgents(
            sql="SELECT missing FROM customers", repair_sql="SELECT missing FROM customers"
        )
        async with service_for(project, agents, config=WorkflowConfig(max_repairs=1)) as svc:
            view = await svc.start(SQLAgentRequest(question="客户数"))
            assert view.status == "failed", view
            assert view.response.error.code == "budget_exhausted"

    asyncio.run(scenario())


def test_checkpoint_does_not_store_raw_question_sql_or_result(project):
    async def scenario():
        async with service_for(project, ScriptedAgents()) as svc:
            view = await svc.start(SQLAgentRequest(question="private-question-marker 客户姓名"))
            assert view.status == "awaiting_approval"
            snapshot = await svc.workflow.graph.aget_state(svc.config(view.run_id))
            encoded = json.dumps(snapshot.values)
            assert "private-question-marker" not in encoded
            assert "Alice-private" not in encoded
            assert "SELECT name FROM customers" not in encoded
        data = (project / "checkpoints.sqlite").read_bytes()
        assert b"Alice-private" not in data
        assert b"private-question-marker" not in data
        assert b"SELECT name FROM customers" not in data

    asyncio.run(scenario())


def test_http_start_status_resume_and_sse_approval(project):
    from fastapi.testclient import TestClient

    from chatbi.api.app import create_app

    @asynccontextmanager
    async def factory(root):
        async with service_for(root, ScriptedAgents()) as service:
            yield service

    @asynccontextmanager
    async def no_vision(root):
        yield None

    app = create_app(project, service_factory=factory, fire_risk_factory=no_vision)
    with TestClient(app) as client:
        response = client.post("/api/v1/langchain/runs", json={"question": "客户姓名"})
        assert response.status_code == 200, response.text
        run = response.json()
        assert run["status"] == "awaiting_approval", run
        assert "Alice-private" not in response.text
        headers = {"X-Run-Token": run["run_token"]}
        url = "/api/v1/langchain/runs/" + run["run_id"]
        assert client.get(url).status_code == 422
        assert client.get(url, headers={"X-Run-Token": "x" * 43}).status_code == 404
        assert client.get(url, headers=headers).json()["approval"] == run["approval"]
        body = {"interrupt_id": run["approval"]["interrupt_id"], "approved": True}
        resumed = client.post(url + "/resume", headers=headers, json=body)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["response"]["result"]["rows"] == [["Alice-private"]]
        assert client.post(url + "/resume", headers=headers, json=body).status_code == 409
        stream = client.post("/api/v1/chat/stream", json={"question": "客户姓名"})
        assert "approval.required" in stream.text
        assert "awaiting_approval" in stream.text
        assert "Alice-private" not in stream.text


def test_specialists_use_real_create_agent_graph_and_messages():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    from chatbi.sql_agent_langchain.agents import LangChainAgents

    class ToolModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    async def scenario():
        model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call-summary",
                            "name": "SummaryAnswer",
                            "args": {"answer": "一位客户"},
                        }
                    ],
                )
            ]
        )
        agents = LangChainAgents(model, 5)
        output = await agents.invoke(
            "summary",
            {"question": "客户数"},
            [
                {"role": "user", "content": "分析客户"},
                {"role": "assistant", "content": "请提供问题"},
            ],
        )
        assert output.answer == "一位客户"
        assert "model" in agents._agents["summary"].get_graph().nodes
        assert set(agents.specifications) == {
            "understanding",
            "generation",
            "repair",
            "summary",
            "access_review",
            "disclosure_review",
        }

    asyncio.run(scenario())


def test_parallel_resume_only_runs_summary_once(project):
    async def scenario():
        agents = ScriptedAgents()
        async with service_for(project, agents) as svc:
            view = await svc.start(SQLAgentRequest(question="客户姓名"))
            decision = ResumeRequest(interrupt_id=view.approval.interrupt_id, approved=True)
            results = await asyncio.gather(
                svc.resume(view.run_id, view.run_token, decision),
                svc.resume(view.run_id, view.run_token, decision),
                return_exceptions=True,
            )
            assert sum(isinstance(r, RunAccessError) for r in results) == 1
            assert [c[0] for c in agents.calls].count("summary") == 1

    asyncio.run(scenario())


def test_cancel_running_resume(project):
    async def scenario():
        entered = asyncio.Event()

        class SlowAgents(ScriptedAgents):
            async def invoke(self, name, payload, history):
                if name == "summary":
                    entered.set()
                    await asyncio.Event().wait()
                return await super().invoke(name, payload, history)

        async with service_for(project, SlowAgents()) as svc:
            view = await svc.start(SQLAgentRequest(question="客户姓名"))
            task = asyncio.create_task(
                svc.resume(
                    view.run_id,
                    view.run_token,
                    ResumeRequest(interrupt_id=view.approval.interrupt_id, approved=True),
                )
            )
            await asyncio.wait_for(entered.wait(), 5)
            cancelled = await asyncio.wait_for(svc.cancel(view.run_id, view.run_token), 5)
            assert cancelled.status == "cancelled"
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (await svc.get_run(view.run_id, view.run_token)).status == "cancelled"

    asyncio.run(scenario())
