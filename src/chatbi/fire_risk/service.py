"""Corridor fire-safety assessment backed by the DeepSeek vision model."""

import base64
import json
from typing import Any, Self

from openai import AsyncOpenAI, OpenAIError
from pydantic import ValidationError

from chatbi.fire_risk.models import FireRiskAssessment
from chatbi.fire_risk.prompts import SYSTEM_PROMPT, TOOL_NAME, TOOL_SCHEMA
from chatbi.settings import ModelConfig

INVALID_OUTPUT = "视觉模型未能返回有效的风险判断"
MODEL_CALL_FAILED = "视觉模型调用失败"


class FireRiskError(Exception):
    """Safe, displayable failure; never leaks endpoint or payload details."""


class FireRiskService:
    """Runs one image through the vision model and returns a validated verdict."""

    def __init__(self, config: ModelConfig) -> None:
        self._client = AsyncOpenAI(
            api_key=config.api_key.get_secret_value(),
            base_url=str(config.base_url),
            timeout=config.timeout_seconds,
            max_retries=config.max_retries,
        )
        self._model = config.model

    async def assess(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> FireRiskAssessment:
        data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode()}"
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": {"url": data_url}}],
                    },
                ],
                tools=[TOOL_SCHEMA],
                tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
                # deepseek-flash defaults to thinking mode, which rejects a forced
                # tool_choice (HTTP 400); disabling reasoning keeps the structured call.
                reasoning_effort="none",
            )
        except OpenAIError as exc:
            raise FireRiskError(MODEL_CALL_FAILED) from exc
        return self._parse(response)

    @staticmethod
    def _parse(response: Any) -> FireRiskAssessment:
        message = response.choices[0].message
        tool_calls = message.tool_calls or []
        if not tool_calls:
            raise FireRiskError(INVALID_OUTPUT)
        try:
            arguments = json.loads(tool_calls[0].function.arguments)
        except (json.JSONDecodeError, AttributeError) as exc:
            raise FireRiskError(INVALID_OUTPUT) from exc
        try:
            return FireRiskAssessment.model_validate(arguments)
        except ValidationError as exc:
            raise FireRiskError(INVALID_OUTPUT) from exc

    async def close(self) -> None:
        await self._client.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
