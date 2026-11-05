"""Application composition root backed by the LangChain runner."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from cryptography.fernet import Fernet
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from chatbi.settings import ModelConfig, load_model_config
from chatbi.sql_agent.config import (
    AccessConfig,
    DatabaseConfig,
    ExecutionConfig,
    WorkflowConfig,
)
from chatbi.sql_agent.database import DatabaseService, SQLiteAdapter
from chatbi.sql_agent_langchain.agents import LangChainAgents
from chatbi.sql_agent_langchain.graph import SQLAgentGraph
from chatbi.sql_agent_langchain.runtime import LangChainRunner
from chatbi.sql_agent_langchain.service import LangChainSQLAgentService


@asynccontextmanager
async def open_sql_agent_langchain(
    project_root: Path,
    *,
    model_config: ModelConfig | None = None,
    execution_config: ExecutionConfig | None = None,
    workflow_config: WorkflowConfig | None = None,
    access_config: AccessConfig | None = None,
    approval_access_config: AccessConfig | None = None,
    checkpoint_path: Path | None = None,
    checkpoint_encryption_key: str | None = None,
) -> AsyncIterator[LangChainSQLAgentService]:
    """Compose native agents and a durable graph, preserving the OpenAI backend.

    approval_access_config is a separate administrator-defined delegation ceiling.
    None means denied database resources can never be granted by a user.
    """
    model = model_config if model_config is not None else load_model_config(project_root)
    execution = execution_config if execution_config is not None else ExecutionConfig()
    workflow_config = workflow_config if workflow_config is not None else WorkflowConfig()
    access = access_config if access_config is not None else AccessConfig()
    key = checkpoint_encryption_key or os.getenv("CHATBI_LANGCHAIN_CHECKPOINT_KEY")
    encryption_key = key.encode("ascii") if key else Fernet.generate_key()
    Fernet(encryption_key)  # Fail at startup if the configured key is invalid.
    adapter = SQLiteAdapter(DatabaseConfig(project_root=project_root), execution, access)
    with DatabaseService(adapter) as database:
        async with LangChainRunner(model) as runner:
            path = checkpoint_path or project_root / ".chatbi" / "langchain-checkpoints.sqlite"
            path.parent.mkdir(parents=True, exist_ok=True)
            async with AsyncSqliteSaver.from_conn_string(str(path)) as checkpointer:
                await checkpointer.setup()
                workflow = SQLAgentGraph(
                    agents=LangChainAgents(runner.model, model.timeout_seconds),
                    database=database,
                    database_factory=lambda policy: DatabaseService(
                        SQLiteAdapter(DatabaseConfig(project_root=project_root), execution, policy)
                    ),
                    access=access,
                    approval_access=approval_access_config,
                    config=workflow_config,
                    checkpointer=checkpointer,
                    encryption_key=encryption_key,
                )
                yield LangChainSQLAgentService(workflow, workflow_config)
