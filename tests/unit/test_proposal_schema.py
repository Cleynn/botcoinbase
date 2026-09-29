"""Strict proposal schema and parser: closed schema, no unknown fields, safe decoding."""

from __future__ import annotations

import copy
import json
import time
from typing import Any

import pytest

from app.proposals import schema
from app.proposals.schema import ProposalRejected, decode_json, max_depth, parse_text, validate

PKG = "123e4567-e89b-12d3-a456-426614174000"
REPORT = "123e4567-e89b-12d3-a456-426614174001"
SHA = "ab" * 32


def good() -> dict[str, Any]:
    return {
        "proposal_version": 1,
        "proposal_id": "prop-2026-001",
        "linked_review_package_id": PKG,
        "linked_review_package_sha256": SHA,
        "category": "strategy",
        "summary": "Require a wider minimum band before the grid is built.",
        "evidence_references": ["data/backtest_runs.jsonl#L3", f"report:{REPORT}", "manifest.json"],
        "assumptions": ["Fees stay at the attested rate.", "The stress scenario stays at 0.6%."],
        "suggested_change": "Raise the minimum band ratio from 0.02 to 0.03 so cells clear fees.",
        "expected_benefit": "Fewer fee-negative cycles in the stress scenario.",
        "risk_tradeoffs": "Fewer trading windows and lower activity overall.",
        "required_validation": "Walk-forward backtest on a frozen snapshot, then a paper run.",
        "rollback_plan": "Revert the parameter through a reviewed release and rerun the backtest.",
        "should_remain_no_trade_until_validated": True,
        "no_profit_guarantee": True,
    }


def rules_of(obj: Any) -> list[str]:
    with pytest.raises(ProposalRejected) as caught:
        validate(obj)
    return caught.value.rules


def text_rules(text: str) -> list[str]:
    with pytest.raises(ProposalRejected) as caught:
        parse_text(text, 6)
    return caught.value.rules


# ------------------------------------------------------------------ the good document
def test_a_complete_proposal_parses_into_typed_fields() -> None:
    parsed = parse_text(json.dumps(good()), 6)
    assert parsed.proposal_id == "prop-2026-001" and parsed.category == "strategy"
    assert parsed.linked_package_id == PKG and parsed.linked_package_sha256 == SHA
    assert len(parsed.evidence) == 3 and len(parsed.assumptions) == 2
    assert parsed.should_remain_no_trade and parsed.no_profit_guarantee
    assert parsed.as_json() == good()  # round-trips exactly


@pytest.mark.parametrize("category", schema.CATEGORIES)
def test_every_category_is_accepted(category: str) -> None:
    doc = good()
    doc["category"] = category
    assert validate(doc).category == category


def test_the_schema_names_exactly_the_fifteen_required_fields() -> None:
    assert set(schema.FIELDS) == set(good())
    assert len(schema.FIELDS) == 15


# ------------------------------------------------------------------ required and unknown fields
@pytest.mark.parametrize("name", schema.FIELDS)
def test_every_field_is_required(name: str) -> None:
    doc = good()
    del doc[name]
    assert f"MISSING_FIELD:{name}" in rules_of(doc)


@pytest.mark.parametrize(
    "extra",
    [
        "apply",
        "command",
        "exec",
        "script",
        "config",
        "sql",
        "url",
        "__proto__",
        "constructor",
        "$schema",
        "extra",
        "Summary",
        "summary ",
        "notes",
        "auto_apply",
        "override_risk",
        "",
    ],
)
def test_unknown_fields_are_rejected(extra: str) -> None:
    doc = good()
    doc[extra] = "x"
    assert "UNKNOWN_FIELD" in rules_of(doc)


def test_unknown_fields_are_rejected_even_with_a_valid_document_otherwise() -> None:
    doc = good()
    doc["apply_automatically"] = True
    assert rules_of(doc) == ["UNKNOWN_FIELD"]


