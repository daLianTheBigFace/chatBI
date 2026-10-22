"""Public catalog projection must not expose data joined or aliased from private sources."""

import pytest

from chatbi.sql_agent.config import CHINOOK_PUBLIC_CATALOG_TABLES
from chatbi.sql_agent.models import QueryResult, SQLQuery
from chatbi.sql_agent.redaction import REDACTED, PublicCatalogDisplayRedaction


def result():
    return QueryResult(
        columns=["track_name", "artist_name", "album_title", "genre_name", "duration_ms"],
        rows=[["Song", "Artist", "Album", "Rock", 120000]],
        execution_time_ms=1,
    )


def test_public_music_text_survives_and_source_data_stays_unchanged():
    display = PublicCatalogDisplayRedaction(CHINOOK_PUBLIC_CATALOG_TABLES)
    display.query(SQLQuery(sql="SELECT t.Name, a.Title FROM tracks t JOIN albums a USING(AlbumId)"))
    original = result()
    assert display.result(original) == original
    assert display.text("Song by Artist") == "Song by Artist"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT c.Email AS track_name FROM customers c",
        "SELECT t.Name, c.Email FROM tracks t CROSS JOIN customers c",
        "SELECT Name FROM tracks UNION SELECT Email FROM customers",
        "SELECT (SELECT Email FROM customers LIMIT 1) AS track_name FROM tracks",
        "SELECT 'private literal' AS track_name",
        "SELECT Name FROM main.tracks",
    ],
)
def test_private_sources_and_unknown_provenance_restore_masking(sql):
    display = PublicCatalogDisplayRedaction(CHINOOK_PUBLIC_CATALOG_TABLES)
    display.query(SQLQuery(sql=sql))
    assert display.result(result()).rows[0][:4] == [REDACTED] * 4


def test_known_parameters_and_private_patterns_still_mask_in_public_query():
    display = PublicCatalogDisplayRedaction(CHINOOK_PUBLIC_CATALOG_TABLES)
    display.query(
        SQLQuery(sql="SELECT Name FROM tracks WHERE Name=:name", parameters={"name": "Song"})
    )
    data = result()
    data.rows[0][1] = "alice@example.com"
    projected = display.result(data)
    assert projected.rows[0][:2] == [REDACTED, REDACTED]


def test_policy_resets_between_repair_candidates():
    display = PublicCatalogDisplayRedaction(CHINOOK_PUBLIC_CATALOG_TABLES)
    display.query(SQLQuery(sql="SELECT Name FROM tracks"))
    assert display.result(result()).rows[0][0] == "Song"
    display.query(SQLQuery(sql="SELECT Email FROM customers"))
    assert display.result(result()).rows[0][0] == REDACTED


def test_sampling_limit_is_hidden_in_sql_but_not_treated_as_private_answer_data():
    display = PublicCatalogDisplayRedaction(CHINOOK_PUBLIC_CATALOG_TABLES)
    public = display.query(SQLQuery(sql="SELECT Name FROM tracks ORDER BY RANDOM() LIMIT 5"))
    assert "5" not in public.sql
    assert display.text("随机选取 5 首歌曲") == "随机选取 5 首歌曲"
