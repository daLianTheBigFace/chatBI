"""Fire-risk assessor verified without network access."""

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from openai import OpenAIError

from chatbi.api.app import create_app
from chatbi.fire_risk.bootstrap import open_fire_risk_service
from chatbi.fire_risk.models import FireRiskAssessment, RiskLevel
from chatbi.fire_risk.service import FireRiskError, FireRiskService
from chatbi.settings import ModelConfig, load_fire_risk_config


def model_config(**changes):
    return ModelConfig(
        **(
            {
                "api_key": "test-only-secret",
                "base_url": "https://example.test/v1",
                "model": "deepseek-flash",
            }
            | changes
        )
    )


@asynccontextmanager
async def _sql_factory(root):
    """Stand-in SQL runtime so fire-risk endpoint tests never touch model config."""
    yield object()


class FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, name, arguments):
        self.id = "call_1"
        self.function = FakeFunction(name, arguments)


class FakeMessage:
    def __init__(self, tool_calls):
        self.content = None
        self.tool_calls = tool_calls


class FakeChoice:
    def __init__(self, message):
        self.message = message


class FakeResponse:
    def __init__(self, message):
        self.choices = [FakeChoice(message)]


class FakeCompletions:
    def __init__(self):
        self.create = AsyncMock()


class FakeChat:
    def __init__(self):
        self.completions = FakeCompletions()


class FakeClient:
    def __init__(self, **kwargs):
        self.config = kwargs
        self.closed = False
        self.chat = FakeChat()

    async def close(self):
        self.closed = True


def install_client(monkeypatch):
    monkeypatch.setattr("chatbi.fire_risk.service.AsyncOpenAI", FakeClient)


def verdict(arguments: dict) -> FakeResponse:
    return FakeResponse(FakeMessage([FakeToolCall("report_fire_risk", json.dumps(arguments))]))


def valid_arguments() -> dict:
    return {
        "level": "high",
        "reasoning": "楼道内停放电动车并有飞线充电",
        "hazards": ["电动车", "飞线充电"],
    }


# --- FireRiskService -----------------------------------------------------


def test_assess_calls_model_with_image_and_schema(monkeypatch):
    install_client(monkeypatch)
    service = FireRiskService(model_config())
    service._client.chat.completions.create.return_value = verdict(valid_arguments())

    async def scenario():
        await service.assess(b"fake-image", "image/jpeg")

    asyncio.run(scenario())
    kwargs = service._client.chat.completions.create.await_args.kwargs
    assert kwargs["model"] == "deepseek-flash"
    assert kwargs["messages"][0]["role"] == "system"
    user_content = kwargs["messages"][1]["content"]
    assert user_content[0]["type"] == "image_url"
    assert user_content[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert kwargs["tool_choice"] == {"type": "function", "function": {"name": "report_fire_risk"}}
    assert kwargs["tools"][0]["function"]["name"] == "report_fire_risk"


def test_assess_parses_verdict(monkeypatch):
    install_client(monkeypatch)
    service = FireRiskService(model_config())
    service._client.chat.completions.create.return_value = verdict(valid_arguments())

    async def scenario():
        return await service.assess(b"fake-image")

    result = asyncio.run(scenario())
    assert isinstance(result, FireRiskAssessment)
    assert result.level == RiskLevel.HIGH
    assert result.hazards == ["电动车", "飞线充电"]


def test_assess_maps_openai_error(monkeypatch):
    install_client(monkeypatch)
    service = FireRiskService(model_config())
    service._client.chat.completions.create.side_effect = OpenAIError("boom")

    async def scenario():
        await service.assess(b"fake-image")

    with pytest.raises(FireRiskError) as error:
        asyncio.run(scenario())
    assert "boom" not in str(error.value)


def test_assess_rejects_missing_tool_call(monkeypatch):
    install_client(monkeypatch)
    service = FireRiskService(model_config())
    service._client.chat.completions.create.return_value = FakeResponse(FakeMessage([]))

    async def scenario():
        await service.assess(b"fake-image")

    with pytest.raises(FireRiskError):
        asyncio.run(scenario())


def test_assess_rejects_invalid_arguments(monkeypatch):
    install_client(monkeypatch)
    service = FireRiskService(model_config())
    service._client.chat.completions.create.return_value = verdict({})
    # Missing required fields, plus an out-of-enum level.
    service._client.chat.completions.create.return_value = verdict(
        {"level": "unknown", "reasoning": "", "hazards": []}
    )

    async def scenario():
        await service.assess(b"fake-image")

    with pytest.raises(FireRiskError):
        asyncio.run(scenario())


# --- bootstrap -----------------------------------------------------------


def test_bootstrap_yields_none_when_unconfigured(tmp_path, monkeypatch):
    def missing(root):
        raise RuntimeError("no config")

    monkeypatch.setattr("chatbi.fire_risk.bootstrap.load_fire_risk_config", missing)

    async def scenario():
        async with open_fire_risk_service(tmp_path) as service:
            return service

    assert asyncio.run(scenario()) is None


def test_bootstrap_yields_service_when_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "chatbi.fire_risk.bootstrap.load_fire_risk_config", lambda root: model_config()
    )

    class FakeService:
        def __init__(self, config):
            self.config = config
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

    monkeypatch.setattr("chatbi.fire_risk.bootstrap.FireRiskService", FakeService)

    async def scenario():
        async with open_fire_risk_service(tmp_path) as service:
            return service

    service = asyncio.run(scenario())
    assert isinstance(service, FakeService)
    assert service.config.model == "deepseek-flash"
    assert service.closed


