"""Named LangChain agents; orchestration lives in the parent LangGraph."""

import asyncio
import json

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage, HumanMessage
from openai import OpenAIError
from pydantic import BaseModel, ValidationError

from chatbi.sql_agent.agent_models import GeneratedSQL, ParsedQuery
from chatbi.sql_agent.enums import ErrorCode
from chatbi.sql_agent.errors import SQLAgentError
from chatbi.sql_agent.prompts import GENERATION_PROMPT, REPAIR_PROMPT, UNDERSTANDING_PROMPT
from chatbi.sql_agent.summary import SUMMARY_PROMPT, SummaryAnswer
from chatbi.sql_agent_langchain.contracts import (
    ACCESS_PROMPT,
    DISCLOSURE_PROMPT,
    PrivacyDecision,
)


class LangChainAgents:
    """Each specialist has a native agent graph and isolated message history."""

    def __init__(self, model, timeout_seconds: float):
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._agents = {}
        self.specifications = {
            "understanding": (UNDERSTANDING_PROMPT, ParsedQuery),
            "generation": (GENERATION_PROMPT, GeneratedSQL),
            "repair": (REPAIR_PROMPT, GeneratedSQL),
            "summary": (SUMMARY_PROMPT, SummaryAnswer),
            "access_review": (ACCESS_PROMPT, PrivacyDecision),
            "disclosure_review": (DISCLOSURE_PROMPT, PrivacyDecision),
        }

    async def invoke(self, name: str, payload: dict, history: list[dict]) -> BaseModel:
        prompt, schema = self.specifications[name]
        if name not in self._agents:
            self._agents[name] = create_agent(
                model=self.model,
                name=name,
                system_prompt=prompt,
                response_format=ToolStrategy(schema, handle_errors=False),
            )
        messages = [
            (HumanMessage if item["role"] == "user" else AIMessage)(content=item["content"])
            for item in history
        ]
        messages.append(HumanMessage(content=json.dumps(payload, ensure_ascii=False)))
        try:
            async with asyncio.timeout(self.timeout_seconds):
                output = await self._agents[name].ainvoke(
                    {"messages": messages}, {"recursion_limit": 6}
                )
            return schema.model_validate(output["structured_response"])
        except TimeoutError:
            raise SQLAgentError(
                ErrorCode.TIMEOUT, "Model request exceeded its time budget"
            ) from None
        except OpenAIError:
            raise SQLAgentError(ErrorCode.MODEL_CALL, "Model request failed") from None
        except (ValidationError, ValueError, KeyError):
            raise SQLAgentError(ErrorCode.MODEL_OUTPUT, "Invalid agent response") from None
