"""Incremental masking stays safe for every split, including single characters."""

import pytest

from chatbi.sql_agent.models import SQLQuery
from chatbi.sql_agent.redaction import REDACTED, ConservativeDisplayRedaction

PRIVATE_CASES = [
    ("alice@example.com", ""),
    ("用户@example.com", ""),
    ("13812345678", ""),
    ("+86 13812345678", ""),
    ("Bearer abc.def-secret", ""),
    ("sk-long-secret", ""),
    ("password = abcdef", ""),
    ("API_KEY: abcdef", ""),
    ("token=中文秘密", ""),
    ("Alice Smith", "Alice Smith"),
    ("张 三", "张 三"),
    ("私有，多段。内容", "私有，多段。内容"),
]


@pytest.mark.parametrize(("secret", "known"), PRIVATE_CASES)
def test_private_tokens_never_leak_across_any_chunk_boundary(secret, known):
    text = f"结果：{secret}；完成。\n"
    partitions = [[text[:index], text[index:]] for index in range(1, len(text))]
    partitions.append(list(text))
    for chunks in partitions:
        display = ConservativeDisplayRedaction()
        if known:
            display.query(SQLQuery(sql="SELECT :value", parameters={"value": known}))
        buffer = output = ""
        for chunk in chunks:
            safe, buffer = display.stream_prefix(buffer + chunk)
            output += safe
            # A completed private token must never appear in an intermediate view.
            assert secret not in output
        output += display.text(buffer)
        assert output == display.text(text)
        assert REDACTED in output


def test_non_newline_paragraph_releases_each_clause_without_waiting_for_final_answer():
    display = ConservativeDisplayRedaction()
    buffer = ""
    for chunk in ["共查询", "到五首歌曲，", "以下是", "查询结果。"]:
        safe, buffer = display.stream_prefix(buffer + chunk)
        if chunk.endswith(("，", "。")):
            assert safe and not buffer


def test_known_value_prefix_is_retained_until_it_can_be_masked():
    display = ConservativeDisplayRedaction()
    display.query(SQLQuery(sql="SELECT :value", parameters={"value": "Alice Smith"}))
    safe, pending = display.stream_prefix("结果：Alice ")
    assert safe == "结果：" and pending == "Alice "
    safe, pending = display.stream_prefix(pending + "Smith；完成。")
    assert safe == REDACTED + "；完成。" and not pending
