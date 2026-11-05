"""Checkpointed multi-agent SQL workflow with explicit human approval nodes.

Interrupt nodes have no database/model side effects: LangGraph replays a node
when Command(resume=...) is supplied. SQL execution is always a separate node.
"""

import hashlib
import json
from collections.abc import Callable
from contextvars import ContextVar
from datetime import date
from typing import TypedDict
from uuid import UUID

from cryptography.fernet import Fernet
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from chatbi.sql_agent.agent_models import GeneratedSQL, ParsedQuery
from chatbi.sql_agent.agents.stages import ExplicitTableSelectionStrategy
from chatbi.sql_agent.config import CHINOOK_PUBLIC_CATALOG_TABLES, AccessConfig, WorkflowConfig
from chatbi.sql_agent.database.executor import run_in_worker
from chatbi.sql_agent.database.service import DatabaseService
from chatbi.sql_agent.enums import ErrorCode, UnderstandingStatus, WorkflowStage
from chatbi.sql_agent.errors import SQLAgentError
from chatbi.sql_agent.models import DatabaseSchema, QueryResult, SQLQuery
from chatbi.sql_agent.redaction import PublicCatalogDisplayRedaction
from chatbi.sql_agent.repair import REPAIRABLE_CODES
from chatbi.sql_agent.summary import (
    MAX_ANSWER_CHARACTERS,
    PARTIAL_ANSWER,
    SAMPLE_ANSWER,
    SUMMARY_FALLBACK_WARNING,
    basic_answer,
)
from chatbi.sql_agent_langchain.agents import LangChainAgents
from chatbi.sql_agent_langchain.contracts import PrivacyDecision

# Transient observers are deliberately excluded from persisted graph state.
observer_context: ContextVar = ContextVar("langchain_sql_observer", default=None)


class SQLGraphState(TypedDict, total=False):
    request_id: str
    token_hash: str
    request_ciphertext: str
    reference_date: str
    schema: dict
    eligible_schema: dict
    selected_schema: dict
    parsed_ciphertext: str
    query_ciphertext: str
    result_ciphertext: str
    safe_result: dict
    error: dict | None
    tool_call_id: str | None
    pending: dict | None
    access_tables: list[str]
    access_approved: bool
    disclosure_approved: bool
    execution_count: int
    repair_count: int
    response_ciphertext: str
    cancelled: bool
    elapsed_ms: float
    policy_hash: str
    expires_at: float


