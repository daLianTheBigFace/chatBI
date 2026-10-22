"""Conservative display policy, separate from database access permissions."""

import re
from typing import Protocol

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError

from chatbi.sql_agent.events import DisplaySQL
from chatbi.sql_agent.models import QueryResult, SQLQuery

REDACTED = "[REDACTED]"
HIDDEN_SQL = "[SQL unavailable for safe display]"
SQL_LITERAL_TYPES = (exp.Literal, exp.HexString, exp.ByteString, exp.BitString, exp.RawString)
SENSITIVE_COLUMN = re.compile(
    r"name|email|phone|fax|address|postal|password|secret|token|key|birth|"
    r"customer.?id|employee.?id|姓名|邮箱|手机|电话|地址|身份证",
    re.IGNORECASE,
)
PRIVATE_TEXT = re.compile(
    r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|"
    r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)|"
    r"\b(?:sk-|Bearer\s+)[\w.-]+|"
    r"(?i:password|api[_-]?key|secret|token)\s*[:=]\s*[^\s,;]+"
)
# These suffixes may grow into a private match in the next model chunk. Unicode
# words are included because the email policy also accepts Unicode local parts.
PENDING_PRIVATE_TEXT = (
    re.compile(r"[\w.+-]+(?:@[\w.-]*)?\Z"),
    re.compile(r"\bBearer\s+[\w.-]*\Z"),
    re.compile(r"(?i:password|api[_-]?key|secret|token)\s*(?:[:=]\s*[^\s,;]*)?\Z"),
    re.compile(r"(?<!\d)(?:\+?86[- ]?)(?:1[3-9]?\d{0,9})?\Z"),
)


class DisplayRedactionStrategy(Protocol):
    def query(self, query: SQLQuery) -> DisplaySQL: ...
    def result(self, result: QueryResult) -> QueryResult: ...
    def text(self, text: str) -> str: ...
    def stream_prefix(self, text: str) -> tuple[str, str]: ...


class ConservativeDisplayRedaction:
    """Request-local: hide all SQL values and text cells, plus sensitive numeric columns."""

    def __init__(self) -> None:
        self._private_values: set[str] = set()
        self._private_pattern: re.Pattern | None = None

    def _remember(self, value: object) -> None:
        if value is not None and str(value):
            if str(value) not in self._private_values:
                self._private_values.add(str(value))
                self._private_pattern = None

    def query(self, query: SQLQuery) -> DisplaySQL:
        for value in query.parameters.values():
            self._remember(value)
        try:
            tree = parse_one(query.sql, dialect="sqlite")
            for node in list(tree.walk()):
                node.comments = None
                if isinstance(node, SQL_LITERAL_TYPES):
                    if not isinstance(node.parent, exp.Limit | exp.Offset):
                        self._remember(node.this)
                    node.replace(exp.Placeholder())
            sql = tree.sql(dialect="sqlite", comments=False)
        except (SqlglotError, ValueError):
            sql = HIDDEN_SQL
        return DisplaySQL(
            sql=self.text(sql), parameter_names=[self.text(name) for name in query.parameters]
        )

    def result(self, result: QueryResult) -> QueryResult:
        safe = result.model_copy(deep=True)
        for row in safe.rows:
            for index, value in enumerate(row):
                if isinstance(value, str) or SENSITIVE_COLUMN.search(safe.columns[index]):
                    self._remember(value)
                    if value is not None:
                        row[index] = REDACTED
        safe.columns = [self.text(name) for name in safe.columns]
        return safe

    def text(self, text: str) -> str:
        if self._private_values and self._private_pattern is None:
            patterns = []
            for value in sorted(self._private_values, key=len, reverse=True):
                pattern = re.escape(value)
                if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value):
                    pattern = r"(?<![\d.])" + pattern + r"(?![\d.])"
                patterns.append(pattern)
            self._private_pattern = re.compile("|".join(patterns))
        if self._private_pattern is not None:
            text = self._private_pattern.sub(REDACTED, text)
        return PRIVATE_TEXT.sub(REDACTED, text)

    def stream_prefix(self, text: str) -> tuple[str, str]:
        """Return a scrubbed stable prefix and a raw suffix needing more context.

        Never split a known private value or a potential email/key/phone token.
        Unlike line buffering, punctuation and ordinary word boundaries can
        release text immediately, even when the model never emits a newline.
        """
        self.text("")  # Refresh the request's compiled private-value matcher.
        boundary = len(text)
        for pattern in PENDING_PRIVATE_TEXT:
            if match := pattern.search(text):
                boundary = min(boundary, match.start())
        for value in self._private_values:
            for size in range(min(len(value), len(text)), 0, -1):
                if text.endswith(value[:size]):
                    boundary = min(boundary, len(text) - size)
                    break
        # A complete match may straddle the candidate cut (e.g. a multiword
        # private value). Move the cut before that match, never through it.
        patterns = [PRIVATE_TEXT]
        if self._private_pattern is not None:
            patterns.append(self._private_pattern)
        matches = [match for pattern in patterns for match in pattern.finditer(text)]
        while crossing := [
            match.start() for match in matches if match.start() < boundary < match.end()
        ]:
            boundary = min(crossing)
        return self.text(text[:boundary]), text[boundary:]


class PublicCatalogDisplayRedaction(ConservativeDisplayRedaction):
    """Opt-in public catalog text; a single non-public source restores full masking."""

    def __init__(self, public_tables: frozenset[str]) -> None:
        super().__init__()
        self._public_tables = {name.casefold() for name in public_tables}
        self._public_query = False

    def query(self, query: SQLQuery) -> DisplaySQL:
        self._public_query = False
        try:
            tree = parse_one(query.sql, dialect="sqlite")
            sources = list(tree.find_all(exp.Table))
            self._public_query = bool(sources) and all(
                isinstance(table.this, exp.Identifier)
                and table.name.casefold() in self._public_tables
                and not table.db
                and not table.catalog
                for table in sources
            )
        except (SqlglotError, ValueError):
            pass
        return super().query(query)

    def result(self, result: QueryResult) -> QueryResult:
        if not self._public_query:
            return super().result(result)
        safe = result.model_copy(deep=True)
        safe.columns = [self.text(name) for name in safe.columns]
        safe.rows = [
            [self.text(value) if isinstance(value, str) else value for value in row]
            for row in safe.rows
        ]
        return safe
