"""Configuration precedence and secret handling without reading real credentials."""

import os

import pytest

from chatbi.settings import load_model_config
from chatbi.sql_agent.enums import ErrorCode
from chatbi.sql_agent.errors import SQLAgentError

EXAMPLE_ENV = (
    "OPENAI_API_KEY=test-only-secret\n"
    "OPENAI_BASE_URL=https://example.test/v1\n"
    "OPENAI_MODEL=test-model\n"
)


def test_config_loads_from_explicit_project_path_without_mutating_environment(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text(EXAMPLE_ENV)
    monkeypatch.chdir(tmp_path)
    before = dict(os.environ)
    config = load_model_config(project, environment={})
    assert config.model == "test-model"
    assert config.api_key.get_secret_value() == "test-only-secret"
    assert dict(os.environ) == before
    assert "test-only-secret" not in repr(config)
    assert "api_key" not in config.model_dump()


def test_explicit_environment_overrides_dotenv(tmp_path):
    (tmp_path / ".env").write_text(EXAMPLE_ENV)
    config = load_model_config(tmp_path, environment={"OPENAI_MODEL": "override-model"})
    assert config.model == "override-model"


@pytest.mark.parametrize(
    "contents",
    [
        "",
        EXAMPLE_ENV.replace("test-only-secret", "   "),
        EXAMPLE_ENV.replace("test-model", ""),
        EXAMPLE_ENV.replace("https://example.test/v1", "not-a-url"),
        EXAMPLE_ENV.replace("https://example.test/v1", "https://user:secret@example.test/v1"),
        EXAMPLE_ENV.replace("https://example.test/v1", "https://example.test/v1?key=secret"),
    ],
)
def test_invalid_config_has_a_sanitized_error(tmp_path, contents):
    (tmp_path / ".env").write_text(contents)
    with pytest.raises(SQLAgentError) as error:
        load_model_config(tmp_path, environment={})
    assert error.value.code == ErrorCode.CONFIGURATION
    assert "test-only-secret" not in str(error.value)
    assert "https://" not in str(error.value)


def test_missing_env_file_can_use_environment(tmp_path):
    config = load_model_config(
        tmp_path,
        environment={
            "OPENAI_API_KEY": "test-only-secret",
            "OPENAI_BASE_URL": "https://example.test/v1",
            "OPENAI_MODEL": "test-model",
        },
    )
    assert config.model == "test-model"
