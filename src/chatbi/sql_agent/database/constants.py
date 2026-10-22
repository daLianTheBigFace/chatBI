"""SQLite connection settings and stable diagnostic messages."""

SQLITE_DRIVER = "sqlite+pysqlite"
READ_ONLY_URI_SUFFIX = "?mode=ro"
SQLITE_URI_PARAMETER = "uri"
SQLITE_URI_ENABLED = "true"
QUERY_ONLY_PRAGMA = "PRAGMA query_only = ON"
EXTENSION_LOADING_ENABLED = False
ROW_LOOKAHEAD = 1
MILLISECONDS_PER_SECOND = 1000
BINARY_PREFIX = "hex:"

DATABASE_NOT_FOUND = "Database file does not exist or is not a regular file"
DATABASE_OPEN_FAILED = "Unable to open the SQLite database in read-only mode"
DATABASE_READ_FAILED = "Unable to read database metadata or sample data"
DATABASE_CLOSED = "Database adapter has been closed"
TABLE_NOT_ALLOWED = "Table is not available in the database schema"
QUERY_FAILED = "SQLite could not execute the query"
QUERY_DENIED = "Query was rejected by the SQLite access policy"
QUERY_TIMEOUT = "Query exceeded its execution budget or was cancelled"
RESULT_TOO_LARGE = "Query exceeded the configured result budget"
MAIN_DATABASE = "main"
MIN_SQLITE_VALUE_LIMIT = 65_536
MAX_SQLITE_COLUMNS = 256
MAX_SQLITE_EXPRESSION_DEPTH = 100
EXPLAIN_PREFIX = "EXPLAIN QUERY PLAN "
SCHEMA_PRELOAD_SQL = "SELECT name FROM sqlite_master LIMIT 0"
TABLE_LIST_SQL = "PRAGMA main.table_list"
ORDINARY_TABLE_TYPE = "table"
RESULT_SERIALIZATION_FAILED = "Query result cannot be serialized to finite JSON values"
