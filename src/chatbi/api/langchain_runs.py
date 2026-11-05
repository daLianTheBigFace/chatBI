"""Frontend API for durable LangGraph interrupts and continuation."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request

from chatbi.sql_agent.models import SQLAgentRequest
from chatbi.sql_agent_langchain.contracts import ResumeRequest, RunView
from chatbi.sql_agent_langchain.service import LangChainSQLAgentService, RunAccessError

router = APIRouter(prefix="/api/v1/langchain/runs", tags=["LangChain runs"])
RunToken = Annotated[str, Header(alias="X-Run-Token", min_length=32, max_length=256)]


def service_for(request: Request) -> LangChainSQLAgentService:
    service = request.app.state.sql_agent
    if not isinstance(service, LangChainSQLAgentService):
        raise HTTPException(409, "Select the LangChain service factory to use durable runs")
    return service


async def invoke(operation):
    try:
        return await operation
    except RunAccessError as exc:
        raise HTTPException(exc.status_code, exc.detail) from None


@router.post("", response_model=RunView)
async def start_run(body: SQLAgentRequest, request: Request):
    return await invoke(service_for(request).start(body))


@router.get("/{run_id}", response_model=RunView)
async def get_run(run_id: UUID, request: Request, run_token: RunToken):
    return await invoke(service_for(request).get_run(str(run_id), run_token))


@router.post("/{run_id}/resume", response_model=RunView)
async def resume_run(run_id: UUID, body: ResumeRequest, request: Request, run_token: RunToken):
    return await invoke(service_for(request).resume(str(run_id), run_token, body))


@router.post("/{run_id}/cancel", response_model=RunView)
async def cancel_run(run_id: UUID, request: Request, run_token: RunToken):
    return await invoke(service_for(request).cancel(str(run_id), run_token))
