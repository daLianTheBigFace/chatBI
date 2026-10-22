"""Read-only connection and metadata behavior against SQLite and Chinook."""

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy.exc import DBAPIError

from chatbi.sql_agent.config import DatabaseConfig, ExecutionConfig
from chatbi.sql_agent.database import DatabaseService, SQLiteAdapter
from chatbi.sql_agent.enums import ErrorCode
from chatbi.sql_agent.errors import SQLAgentError

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def database_path(tmp_path):
    # URI metacharacters must not be interpreted as connection options.
    path = tmp_path / "sample #数据.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            "CREATE TABLE artist (id INTEGER PRIMARY KEY, name TEXT, payload BLOB);"
            "CREATE TABLE album (id INTEGER PRIMARY KEY, artist_id INTEGER REFERENCES artist(id));"
            "INSERT INTO artist VALUES (1, '  Alice  ', X'00FF');"
            "INSERT INTO artist VALUES (2, NULL, NULL);"
            "INSERT INTO artist VALUES (3, 'Carol', NULL);"
        )
    return path


def make_adapter(path, **settings):
    return SQLiteAdapter(
        DatabaseConfig(project_root=path.parent, path=path), ExecutionConfig(**settings)
    )


def test_schema_includes_columns_primary_keys_and_relationships(database_path):
    with DatabaseService(make_adapter(database_path)) as service:
        tables = {table.name: table for table in service.get_schema().tables}
        assert set(tables) == {"album", "artist"}
        assert tables["artist"].columns[0].primary_key
        assert tables["album"].foreign_keys[0].referred_table == "artist"
        assert tables["album"].foreign_keys[0].columns == ["artist_id"]


def test_sample_is_bounded_and_preserves_null_and_whitespace(database_path):
    with DatabaseService(make_adapter(database_path, sample_rows=2)) as service:
        result = service.get_table_sample("artist")
        assert result.rows == [[1, "  Alice  ", "hex:00ff"], [2, None, None]]
        assert result.returned_row_count == 2
        assert result.truncated
        assert not service.get_table_sample("album").truncated


def test_sample_respects_max_rows(database_path):
    with DatabaseService(make_adapter(database_path, sample_rows=5, max_rows=1)) as service:
        assert service.get_table_sample("artist").returned_row_count == 1


@pytest.mark.parametrize("table", ["sqlite_master", "artist; DROP TABLE artist", "missing"])
def test_unavailable_table_is_rejected(database_path, table):
    with DatabaseService(make_adapter(database_path)) as service:
        with pytest.raises(SQLAgentError) as error:
            service.get_table_sample(table)
        assert error.value.code == ErrorCode.ACCESS_DENIED


def test_native_connection_stays_readonly_after_query_only_disabled(database_path):
    adapter = make_adapter(database_path)
    try:
        # Private engine access is intentional here to verify the native protection,
        # independently of application validation and query_only.
        with adapter._engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA query_only = OFF")
            with pytest.raises(DBAPIError):
                connection.exec_driver_sql("INSERT INTO artist (id) VALUES (4)")
    finally:
        adapter.close()
    with closing(sqlite3.connect(database_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM artist").fetchone()[0] == 3


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(SQLAgentError) as error:
        make_adapter(path)
    assert error.value.code == ErrorCode.CONFIGURATION
    assert not path.exists()


def test_invalid_database_is_reported_as_configuration_error(tmp_path):
    path = tmp_path / "invalid.db"
    path.write_text("not a database")
    with pytest.raises(SQLAgentError) as error:
        make_adapter(path)
    assert error.value.code == ErrorCode.CONFIGURATION


def test_closed_adapter_cannot_reopen_implicitly(database_path):
    adapter = make_adapter(database_path)
    adapter.close()
    adapter.close()
    with pytest.raises(SQLAgentError):
        adapter.get_schema()
    # NullPool and context managers release file handles on Windows.
    database_path.rename(database_path.with_suffix(".closed"))


def test_chinook_metadata_and_sample_do_not_change_database():
    path = PROJECT_ROOT / "data" / "chinook.db"
    before = hashlib.sha256(path.read_bytes()).digest()
    adapter = SQLiteAdapter(DatabaseConfig(project_root=PROJECT_ROOT), ExecutionConfig())
    with DatabaseService(adapter) as service:
        schema = service.get_schema()
        assert schema.tables
        table = next(
            table for table in schema.tables if table.name.lower() in {"artist", "artists"}
        )
        result = service.get_table_sample(table.name)
        assert result.returned_row_count > 0
        assert result.returned_row_count <= ExecutionConfig().sample_rows
    assert hashlib.sha256(path.read_bytes()).digest() == before