@pytest.mark.parametrize("value", [[], "string", 1, None, [{"a": 1}], True])
def test_the_document_must_be_a_single_object(value: Any) -> None:
    assert rules_of(value) == ["NOT_AN_OBJECT"]


# ------------------------------------------------------------------ types and values
@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("proposal_version", 2, "BAD_VERSION"),
        ("proposal_version", "1", "BAD_VERSION"),
        ("proposal_version", True, "BAD_VERSION"),
        ("proposal_version", 1.0, "BAD_VERSION"),
        ("proposal_version", None, "BAD_VERSION"),
        ("proposal_id", "ab", "BAD_VALUE:proposal_id"),
        ("proposal_id", "has space", "BAD_VALUE:proposal_id"),
        ("proposal_id", "<script>", "BAD_VALUE:proposal_id"),
        ("proposal_id", "x" * 65, "BAD_VALUE:proposal_id"),
        ("proposal_id", 12345, "BAD_VALUE:proposal_id"),
        ("proposal_id", "../etc/passwd", "BAD_VALUE:proposal_id"),
        ("linked_review_package_id", "not-a-uuid", "BAD_VALUE:linked_review_package_id"),
        ("linked_review_package_id", PKG.upper(), "BAD_VALUE:linked_review_package_id"),
        ("linked_review_package_id", PKG + "0", "BAD_VALUE:linked_review_package_id"),
        ("linked_review_package_id", None, "BAD_VALUE:linked_review_package_id"),
        ("linked_review_package_sha256", "short", "BAD_VALUE:linked_review_package_sha256"),
        ("linked_review_package_sha256", "A" * 64, "BAD_VALUE:linked_review_package_sha256"),
        ("linked_review_package_sha256", "g" * 64, "BAD_VALUE:linked_review_package_sha256"),
        ("category", "trading", "BAD_VALUE:category"),
        ("category", "Strategy", "BAD_VALUE:category"),
        ("category", ["strategy"], "BAD_VALUE:category"),
        ("summary", 5, "BAD_TYPE:summary"),
        ("summary", None, "BAD_TYPE:summary"),
        ("summary", ["a"], "BAD_TYPE:summary"),
        ("summary", "short", "TOO_SHORT:summary"),
        ("summary", "   \n\t  ", "TOO_SHORT:summary"),
        ("summary", "x" * 501, "TOO_LONG:summary"),
        ("suggested_change", "x" * 2001, "TOO_LONG:suggested_change"),
        ("suggested_change", "too short", "TOO_SHORT:suggested_change"),
        ("expected_benefit", "x" * 1001, "TOO_LONG:expected_benefit"),
        ("risk_tradeoffs", "", "TOO_SHORT:risk_tradeoffs"),
        ("required_validation", "x" * 1001, "TOO_LONG:required_validation"),
        ("rollback_plan", "x" * 1001, "TOO_LONG:rollback_plan"),
        ("evidence_references", "data/backtest_runs.jsonl", "BAD_TYPE:evidence_references"),
        ("evidence_references", [], "TOO_FEW:evidence_references"),
        ("assumptions", "one", "BAD_TYPE:assumptions"),
        ("assumptions", {"a": 1}, "BAD_TYPE:assumptions"),
        (
            "should_remain_no_trade_until_validated",
            "true",
            "BAD_TYPE:should_remain_no_trade_until_validated",
        ),
        (
            "should_remain_no_trade_until_validated",
            1,
            "BAD_TYPE:should_remain_no_trade_until_validated",
        ),
        ("no_profit_guarantee", "yes", "BAD_TYPE:no_profit_guarantee"),
        ("no_profit_guarantee", None, "BAD_TYPE:no_profit_guarantee"),
    ],
)
def test_bad_types_and_values_are_rejected_with_a_fixed_code(
    field: str, value: Any, code: str
) -> None:
    doc = good()
    doc[field] = value
    assert code in rules_of(doc)


