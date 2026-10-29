"""Composition root for the fire-risk capability."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from chatbi.fire_risk.service import FireRiskService
from chatbi.settings import load_fire_risk_config


@asynccontextmanager
async def open_fire_risk_service(
    project_root: Path,
) -> AsyncIterator[FireRiskService | None]:
    """Open the vision-backed risk assessor, or yield ``None`` when unconfigured.

    Fire-risk is optional: when the shared model credentials are absent the app
    still serves the SQL endpoints. Returning ``None`` keeps startup resilient.
    """
    try:
        config = load_fire_risk_config(project_root)
    except Exception:
        yield None
        return
    async with FireRiskService(config) as service:
        yield service
