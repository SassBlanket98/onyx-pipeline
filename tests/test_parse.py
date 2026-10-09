"""Unit tests for plan trailer and PARTS parsing."""

from __future__ import annotations

from onyx_pipeline.parse import parse_plan_parts, parse_plan_trailers, parse_section
from onyx_pipeline.prompts import plan_prompt

PLAN_HEADER = """\
# Plan
Do the work.

"""

TRAILERS = "SIZE: small\nJUDGMENT: none\nVERIFY: true\n"


def _plan(*parts: str) -> str:
    body = PLAN_HEADER
    if parts:
        body += "## PARTS\n" + "\n".join(parts) + "\n"
    return body + TRAILERS


def test_missing_parts_is_empty_without_note() -> None:
    parts, note = parse_plan_parts(_plan())
    assert parts == []
    assert note is None


def test_three_independent_parts() -> None:
    text = _plan(
        "PART 1: Alpha | FILES: a.py, lib/b.py | AFTER: none | JUDGMENT: none",
        "steps for alpha",
        "PART 2: Beta | FILES: c.py | AFTER: none | JUDGMENT: needed",
        "steps for beta",
        "PART 3: Gamma | FILES: d.py | AFTER: none | JUDGMENT: none",
        "steps for gamma",
    )
    parts, note = parse_plan_parts(text)
    assert note is None
    assert [p.n for p in parts] == [1, 2, 3]
    assert parts[0].title == "Alpha"
    assert parts[0].files == ["a.py", "lib/b.py"]
    assert parts[0].after == []
    assert parts[0].judgment == "none"
    assert "steps for alpha" in parts[0].text
    assert parts[1].judgment == "needed"
    assert parts[1].files == ["c.py"]


def test_after_dependency() -> None:
    text = _plan(
        "PART 1: Base | FILES: a.py | AFTER: none | JUDGMENT: none",
        "base steps",
        "PART 2: Uses base | FILES: b.py | AFTER: 1 | JUDGMENT: none",
        "dep steps",
    )
    parts, note = parse_plan_parts(text)
    assert note is None
    assert parts[1].after == [1]


def test_malformed_parts_falls_back() -> None:
    text = _plan("this is not a part header", "PART 2 has no structure")
    parts, note = parse_plan_parts(text)
    assert parts == []
    assert note is not None
    assert "malformed" in note.lower()


def test_too_many_parts_falls_back() -> None:
    headers = [
        f"PART {i}: P{i} | FILES: p{i}.py | AFTER: none | JUDGMENT: none" for i in range(1, 5)
    ]
    parts, note = parse_plan_parts(_plan(*headers), max_parts=3)
    assert parts == []
    assert note is not None
    assert "3" in note or "more" in note.lower()


def test_cycle_falls_back() -> None:
    text = _plan(
        "PART 1: A | FILES: a.py | AFTER: 2 | JUDGMENT: none",
        "one",
        "PART 2: B | FILES: b.py | AFTER: 1 | JUDGMENT: none",
        "two",
    )
    parts, note = parse_plan_parts(text)
    assert parts == []
    assert note is not None
    assert "cycle" in note.lower()


def test_independent_shared_files_falls_back() -> None:
    text = _plan(
        "PART 1: A | FILES: a.py, shared.py | AFTER: none | JUDGMENT: none",
        "one",
        "PART 2: B | FILES: b.py, shared.py | AFTER: none | JUDGMENT: none",
        "two",
    )
    parts, note = parse_plan_parts(text)
    assert parts == []
    assert note is not None
    assert "file" in note.lower()


def test_dependent_parts_may_share_files() -> None:
    text = _plan(
        "PART 1: A | FILES: a.py | AFTER: none | JUDGMENT: none",
        "one",
        "PART 2: B | FILES: a.py, b.py | AFTER: 1 | JUDGMENT: none",
        "two",
    )
    parts, note = parse_plan_parts(text)
    assert note is None
    assert len(parts) == 2
    assert parts[1].files == ["a.py", "b.py"]


def test_trailers_still_parse_when_parts_present() -> None:
    text = _plan(
        "PART 1: A | FILES: a.py | AFTER: none | JUDGMENT: needed",
        "one",
    )
    size, judgment, verify = parse_plan_trailers(text)
    assert size == "small"
    assert judgment == "none"
    assert verify == "true"


def test_plan_prompt_describes_parts_before_trailers() -> None:
    prompt = plan_prompt("task", "evidence")
    parts_at = prompt.index("## PARTS")
    size_at = prompt.index("SIZE: small|big")
    assert parts_at < size_at
    assert "edit different files" in prompt
    assert "At most 3 parts" in prompt
    assert "AFTER" in prompt
    assert "Leave out" in prompt


def test_plan_prompt_requires_david_sections_before_trailers() -> None:
    prompt = plan_prompt("task", "evidence")
    end_at = prompt.index("End with exactly these three lines")
    for heading in ("## GOAL", "## CHANGES", "## OUT OF SCOPE", "## ASSUMPTIONS AND DEVIATIONS"):
        assert heading in prompt
        assert prompt.index(heading) < end_at


SECTIONED = """\
# Plan
intro

## GOAL
Ship the marker file.

## CHANGES
- a.py — add helper
- b.py — call it

## OUT OF SCOPE
- docs

## assumptions and deviations
none

## PARTS
PART 1: A | FILES: a.py | AFTER: none | JUDGMENT: none
steps

SIZE: small
JUDGMENT: none
VERIFY: true
"""


def test_parse_section_returns_body() -> None:
    assert parse_section(SECTIONED, "GOAL") == "Ship the marker file."
    assert parse_section(SECTIONED, "CHANGES") == "- a.py — add helper\n- b.py — call it"
    assert parse_section(SECTIONED, "OUT OF SCOPE") == "- docs"


def test_parse_section_heading_is_case_insensitive() -> None:
    assert parse_section(SECTIONED, "ASSUMPTIONS AND DEVIATIONS") == "none"
    assert parse_section(SECTIONED, "goal") == "Ship the marker file."


def test_parse_section_stops_at_trailer_and_strips() -> None:
    text = "## GOAL\n\n  body line  \n\nSIZE: small\n"
    assert parse_section(text, "GOAL") == "body line"


def test_parse_section_absent_is_empty() -> None:
    assert parse_section("# Plan\nno headings\n", "GOAL") == ""
    assert parse_section("", "CHANGES") == ""