def test_length_limits_are_inclusive() -> None:
    doc = good()
    doc["summary"] = "x" * 500
    doc["suggested_change"] = "y" * 2000
    doc["expected_benefit"] = "z" * 1000
    assert validate(doc)
    doc["summary"] = "x" * 10
    assert validate(doc)


# ------------------------------------------------------------------ evidence references
@pytest.mark.parametrize(
    "ref",
    [
        "data/backtest_runs.jsonl",
        "data/backtest_runs.jsonl#L1",
        "data/audit_summary.jsonl#L999999",
        "csv/summary.csv",
        "csv/timeline.csv#L7",
        "efficiency_summary.json",
        "manifest.json",
        f"report:{REPORT}",
        f"snapshot:{REPORT}",
        f"backtest_run:{REPORT}",
    ],
)
def test_allowed_evidence_forms(ref: str) -> None:
    doc = good()
    doc["evidence_references"] = [ref]
    assert validate(doc).evidence == (ref,)


@pytest.mark.parametrize(
    "ref",
    [
        "../manifest.json",
        "/etc/passwd",
        "data/../../x.jsonl",
        "data/x.jsonl#L0",
        "data/x.jsonl#L1000000",
        "data/x.jsonl#frag",
        "data/UPPER.jsonl",
        "http://evil.example/x",
        "javascript:alert(1)",
        "report:not-a-uuid",
        "reports:" + REPORT,
        f"file:{REPORT}",
        "manifest.json ",
        "manifest.json\n",
        "README.md",
        "CHECKSUMS.sha256",
        "data/a/b.jsonl",
        "",
        " ",
        "<b>x</b>",
        "data/x.jsonl?q=1",
        f"report:{REPORT}#L1",
    ],
)
def test_other_evidence_references_are_rejected(ref: str) -> None:
    doc = good()
    doc["evidence_references"] = [ref]
    assert "BAD_EVIDENCE_REFERENCE" in rules_of(doc)


def test_evidence_must_be_unique_and_bounded() -> None:
    doc = good()
    doc["evidence_references"] = ["manifest.json", "manifest.json"]
    assert "DUPLICATE_EVIDENCE_REFERENCE" in rules_of(doc)
    doc["evidence_references"] = [
        f"data/x_{i:02d}.jsonl".replace("0", "a").replace("1", "b") for i in range(21)
    ]
    assert any(r.startswith("TOO_MANY") for r in rules_of(doc))
    doc["evidence_references"] = [1, 2]
    assert "BAD_EVIDENCE_REFERENCE" in rules_of(doc)


def test_assumptions_are_bounded_and_single_line() -> None:
    doc = good()
    doc["assumptions"] = []
    assert validate(doc).assumptions == ()
    doc["assumptions"] = ["x" * 301]
    assert "BAD_VALUE:assumptions" in rules_of(doc)
    doc["assumptions"] = ["line one\nline two"]
    assert "CONTROL_CHARACTERS:assumptions" in rules_of(doc)
    doc["assumptions"] = ["ok"] * 21
    assert "TOO_MANY:assumptions" in rules_of(doc)
    doc["assumptions"] = [5]
    assert "BAD_VALUE:assumptions" in rules_of(doc)
    doc["assumptions"] = [""]
    assert "BAD_VALUE:assumptions" in rules_of(doc)


# ------------------------------------------------------------------ hidden characters in strings
@pytest.mark.parametrize("ch", ["​", "‮", "⁦", "﻿", "", " ", "\ud800", "\x00", "\x1b", "\x7f"])
def test_hidden_or_control_characters_in_any_string_are_rejected(ch: str) -> None:
    for field in schema.TEXT_FIELDS:
        doc = good()
        doc[field] = f"a perfectly ordinary sentence {ch} with something hidden inside it"
        found = rules_of(doc)
        assert any(
            r.endswith(f":{field}")
            and r.split(":")[0] in {"HIDDEN_CHARACTERS", "CONTROL_CHARACTERS"}
            for r in found
        ), (field, found)


