"""Capability-protected durable runs; clients never receive checkpoint state."""

import asyncio
import hashlib
import hmac
import json
import secrets
from contextlib import suppress
from time import perf_counter, time
from uuid import uuid4
from weakref import WeakValueDictionary

from langgraph.types import Command

from chatbi.sql_agent.config import WorkflowConfig
from chatbi.sql_agent.enums import ErrorCode, ResponseStatus
from chatbi.sql_agent.errors import SQLAgentError
from chatbi.sql_agent.models import (
    PermissionRequest,
    RunContinuation,
    RunMetadata,
    SQLAgentRequest,
    SQLAgentResponse,
)
from chatbi.sql_agent.service import SQLAgentService
from chatbi.sql_agent_langchain.contracts import ResumeRequest, RunView
from chatbi.sql_agent_langchain.graph import SQLAgentGraph, observer_context, today_iso


class RunAccessError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


class LangChainSQLAgentService(SQLAgentService):
    """The legacy HTTP boundary delegates to this graph, never SQLAgentWorkflow.

    Run tokens are bearer capabilities for the local application, not user IDs.
    A production identity gateway should additionally bind them to its principal.
    SQLite checkpoints support restart recovery; use one application worker.
    """

    def __init__(self, workflow: SQLAgentGraph, config: WorkflowConfig, *, approval_ttl=86400):
        self.workflow = workflow
        self._config = config
        self.approval_ttl = approval_ttl
        self._locks = WeakValueDictionary()
        self._active = {}
        self.policy_hash = hashlib.sha256(
            json.dumps(
                {
                    "access": self.policy_data(workflow.access),
                    "approval": (
                        self.policy_data(workflow.approval_access)
                        if workflow.approval_access
                        else None
                    ),
                    "cipher": workflow.cipher_id,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        # Preserve the established SSE projection and event transport.
        from functools import partial

        from chatbi.sql_agent.config import CHINOOK_PUBLIC_CATALOG_TABLES
        from chatbi.sql_agent.redaction import PublicCatalogDisplayRedaction

        self._display_factory = partial(
            PublicCatalogDisplayRedaction, CHINOOK_PUBLIC_CATALOG_TABLES
        )

    @staticmethod
    def policy_data(policy):
        # Frozenset iteration order changes across Python processes. Canonicalize
        # before hashing so legitimate approvals survive a service restart.
        return {
            "allowed_tables": sorted(t.casefold() for t in policy.allowed_tables)
            if policy.allowed_tables is not None
            else None,
            "allowed_columns": {
                t.casefold(): sorted(c.casefold() for c in columns)
                for t, columns in policy.allowed_columns.items()
            },
            "allowed_functions": sorted(f.casefold() for f in policy.allowed_functions),
        }

    @staticmethod
    def config(run_id):
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 100}

    def lock(self, run_id):
        lock = self._locks.get(run_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[run_id] = lock
        return lock

    async def start(self, request: SQLAgentRequest, *, run_id=None, observer=None):
        request = SQLAgentRequest.model_validate(request.model_dump())
        run_id = run_id or str(uuid4())
        token = secrets.token_urlsafe(32)
        state = {
            "request_id": run_id,
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "request_ciphertext": self.workflow.seal(request.model_dump(mode="json")),
            "reference_date": today_iso(),
            "execution_count": 0,
            "repair_count": 0,
            "access_approved": False,
            "disclosure_approved": False,
            "cancelled": False,
            "elapsed_ms": 0,
            "policy_hash": self.policy_hash,
            "expires_at": time() + self.approval_ttl,
        }
        async with self.lock(run_id):
            await self.drive(run_id, state, observer=observer)
            view = await self.project(run_id)
        view.run_token = token
        return view

    async def authorize(self, run_id, token):
        snapshot = await self.workflow.graph.aget_state(self.config(run_id))
        values = snapshot.values
        actual = hashlib.sha256(token.encode()).hexdigest()
        if not values or not hmac.compare_digest(values.get("token_hash", ""), actual):
            raise RunAccessError(404, "Run not found")
        if values.get("policy_hash") != self.policy_hash:
            raise RunAccessError(409, "Access policy changed; start a new run")
        if values.get("expires_at", 0) < time():
            raise RunAccessError(410, "Run expired; start a new run")
        return snapshot

    async def get_run(self, run_id, token):
        await self.authorize(run_id, token)
        return await self.project(run_id)

    async def resume(self, run_id, token, request: ResumeRequest):
        async with self.lock(run_id):
            snapshot = await self.authorize(run_id, token)
            if snapshot.values.get("response_ciphertext") or snapshot.values.get("cancelled"):
                raise RunAccessError(409, "Run is already terminal")
            interrupts = snapshot.interrupts
            if len(interrupts) != 1 or interrupts[0].id != request.interrupt_id:
                raise RunAccessError(409, "Approval is stale or run is not paused")
            await self.drive(run_id, Command(resume={request.interrupt_id: request.approved}))
            return await self.project(run_id)

    async def cancel(self, run_id, token):
        await self.authorize(run_id, token)
        if task := self._active.get(run_id):
            task.cancel()
        async with self.lock(run_id):
            snapshot = await self.authorize(run_id, token)
            if snapshot.values.get("response_ciphertext"):
                if snapshot.values.get("cancelled"):
                    return await self.project(run_id)
                raise RunAccessError(409, "Run is already terminal")
            await self.workflow.graph.aupdate_state(
                self.config(run_id),
                {
                    "cancelled": True,
                    "pending": None,
                    "response_ciphertext": self.workflow.seal(
                        {
                            "request_id": run_id,
                            "status": "cancelled",
                            "answer": "运行已取消。",
                        }
                    ),
                },
            )
            return await self.project(run_id)

    async def drive(self, run_id, input_value, *, observer=None):
        context_token = observer_context.set(observer)
        self._active[run_id] = asyncio.current_task()
        started = perf_counter()
        try:
            async with asyncio.timeout(self._config.request_timeout_seconds):
                await self.workflow.graph.ainvoke(input_value, self.config(run_id))
        except asyncio.CancelledError:
            # A disconnected caller must not leave an executable orphan checkpoint.
            with suppress(Exception):
                await self.workflow.graph.aupdate_state(
                    self.config(run_id),
                    {
                        "cancelled": True,
                        "response_ciphertext": self.workflow.seal(
                            {
                                "request_id": run_id,
                                "status": "cancelled",
                                "answer": "运行已取消。",
                            }
                        ),
                    },
                )
            raise
        except Exception as exc:
            code = ErrorCode.INTERNAL
            message = "SQL Agent request failed"
            if isinstance(exc, TimeoutError):
                code, message = ErrorCode.TIMEOUT, "SQL Agent request exceeded its time budget"
            elif isinstance(exc, SQLAgentError):
                code, message = exc.code, str(exc)
            await self.workflow.graph.aupdate_state(
                self.config(run_id),
                {
                    "response_ciphertext": self.workflow.seal(
                        self.workflow.failure({"request_id": run_id}, code, message)
                    )
                },
            )
        finally:
            snapshot = await self.workflow.graph.aget_state(self.config(run_id))
            if snapshot.values.get("response_ciphertext"):
                await self.workflow.graph.aupdate_state(
                    self.config(run_id),
                    {
                        "elapsed_ms": snapshot.values.get("elapsed_ms", 0)
                        + (perf_counter() - started) * 1000,
                    },
                )
            self._active.pop(run_id, None)
            observer_context.reset(context_token)

    async def project(self, run_id):
        snapshot = await self.workflow.graph.aget_state(self.config(run_id))
        state = snapshot.values
        metadata = RunMetadata(
            execution_count=state.get("execution_count", 0),
            repair_count=state.get("repair_count", 0),
            elapsed_ms=state.get("elapsed_ms", 0),
        )
        if state.get("response_ciphertext"):
            response = SQLAgentResponse.model_validate(
                self.workflow.open(state["response_ciphertext"])
            )
            response.metadata = metadata
            return RunView(run_id=run_id, status=response.status, response=response)
        if snapshot.interrupts:
            item = snapshot.interrupts[0]
            approval = PermissionRequest(interrupt_id=item.id, **item.value)
            return RunView(run_id=run_id, status="awaiting_approval", approval=approval)
        return RunView(run_id=run_id, status="running")

    async def _ask(
        self, request, *, request_id=None, observer=None, display=None, answer_sink=None
    ):
        view = await self.start(request, run_id=request_id, observer=observer)
        if view.response is not None:
            return view.response
        return SQLAgentResponse(
            request_id=view.run_id,
            status=ResponseStatus.AWAITING_APPROVAL,
            continuation=RunContinuation(
                run_id=view.run_id,
                run_token=view.run_token,
                approval=view.approval,
            ),
        )