# --- create_app endpoint -------------------------------------------------


def test_fire_risk_endpoint_returns_verdict(tmp_path):
    @asynccontextmanager
    async def fire_risk_factory(root):
        class Service:
            async def assess(self, image_bytes, mime_type="image/jpeg"):
                assert image_bytes == b"fake-image"
                assert mime_type == "image/jpeg"
                return FireRiskAssessment(
                    level=RiskLevel.MEDIUM,
                    reasoning="堆积纸箱阻塞通道",
                    hazards=["纸箱"],
                )

            async def close(self):
                pass

        yield Service()

    with TestClient(
        create_app(tmp_path, service_factory=_sql_factory, fire_risk_factory=fire_risk_factory)
    ) as client:
        response = client.post(
            "/api/v1/fire-risk",
            json={
                "image_base64": base64.b64encode(b"fake-image").decode(),
                "mime_type": "image/jpeg",
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["level"] == "medium"
    assert body["level_label"] == "中风险"
    assert body["hazards"] == ["纸箱"]


def test_fire_risk_endpoint_reports_unconfigured(tmp_path):
    @asynccontextmanager
    async def fire_risk_factory(root):
        yield None

    with TestClient(
        create_app(tmp_path, service_factory=_sql_factory, fire_risk_factory=fire_risk_factory)
    ) as client:
        response = client.post(
            "/api/v1/fire-risk",
            json={"image_base64": "aW1hZ2U=", "mime_type": "image/jpeg"},
        )
    assert response.status_code == 503


def test_fire_risk_endpoint_rejects_invalid_base64(tmp_path):
    @asynccontextmanager
    async def fire_risk_factory(root):
        class Service:
            async def assess(self, image_bytes, mime_type="image/jpeg"):
                raise AssertionError("must not be called")

        yield Service()

    with TestClient(
        create_app(tmp_path, service_factory=_sql_factory, fire_risk_factory=fire_risk_factory)
    ) as client:
        response = client.post(
            "/api/v1/fire-risk",
            json={"image_base64": "!!!not-base64!!!", "mime_type": "image/jpeg"},
        )
    assert response.status_code == 422


# --- settings ------------------------------------------------------------


def test_vision_model_defaults_to_flash(monkeypatch, tmp_path):
    base = model_config(model="deepseek-chat")
    monkeypatch.setattr(
        "chatbi.settings.load_model_config", lambda root, environment=None: base
    )
    monkeypatch.setattr("chatbi.settings.dotenv_values", lambda path, interpolate=False: {})
    config = load_fire_risk_config(tmp_path, environment={})
    assert config.model == "deepseek-flash"
    assert config.api_key.get_secret_value() == "test-only-secret"


def test_vision_model_env_override(monkeypatch, tmp_path):
    base = model_config(model="deepseek-chat")
    monkeypatch.setattr(
        "chatbi.settings.load_model_config", lambda root, environment=None: base
    )
    monkeypatch.setattr("chatbi.settings.dotenv_values", lambda path, interpolate=False: {})
    config = load_fire_risk_config(tmp_path, environment={"OPENAI_VISION_MODEL": "my-vision"})
    assert config.model == "my-vision"