def test_newlines_and_tabs_are_allowed_in_the_long_text_fields_only() -> None:
    doc = good()
    doc["suggested_change"] = (
        "Step one: narrow the band.\n\tStep two: rerun the backtest for the frozen snapshot."
    )
    assert validate(doc)
    doc = good()
    doc["proposal_id"] = "prop\n-001"
    assert "BAD_VALUE:proposal_id" in rules_of(doc)


# ------------------------------------------------------------------ decoding
def test_invalid_json_is_rejected() -> None:
    for text in [
        "{",
        "{'a': 1}",
        '{"a": }',
        '{"a": 1,}',
        "{a: 1}",
        '{"a": 1} {"b": 2}',
        "",
        '{"a": "unterminated}',
        '{"a": 01}',
        '{"a": 1} // comment',
        "/* c */ {}",
    ]:
        assert text_rules(text) == ["JSON_INVALID"], text


def test_duplicate_keys_are_rejected_not_last_wins() -> None:
    body = json.dumps(good())[:-1] + ', "summary": "a second, conflicting summary here"}'
    assert text_rules(body) == ["JSON_DUPLICATE_KEY"]
    assert text_rules('{"a": {"b": 1, "b": 2}}') == ["JSON_DUPLICATE_KEY"]


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_numbers_are_rejected(constant: str) -> None:
    assert text_rules('{"proposal_version": ' + constant + "}") == ["JSON_NON_FINITE_NUMBER"]


def test_deeply_nested_input_is_refused_without_recursion() -> None:
    for depth in (7, 50, 5000, 200000):
        text = '{"a":' * depth + "1" + "}" * depth
        assert text_rules(text) == ["JSON_TOO_DEEP"], depth
    text = "[" * 100000
    assert text_rules(text) == ["JSON_TOO_DEEP"]


def test_depth_counting_ignores_brackets_inside_strings() -> None:
    text = '{"a": "' + "[{" * 5000 + '"}'
    assert max_depth(text, 6) == 1
    assert decode_json(text, 6) == {"a": "[{" * 5000}
    escaped = '{"a": "x\\"[[[[[[[[[[[["}'
    assert max_depth(escaped, 6) == 1


def test_the_configured_depth_limit_is_honoured() -> None:
    assert decode_json('{"a": {"b": {"c": 1}}}', 3) == {"a": {"b": {"c": 1}}}
    assert text_rules_with('{"a": {"b": {"c": {"d": 1}}}}', 3) == ["JSON_TOO_DEEP"]


def text_rules_with(text: str, depth: int) -> list[str]:
    with pytest.raises(ProposalRejected) as caught:
        decode_json(text, depth)
    return caught.value.rules


def test_parsing_is_fast_on_adversarial_input() -> None:
    started = time.perf_counter()
    for text in (
        '{"a": "' + "\\u0041" * 20000 + '"}',
        "{" + '"k":1,' * 1000 + '"z":2}',
        '{"a": ' + "1" * 5000 + "}",
    ):
        try:
            decode_json(text, 6)
        except ProposalRejected:
            pass
    assert time.perf_counter() - started < 1.0


def test_json_strings_cannot_smuggle_types_or_constructors() -> None:
    doc = good()
    doc["summary"] = {"$ref": "file:///etc/passwd"}
    assert "BAD_TYPE:summary" in rules_of(doc)
    doc = good()
    doc["category"] = {"__class__": "x"}
    assert "BAD_VALUE:category" in rules_of(doc)


def test_the_parser_never_returns_anything_but_plain_data() -> None:
    parsed = parse_text(json.dumps(good()), 6)
    for value in parsed.as_json().values():
        assert isinstance(value, str | int | bool | list)
    assert copy.deepcopy(parsed) == parsed


def test_many_errors_are_reported_at_most_twenty_at_a_time() -> None:
    doc = {name: 5 for name in schema.FIELDS}
    assert 1 <= len(rules_of(doc)) <= 20