class SQLAgentGraph:
    def __init__(
        self,
        agents: LangChainAgents,
        database: DatabaseService,
        database_factory: Callable[[AccessConfig], DatabaseService],
        access: AccessConfig,
        approval_access: AccessConfig | None,
        config: WorkflowConfig,
        checkpointer,
        encryption_key: bytes,
    ):
        self.agents = agents
        self.database = database
        self.database_factory = database_factory
        self.access = access
        self.approval_access = approval_access
        self.config = config
        self.cipher = Fernet(encryption_key)
        self.cipher_id = hashlib.sha256(encryption_key).hexdigest()
        builder = StateGraph(SQLGraphState)
        nodes = {
            "schema": self.load_schema,
            "access_review": self.access_review,
            "access_approval": self.access_approval,
            "understanding": self.understanding,
            "selection": self.selection,
            "generation": self.generation,
            "validation": self.validation,
            "execution": self.execution,
            "repair": self.repair,
            "disclosure_review": self.disclosure_review,
            "disclosure_approval": self.disclosure_approval,
            "summary": self.summary,
        }
        for name, node in nodes.items():
            builder.add_node(name, node)
        builder.add_edge(START, "schema")
        builder.add_edge("schema", "access_review")
        builder.add_conditional_edges(
            "access_review", lambda s: "access_approval" if s.get("pending") else "understanding"
        )
        builder.add_conditional_edges(
            "access_approval", lambda s: END if s.get("response_ciphertext") else "understanding"
        )
        builder.add_conditional_edges(
            "understanding", lambda s: END if s.get("response_ciphertext") else "selection"
        )
        builder.add_edge("selection", "generation")
        builder.add_edge("generation", "validation")
        builder.add_conditional_edges("validation", self.after_validation)
        builder.add_conditional_edges("execution", self.after_execution)
        builder.add_edge("repair", "validation")
        builder.add_conditional_edges(
            "disclosure_review", lambda s: "disclosure_approval" if s.get("pending") else "summary"
        )
        builder.add_edge("disclosure_approval", "summary")
        builder.add_edge("summary", END)
        self.graph = builder.compile(checkpointer=checkpointer, name="langchain_sql_multi_agent")

    async def progress(self, stage):
        if observer := observer_context.get():
            await observer.progress(stage)

    async def invoke(self, name, state, **payload):
        return await self.agents.invoke(
            name,
            {
                "question": self.request(state)["question"],
                "reference_date": state["reference_date"],
                **payload,
            },
            self.request(state).get("history", []),
        )

    def seal(self, value: dict) -> str:
        return self.cipher.encrypt(json.dumps(value, ensure_ascii=False).encode()).decode()

    def open(self, value: str) -> dict:
        return json.loads(self.cipher.decrypt(value.encode()).decode())

    def request(self, state):
        return self.open(state["request_ciphertext"])

    def parsed(self, state):
        return ParsedQuery.model_validate(self.open(state["parsed_ciphertext"]))

    def query(self, state):
        return SQLQuery.model_validate(self.open(state["query_ciphertext"]))

    def result(self, state):
        return QueryResult.model_validate(self.open(state["result_ciphertext"]))

    def effective_schema(self, state):
        base = DatabaseSchema.model_validate(state["schema"])
        if not state.get("access_approved"):
            return base
        eligible = DatabaseSchema.model_validate(state["eligible_schema"])
        tables = {t.name.casefold(): t.model_copy(deep=True) for t in base.tables}
        for table in eligible.tables:
            if table.name.casefold() not in state["access_tables"]:
                continue
            key = table.name.casefold()
            if key in tables:
                columns = {c.name.casefold(): c for c in tables[key].columns}
                columns.update({c.name.casefold(): c for c in table.columns})
                tables[key].columns = list(columns.values())
                tables[key].foreign_keys = list(
                    {
                        json.dumps(k.model_dump(), sort_keys=True): k
                        for k in tables[key].foreign_keys + table.foreign_keys
                    }.values()
                )
            else:
                tables[key] = table.model_copy(deep=True)
        return DatabaseSchema(tables=list(tables.values()))

    def effective_access(self, state):
        schema = self.effective_schema(state)
        return AccessConfig(
            allowed_tables=frozenset(t.name for t in schema.tables),
            allowed_columns={t.name: frozenset(c.name for c in t.columns) for t in schema.tables},
            allowed_functions=self.access.allowed_functions,
        )

    async def load_schema(self, state):
        await self.progress(WorkflowStage.SCHEMA)
        schema = await run_in_worker(self.database.get_schema)
        eligible = DatabaseSchema(tables=[])
        if self.approval_access is not None:
            # Only configured, delegatable metadata is discoverable by the privacy agent.
            with self.database_factory(self.approval_access) as database:
                eligible = await run_in_worker(database.get_schema)
        return {"schema": schema.model_dump(), "eligible_schema": eligible.model_dump()}

    async def access_review(self, state):
        eligible = DatabaseSchema.model_validate(state["eligible_schema"])
        if not eligible.tables:
            return {"pending": None}
        decision: PrivacyDecision = await self.invoke(
            "access_review", state, schema=state["schema"], eligible_schema=state["eligible_schema"]
        )
        if not decision.request_permission:
            return {"pending": None}
        requested = {name.casefold() for name in decision.tables}
        allowed = {t.name.casefold(): t for t in eligible.tables}
        if not requested or not requested <= allowed.keys():
            raise SQLAgentError(ErrorCode.ACCESS_DENIED, "Requested resources are not delegatable")
        return {
            "access_tables": sorted(requested),
            "pending": {
                "kind": "database_access",
                "reason": (
                    "需要读取当前权限未覆盖的字段。相关数据可能发送给模型并返回前端。"
                    "授权仅适用于本次运行。"
                ),
                "tables": sorted(requested),
                "columns": {
                    name: [c.name for c in allowed[name].columns] for name in sorted(requested)
                },
                "scope": "current_run",
            },
        }

    @staticmethod
    def safe_reason(reason):
        return PublicCatalogDisplayRedaction(CHINOOK_PUBLIC_CATALOG_TABLES).text(reason)

    async def access_approval(self, state):
        approved = interrupt(state["pending"])
        if approved is not True:
            return {
                "pending": None,
                "access_approved": False,
                "response_ciphertext": self.seal(
                    self.failure(state, ErrorCode.ACCESS_DENIED, "用户拒绝了数据访问申请。")
                ),
            }
        return {"pending": None, "access_approved": True}

    async def understanding(self, state):
        await self.progress(WorkflowStage.UNDERSTANDING)
        schema = self.effective_schema(state)
        parsed: ParsedQuery = await self.invoke("understanding", state, schema=schema.model_dump())
        if parsed.status == UnderstandingStatus.NEEDS_CLARIFICATION:
            return {
                "parsed_ciphertext": self.seal(parsed.model_dump()),
                "response_ciphertext": self.seal(
                    {
                        "request_id": state["request_id"],
                        "status": "needs_clarification",
                        "clarification_questions": [
                            self.safe_reason(q) for q in parsed.clarification_questions
                        ],
                    }
                ),
            }
        return {"parsed_ciphertext": self.seal(parsed.model_dump())}

    async def selection(self, state):
        await self.progress(WorkflowStage.SELECTION)
        selected = await ExplicitTableSelectionStrategy().select(
            self.parsed(state), self.effective_schema(state)
        )
        return {"selected_schema": selected.model_dump()}

    async def generation(self, state):
        await self.progress(WorkflowStage.GENERATION)
        output: GeneratedSQL = await self.invoke(
            "generation",
            state,
            parsed_query=self.parsed(state).model_dump(),
            schema=state["selected_schema"],
            allowed_functions=sorted(self.access.allowed_functions),
        )
        return {"query_ciphertext": self.seal(output.as_query().model_dump()), "error": None}

    async def validation(self, state):
        observer = observer_context.get()
        call_id = None
        if observer is not None:
            call_id = await observer.sql_started(state["repair_count"] + 1, self.query(state))
        await self.progress(WorkflowStage.VALIDATION)
        with self.database_factory(self.effective_access(state)) as database:
            validation = await run_in_worker(lambda: database.validate_query(self.query(state)))
        if validation.issues and observer is not None:
            await observer.sql_failed(call_id, "validation", validation.issues[0].code)
        return {
            "error": validation.issues[0].model_dump() if validation.issues else None,
            "tool_call_id": str(call_id) if call_id else None,
        }

    def after_validation(self, state):
        return self.repair_route(state) if state.get("error") else "execution"

    def after_execution(self, state):
        return self.repair_route(state) if state.get("error") else "disclosure_review"

    def repair_route(self, state):
        error = state["error"]
        if not error["repairable"] or error["code"] not in REPAIRABLE_CODES:
            raise SQLAgentError(ErrorCode(error["code"]), error["message"])
        if (
            state["repair_count"] >= self.config.max_repairs
            or state["execution_count"] >= self.config.max_executions
        ):
            raise SQLAgentError(ErrorCode.BUDGET_EXHAUSTED, "SQL retry budget exhausted")
        return "repair"

    async def execution(self, state):
        if state["execution_count"] >= self.config.max_executions:
            raise SQLAgentError(ErrorCode.BUDGET_EXHAUSTED, "SQL execution budget exhausted")
        query = self.query(state)
        await self.progress(WorkflowStage.EXECUTION)
        observer = observer_context.get()
        call_id = UUID(state["tool_call_id"]) if state.get("tool_call_id") else None
        if observer is not None:
            await observer.sql_executing(call_id)
        try:
            with self.database_factory(self.effective_access(state)) as database:
                result = await database.execute_query(query)
        except SQLAgentError as exc:
            if observer is not None:
                await observer.sql_failed(call_id, "execution", exc.code)
            return {
                "execution_count": state["execution_count"] + 1,
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "repairable": exc.repairable,
                },
            }
        display = PublicCatalogDisplayRedaction(CHINOOK_PUBLIC_CATALOG_TABLES)
        display.query(query)
        safe = display.result(result)
        if observer is not None:
            await observer.sql_completed(call_id, safe)
        return {
            "result_ciphertext": self.seal(result.model_dump()),
            "safe_result": safe.model_dump(),
            "execution_count": state["execution_count"] + 1,
            "error": None,
        }

    async def repair(self, state):
        await self.progress(WorkflowStage.REPAIR)
        output: GeneratedSQL = await self.invoke(
            "repair",
            state,
            parsed_query=self.parsed(state).model_dump(),
            schema=state["selected_schema"],
            previous_query=self.query(state).model_dump(),
            error=state["error"],
            allowed_functions=sorted(self.access.allowed_functions),
        )
        return {
            "query_ciphertext": self.seal(output.as_query().model_dump()),
            "repair_count": state["repair_count"] + 1,
            "error": None,
        }

    async def disclosure_review(self, state):
        if self.result(state).model_dump() == state["safe_result"]:
            return {"pending": None}
        decision: PrivacyDecision = await self.invoke(
            "disclosure_review", state, result=self.bounded_result(state["safe_result"])[0]
        )
        if not decision.request_permission:
            return {"pending": None}
        return {
            "pending": {
                "kind": "result_disclosure",
                "reason": "问题需要已遮盖的结果字段。授权后，这些字段会发送给模型并返回前端。"
                "授权仅适用于本次运行。",
                "tables": [],
                "columns": {"result": state["safe_result"]["columns"]},
                "scope": "current_run",
            }
        }

    async def disclosure_approval(self, state):
        approved = interrupt(state["pending"])
        return {"pending": None, "disclosure_approved": approved is True}

    @staticmethod
    def bounded_result(result):
        payload = {
            **result,
            "rows": list(result["rows"][:20]),
            "returned_row_count": len(result["rows"]),
        }
        while payload["rows"] and len(json.dumps(payload).encode("utf-8")) > 32000:
            payload["rows"].pop()
        omitted = len(payload["rows"]) < len(result["rows"])
        payload["summary_rows_omitted"] = omitted
        return payload, omitted

    async def summary(self, state):
        await self.progress(WorkflowStage.SYNTHESIS)
        raw_result = self.result(state)
        result = (
            raw_result
            if state.get("disclosure_approved")
            else QueryResult.model_validate(state["safe_result"])
        )
        payload, omitted = self.bounded_result(result.model_dump())
        warnings = []
        try:
            if result.rows and not payload["rows"]:
                answer = basic_answer(result)
            else:
                output = await self.invoke("summary", state, result=payload)
                answer = output.answer
                if len(answer) > MAX_ANSWER_CHARACTERS:
                    raise SQLAgentError(ErrorCode.MODEL_OUTPUT, "Summary exceeded output budget")
                if omitted:
                    answer += SAMPLE_ANSWER
                if result.truncated:
                    answer += PARTIAL_ANSWER
        except (SQLAgentError, ValueError):
            answer = basic_answer(result)
            warnings.append(SUMMARY_FALLBACK_WARNING)
        query = self.query(state)
        if not state.get("disclosure_approved"):
            display = PublicCatalogDisplayRedaction(CHINOOK_PUBLIC_CATALOG_TABLES)
            safe_query = display.query(query)
            display.result(raw_result)
            answer = display.text(answer)
            query = SQLQuery(sql=safe_query.sql)
        if result.truncated:
            warnings.append("查询结果已截断，仅返回预算范围内的数据。")
        return {
            "response_ciphertext": self.seal(
                {
                    "request_id": state["request_id"],
                    "status": "success",
                    "answer": answer,
                    "query": query.model_dump(),
                    "result": result.model_dump(),
                    "warnings": warnings,
                }
            )
        }

    @staticmethod
    def failure(state, code, message):
        return {
            "request_id": state["request_id"],
            "status": "failed",
            "error": {"code": code, "message": message, "repairable": False},
        }


def today_iso():
    return date.today().isoformat()
