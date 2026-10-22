"""Bounded in-memory conversation for one Streamlit browser session."""

from dataclasses import dataclass, field

from chatbi.sql_agent.enums import ResponseStatus
from chatbi.sql_agent.events import AnswerCompleted, ChatEvent, RunCompleted
from chatbi.sql_agent.models import (
    MAX_HISTORY_CHARACTERS,
    MAX_HISTORY_MESSAGE_CHARACTERS,
    MAX_HISTORY_MESSAGES,
    HistoryMessage,
)

MAX_VISIBLE_TURNS = 10


@dataclass
class ConversationTurn:
    question: str
    events: list[ChatEvent] = field(default_factory=list)
    error: str | None = None

    def context(self) -> list[HistoryMessage]:
        terminal = next(
            (e.payload for e in reversed(self.events) if isinstance(e.payload, RunCompleted)), None
        )
        if terminal is None or terminal.status == ResponseStatus.FAILED:
            return []
        answer = next(
            (
                e.payload.text
                for e in reversed(self.events)
                if isinstance(e.payload, AnswerCompleted)
            ),
            "\n".join(terminal.clarification_questions),
        )
        if not answer.strip():
            return []
        return [
            HistoryMessage(role="user", content=self.question),
            HistoryMessage(role="assistant", content=answer[:MAX_HISTORY_MESSAGE_CHARACTERS]),
        ]


def conversation_history(turns: list[ConversationTurn]) -> list[HistoryMessage]:
    pairs = []
    characters = messages = 0
    for turn in reversed(turns):
        pair = turn.context()
        size = sum(len(item.content) for item in pair)
        if (
            characters + size > MAX_HISTORY_CHARACTERS
            or messages + len(pair) > MAX_HISTORY_MESSAGES
        ):
            break
        if pair:
            pairs.append(pair)
            characters += size
            messages += len(pair)
    return [message for pair in reversed(pairs) for message in pair]
