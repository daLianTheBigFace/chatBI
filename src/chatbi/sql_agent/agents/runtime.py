"""Explicit Responses client; no global SDK configuration or import-time API calls."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Self

from agents import Agent, OpenAIResponsesModel, RunConfig, Runner
from agents.exceptions import AgentsException, ModelBehaviorError
from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel, ValidationError

from chatbi.settings import ModelConfig
from chatbi.sql_agent.enums import ErrorCode
from chatbi.sql_agent.errors import SQLAgentError

MODEL_CALL_FAILED = "Model request failed; check endpoint compatibility and model configuration"
MODEL_TIMEOUT = "Model request exceeded its time budget"
MODEL_OUTPUT_INVALID = "Model returned an invalid structured output"
RUNTIME_CLOSED = "Model runtime has been closed"
TRACING_DISABLED = True
TRACE_INCLUDE_SENSITIVE_DATA = False


class AgentsSDKRunner:
    def __init__(self, config: ModelConfig) -> None:
        self._config = config
        self._closed = False
        self._client = AsyncOpenAI(
            api_key=config.api_key.get_secret_value(),
            base_url=str(config.base_url),
            timeout=config.timeout_seconds,
            max_retries=config.max_retries,
        )
        self._model = OpenAIResponsesModel(model=config.model, openai_client=self._client)

    async def run[T: BaseModel](
        self, *, name: str, instructions: str, user_input: str, output_type: type[T]
    ) -> T:
        if self._closed:
            raise SQLAgentError(ErrorCode.CONFIGURATION, RUNTIME_CLOSED)
        agent = Agent(
            name=name, instructions=instructions, model=self._model, output_type=output_type
        )
        try:
            async with asyncio.timeout(self._config.timeout_seconds):
                result = await Runner.run(
                    agent,
                    input=user_input,
                    max_turns=self._config.max_turns,
                    run_config=RunConfig(
                        tracing_disabled=TRACING_DISABLED,
                        trace_include_sensitive_data=TRACE_INCLUDE_SENSITIVE_DATA,
                    ),
                )
            output = result.final_output
            if not isinstance(output, output_type):
                raise SQLAgentError(ErrorCode.MODEL_OUTPUT, MODEL_OUTPUT_INVALID)
            # Revalidate even preconstructed model instances supplied by an SDK/custom runner.
            return output_type.model_validate(output.model_dump())
        except TimeoutError:
            raise SQLAgentError(ErrorCode.TIMEOUT, MODEL_TIMEOUT) from None
        except (ValidationError, ModelBehaviorError):
            raise SQLAgentError(ErrorCode.MODEL_OUTPUT, MODEL_OUTPUT_INVALID) from None
        except (OpenAIError, AgentsException):
            raise SQLAgentError(ErrorCode.MODEL_CALL, MODEL_CALL_FAILED) from None

    async def stream_text(
        self, *, name: str, instructions: str, user_input: str
    ) -> AsyncIterator[str]:
        if self._closed:
            raise SQLAgentError(ErrorCode.CONFIGURATION, RUNTIME_CLOSED)
        agent = Agent(name=name, instructions=instructions, model=self._model)
        try:
            async with asyncio.timeout(self._config.timeout_seconds):
                result = Runner.run_streamed(
                    agent,
                    input=user_input,
                    max_turns=self._config.max_turns,
                    run_config=RunConfig(
                        tracing_disabled=TRACING_DISABLED,
                        trace_include_sensitive_data=TRACE_INCLUDE_SENSITIVE_DATA,
                    ),
                )
                async with aclosing(result.stream_events()) as events:
                    try:
                        async for event in events:
                            if (
                                event.type == "raw_response_event"
                                and event.data.type == "response.output_text.delta"
                            ):
                                yield event.data.delta
                    finally:
                        if not result.is_complete:
                            result.cancel()
        except TimeoutError:
            raise SQLAgentError(ErrorCode.TIMEOUT, MODEL_TIMEOUT) from None
        except (ValidationError, ModelBehaviorError):
            raise SQLAgentError(ErrorCode.MODEL_OUTPUT, MODEL_OUTPUT_INVALID) from None
        except (OpenAIError, AgentsException):
            raise SQLAgentError(ErrorCode.MODEL_CALL, MODEL_CALL_FAILED) from None

    async def close(self) -> None:
        if not self._closed:
            await self._client.close()
            self._closed = True

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
