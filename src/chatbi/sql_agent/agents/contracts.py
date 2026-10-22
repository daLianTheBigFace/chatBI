"""Injectable model runner, permitting deterministic tests without network access."""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class StructuredAgentRunner(Protocol):
    async def run[T: BaseModel](
        self, *, name: str, instructions: str, user_input: str, output_type: type[T]
    ) -> T: ...


@runtime_checkable
class StreamingTextRunner(Protocol):
    def stream_text(
        self, *, name: str, instructions: str, user_input: str
    ) -> AsyncIterator[str]: ...
