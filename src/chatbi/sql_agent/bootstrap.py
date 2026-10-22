"""Application composition root; reusable later from FastAPI lifespan."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from chatbi.settings import ModelConfig, load_model_config
from chatbi.sql_agent.agents.runtime import AgentsSDKRunner
from chatbi.sql_agent.agents.stages import (
    ExplicitTableSelectionStrategy,
    QueryUnderstandingStage,
    SQLGenerationStage,
    SQLRepairStage,
)
from chatbi.sql_agent.config import (
    CHINOOK_PUBLIC_CATALOG_TABLES,
    AccessConfig,
    DatabaseConfig,
    ExecutionConfig,
    WorkflowConfig,
)
from chatbi.sql_agent.database import DatabaseService, SQLiteAdapter
from chatbi.sql_agent.database.validator import default_validation_policy
from chatbi.sql_agent.redaction import PublicCatalogDisplayRedaction
from chatbi.sql_agent.repair import BudgetedSQLRepairPolicy
from chatbi.sql_agent.service import SQLAgentService
from chatbi.sql_agent.summary import ModelAnswerSynthesisStrategy
from chatbi.sql_agent.workflow import SQLAgentWorkflow


@asynccontextmanager
async def open_sql_agent(
    project_root: Path,
    *,
    model_config: ModelConfig | None = None,
    execution_config: ExecutionConfig | None = None,
    workflow_config: WorkflowConfig | None = None,
    access_config: AccessConfig | None = None,
) -> AsyncIterator[SQLAgentService]:
    model = model_config if model_config is not None else load_model_config(project_root)
    execution = execution_config if execution_config is not None else ExecutionConfig()
    workflow_config = workflow_config if workflow_config is not None else WorkflowConfig()
    access = access_config if access_config is not None else AccessConfig()
    adapter = SQLiteAdapter(DatabaseConfig(project_root=project_root), execution, access)
    with DatabaseService(adapter) as database:
        async with AgentsSDKRunner(model) as runner:
            workflow = SQLAgentWorkflow(
                database=database,
                understanding=QueryUnderstandingStage(runner),
                selection=ExplicitTableSelectionStrategy(),
                generation=SQLGenerationStage(runner, access),
                repair=SQLRepairStage(runner, access),
                validation=default_validation_policy(execution),
                repair_policy=BudgetedSQLRepairPolicy(workflow_config),
                config=workflow_config,
            )
            yield SQLAgentService(
                workflow,
                workflow_config,
                synthesis=ModelAnswerSynthesisStrategy(runner),
                display_factory=partial(
                    PublicCatalogDisplayRedaction, CHINOOK_PUBLIC_CATALOG_TABLES
                ),
            )
