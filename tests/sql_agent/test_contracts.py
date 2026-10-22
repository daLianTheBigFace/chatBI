"""Contract boundaries that later workflow and database code must preserve."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from chatbi.sql_agent.config import DatabaseConfig, ExecutionConfig, WorkflowConfig
from chatbi.sql_agent.enums import ErrorCode, ResponseStatus
from chatbi.sql_agent.models import (
    QueryResult,
    SQLAgentRequest,
    SQLAgentResponse,
    SQLQuery,
    ValidationIssue,
    ValidationResult,
)


def test_database_path_is_independent_of_working_directory(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    config = DatabaseConfig(project_root=project_root)
    monkeypatch.chdir(tmp_path)
    assert config.resolved_path == project_root / "data" / "chinook.db"


@pytest.mark.parametrize("question", ["", " \n "])
def test_blank_question_is_rejected(question):
    with pytest.raises(ValidationError):
        SQLAgentRequest(question=question)


def test_query_parameters_are_not_shared():
    first = SQLQuery(sql="SELECT :value", parameters={"value": 1})
    second = SQLQuery(sql="SELECT 1")
    first.parameters["value"] = 2
    assert second.parameters == {}


def test_result_preserves_duplicate_columns_and_nulls():
    result = QueryResult(columns=["name", "name"], rows=[["Alice", None]], execution_time_ms=0)
    assert result.returned_row_count == 1
    assert result.rows == [["Alice", None]]
    assert result.model_dump(mode="json")["rows"] == [["Alice", None]]


def test_result_rejects_invalid_row_shape():
    with pytest.raises(ValidationError):
        QueryResult(columns=["name"], rows=[["Alice", "Bob"]], execution_time_ms=0)


def test_database_values_are_not_trimmed():
    value = "  original value  "
    query = SQLQuery(sql="SELECT :value", parameters={"value": value})
    result = QueryResult(columns=["value"], rows=[[value]], execution_time_ms=0)
    assert query.parameters["value"] == value
    assert result.rows[0][0] == value


@pytest.mark.parametrize(
    "factory,kwargs",
    [(ExecutionConfig, {"max_rows": 0}), (WorkflowConfig, {"max_repairs": -1})],
)
def test_invalid_budgets_are_rejected(factory, kwargs):
    with pytest.raises(ValidationError):
        factory(**kwargs)


def test_zero_repairs_is_supported():
    assert WorkflowConfig(max_repairs=0).max_repairs == 0


def test_validation_cannot_claim_success_with_issues():
    result = ValidationResult(
        issues=[ValidationIssue(code=ErrorCode.ACCESS_DENIED, message="Table is not permitted")]
    )
    assert not result.valid


@pytest.mark.parametrize("status", list(ResponseStatus))
def test_response_rejects_missing_status_payload(status):
    with pytest.raises(ValidationError):
        SQLAgentResponse(request_id="request-1", status=status)


def test_empty_query_result_is_success_not_failure():
    response = SQLAgentResponse(
        request_id="request-1",
        status=ResponseStatus.SUCCESS,
        query=SQLQuery(sql="SELECT name FROM Artist WHERE 1 = 0"),
        result=QueryResult(columns=["name"], rows=[], execution_time_ms=0),
    )
    assert response.result.returned_row_count == 0


def test_unknown_configuration_is_rejected():
    with pytest.raises(ValidationError):
        DatabaseConfig(project_root=Path.cwd(), read_only=False)
