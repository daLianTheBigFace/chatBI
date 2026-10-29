"""LangChain-backed model runner satisfying the shared agent contracts."""

import asyncio
from collections.abc import AsyncIterator
from typing import Self

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel, ValidationError

from chatbi.settings import ModelConfig
from chatbi.sql_agent.enums import ErrorCode
from chatbi.sql_agent.errors import SQLAgentError

MODEL_CALL_FAILED = "Model request failed; check endpoint compatibility and model configuration"
MODEL_TIMEOUT = "Model request exceeded its time budget"
MODEL_OUTPUT_INVALID = "Model returned an invalid structured output"
RUNTIME_CLOSED = "Model runtime has been closed"


class LangChainRunner:
    """Fulfils ``StructuredAgentRunner`` and ``StreamingTextRunner`` with langchain-openai.

    DeepSeek speaks the OpenAI chat-completions protocol, so ``ChatOpenAI`` drives it
    through an explicitly owned ``AsyncOpenAI`` client. The ``name`` and ``max_turns``
    knobs belong to the openai-agents runner; langchain performs a single structured
    request per stage, which is the equivalent of ``max_turns=1``. They are accepted
    for contract compatibility and ignored.
    """

    def __init__(self, config: ModelConfig) -> None:
        self._config = config
        self._closed = False
        self._client = AsyncOpenAI(
            api_key=config.api_key.get_secret_value(),
            base_url=str(config.base_url),
            timeout=config.timeout_seconds,
            max_retries=config.max_retries,
        )
        self._model = ChatOpenAI(
            model=config.model,
            api_key=config.api_key.get_secret_value(),
            async_client=self._client,
            reasoning_effort=config.reasoning_effort,
        )

    async def run[T: BaseModel](
        self, *, name: str, instructions: str, user_input: str, output_type: type[T]
    ) -> T:
        del name  # single-request structured output has no agent loop to name
        if self._closed:
            raise SQLAgentError(ErrorCode.CONFIGURATION, RUNTIME_CLOSED)
        # DeepSeek supports function calling but not OpenAI's json_schema response
        # format, which is ChatOpenAI's default; request tool calling explicitly.
        structured = self._model.with_structured_output(output_type, method="function_calling")
        messages = [SystemMessage(content=instructions), HumanMessage(content=user_input)]
        try:
            async with asyncio.timeout(self._config.timeout_seconds):
                output = await structured.ainvoke(messages)
            if not isinstance(output, output_type):
                raise SQLAgentError(ErrorCode.MODEL_OUTPUT, MODEL_OUTPUT_INVALID)
            # Revalidate even preconstructed model instances supplied by the parser.
            return output_type.model_validate(output.model_dump())
        except TimeoutError:
            raise SQLAgentError(ErrorCode.TIMEOUT, MODEL_TIMEOUT) from None
        except (ValidationError, ValueError):
            raise SQLAgentError(ErrorCode.MODEL_OUTPUT, MODEL_OUTPUT_INVALID) from None
        except OpenAIError:
            raise SQLAgentError(ErrorCode.MODEL_CALL, MODEL_CALL_FAILED) from None

    async def stream_text(
        self, *, name: str, instructions: str, user_input: str
    ) -> AsyncIterator[str]:
        del name
        if self._closed:
            raise SQLAgentError(ErrorCode.CONFIGURATION, RUNTIME_CLOSED)
        messages = [SystemMessage(content=instructions), HumanMessage(content=user_input)]
        try:
            async with asyncio.timeout(self._config.timeout_seconds):
                async for chunk in self._model.astream(messages):
                    content = chunk.content
                    if isinstance(content, str) and content:
                        yield content
        except TimeoutError:
            raise SQLAgentError(ErrorCode.TIMEOUT, MODEL_TIMEOUT) from None
        except (ValidationError, ValueError):
            raise SQLAgentError(ErrorCode.MODEL_OUTPUT, MODEL_OUTPUT_INVALID) from None
        except OpenAIError:
            raise SQLAgentError(ErrorCode.MODEL_CALL, MODEL_CALL_FAILED) from None

    async def close(self) -> None:
        if not self._closed:
            await self._client.close()
            self._closed = True

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
