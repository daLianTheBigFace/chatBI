"""Load model configuration once without mutating process environment variables."""

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    PositiveFloat,
    PositiveInt,
    SecretStr,
    ValidationError,
    field_validator,
)

from chatbi.sql_agent.enums import ErrorCode
from chatbi.sql_agent.errors import SQLAgentError

ENV_FILE_NAME = ".env"
ENV_API_KEY = "OPENAI_API_KEY"
ENV_BASE_URL = "OPENAI_BASE_URL"
ENV_MODEL = "OPENAI_MODEL"
MODEL_ENV_KEYS = (ENV_API_KEY, ENV_BASE_URL, ENV_MODEL)
DEFAULT_MODEL_TIMEOUT_SECONDS = 45.0
DEFAULT_MODEL_MAX_TURNS = 1
DEFAULT_MODEL_MAX_RETRIES = 0
INVALID_MODEL_CONFIG = "Model configuration is missing or invalid; check OPENAI_* settings"


class ModelConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    api_key: SecretStr = Field(repr=False, exclude=True)
    base_url: HttpUrl
    model: str = Field(min_length=1)
    timeout_seconds: PositiveFloat = DEFAULT_MODEL_TIMEOUT_SECONDS
    max_turns: PositiveInt = DEFAULT_MODEL_MAX_TURNS
    max_retries: int = Field(default=DEFAULT_MODEL_MAX_RETRIES, ge=0)

    @field_validator("api_key", mode="before")
    @classmethod
    def check_api_key(cls, value: object) -> object:
        text = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(text, str) or not text.strip():
            raise ValueError(INVALID_MODEL_CONFIG)
        return text.strip()

    @field_validator("model", mode="before")
    @classmethod
    def strip_model(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("base_url")
    @classmethod
    def check_base_url(cls, value: HttpUrl) -> HttpUrl:
        if value.username or value.password or value.query or value.fragment:
            raise ValueError(INVALID_MODEL_CONFIG)
        return value


def load_model_config(
    project_root: Path, *, environment: Mapping[str, str] | None = None
) -> ModelConfig:
    """Explicit environment takes precedence over .env; there is no import-time I/O."""
    try:
        values = dotenv_values(project_root / ENV_FILE_NAME, interpolate=False)
        environ = os.environ if environment is None else environment
        selected = {key: environ.get(key, values.get(key)) for key in MODEL_ENV_KEYS}
        return ModelConfig(
            api_key=selected[ENV_API_KEY],
            base_url=selected[ENV_BASE_URL],
            model=selected[ENV_MODEL],
        )
    except (ValidationError, OSError):
        raise SQLAgentError(ErrorCode.CONFIGURATION, INVALID_MODEL_CONFIG) from None
