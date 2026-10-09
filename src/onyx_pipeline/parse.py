"""Parsers for plan trailers, open questions, review verdicts, and PARTS."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

PART_HEADER_RE = re.compile(
    r"^PART\s+(\d+):\s*([^|]+?)\s*\|\s*FILES:\s*([^|]*)\s*\|\s*"
    r"AFTER:\s*([^|]*)\s*\|\s*JUDGMENT:\s*(none|needed)\s*$"
)


@dataclass
class PlanPart:
    n: int
    title: str
    files: list[str]
    after: list[int] = field(default_factory=list)
    judgment: str = "none"
    text: str = ""


def parse_section(text: str, heading: str) -> str:
    """Return the body of a `## <heading>` section, or empty if it is absent."""
    heading_re = re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.I)
    lines = text.splitlines()
    start: int | None = None
    for i, raw in enumerate(lines):
        if heading_re.match(raw.strip()):
            start = i + 1
            break
    if start is None:
        return ""
    collected: list[str] = []
    for raw in lines[start:]:
        stripped = raw.strip()
        if stripped.startswith("## "):
            break
        if stripped.startswith(("SIZE:", "JUDGMENT:", "VERIFY:")):
            break
        collected.append(raw)
    return "\n".join(collected).strip()


def parse_plan_trailers(text: str) -> tuple[str, str, str | None]:
    size: str | None = None
    judgment: str | None = None
    verify: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("SIZE:"):
            size = line[5:].strip()
        elif line.startswith("JUDGMENT:"):
            judgment = line[9:].strip()
        elif line.startswith("VERIFY:"):
            verify = line[7:].strip() or None
    if size not in ("small", "big"):
        size = "big"
    if judgment not in ("none", "needed"):
        judgment = "none"
    return size, judgment, verify


def parse_open_questions(text: str) -> list[str]:
    questions: list[str] = []
    in_section = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if re.match(r"^##\s+OPEN QUESTIONS\s*$", stripped, re.I):
            in_section = True
            continue
        if not in_section:
            continue
        if stripped.startswith("#") and not stripped.startswith("###"):
            break
        if stripped.startswith(("SIZE:", "JUDGMENT:", "VERIFY:")):
            break
        if not stripped:
            continue
        cleaned = re.sub(r"^(\d+\.|[-*])\s*", "", stripped)
        if cleaned:
            questions.append(cleaned)
    return questions


def parse_verdict(text: str, *, corrections: bool = False) -> str:
    """Read the trailing VERDICT line. CORRECTIONS only counts for fact-checks."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "FAIL"
    last = lines[-1]
    allowed = {"VERDICT: PASS", "VERDICT: FAIL"}
    if corrections:
        allowed.add("VERDICT: CORRECTIONS")
    if last in allowed:
        return last.split(": ", 1)[1]
    return "FAIL"


def planned_files(plan_text: str) -> list[str]:
    """Files the plan says will change, in order, without duplicates."""
    seen: set[str] = set()
    ordered: list[str] = []

    def add(path: str) -> None:
        if path not in seen:
            seen.add(path)
            ordered.append(path)

    parts, _note = parse_plan_parts(plan_text, max_parts=99)
    for part in parts:
        for file_path in part.files:
            add(file_path)
    for raw in parse_section(plan_text, "CHANGES").splitlines():
        stripped = raw.strip()
        if not stripped.startswith("- "):
            continue
        body = stripped[2:].strip()
        for sep in (" — ", " - ", " : "):
            if sep in body:
                body = body.split(sep, 1)[0]
                break
        body = body.strip().strip("`").strip()
        if " " in body:
            continue
        if "/" not in body and "." not in body:
            continue
        add(body)
    return ordered


def plan_text_before_parts(text: str) -> str:
    lines: list[str] = []
    for raw in text.splitlines():
        if re.match(r"^##\s+PARTS\s*$", raw.strip(), re.I):
            break
        lines.append(raw)
    return "\n".join(lines).strip()


def parse_plan_parts(text: str, *, max_parts: int = 3) -> tuple[list[PlanPart], str | None]:
    """Parse ## PARTS. Missing section -> ([], None). Invalid -> ([], note)."""
    section = _parts_section(text)
    if section is None:
        return [], None
    parsed, error = _parse_part_headers(section)
    if error:
        return [], error
    if not parsed:
        return [], "malformed PARTS section"
    if len(parsed) > max_parts:
        return [], f"more than {max_parts} parts"
    numbers = [p.n for p in parsed]
    if numbers != list(range(1, len(parsed) + 1)):
        return [], "malformed PARTS section"
    if any(dep not in {p.n for p in parsed} for part in parsed for dep in part.after):
        return [], "malformed PARTS section"
    if _has_cycle(parsed):
        return [], "cycle in AFTER"
    if _independent_share_files(parsed):
        return [], "independent parts share files"
    return parsed, None


def _parts_section(text: str) -> str | None:
    lines = text.splitlines()
    start: int | None = None
    for i, raw in enumerate(lines):
        if re.match(r"^##\s+PARTS\s*$", raw.strip(), re.I):
            start = i + 1
            break
    if start is None:
        return None
    collected: list[str] = []
    for raw in lines[start:]:
        stripped = raw.strip()
        if stripped.startswith("#") and not stripped.startswith("###"):
            break
        if stripped.startswith(("SIZE:", "JUDGMENT:", "VERIFY:")):
            break
        collected.append(raw)
    return "\n".join(collected)


def _parse_part_headers(section: str) -> tuple[list[PlanPart], str | None]:
    parts: list[PlanPart] = []
    current: PlanPart | None = None
    body: list[str] = []
    for raw in section.splitlines():
        match = PART_HEADER_RE.match(raw.strip())
        if match:
            if current is not None:
                current.text = "\n".join(body).strip()
                parts.append(current)
            n = int(match.group(1))
            title = match.group(2).strip()
            files = [_norm_file(p) for p in match.group(3).split(",") if p.strip()]
            after_raw = match.group(4).strip()
            judgment = match.group(5)
            if not title or not files:
                return [], "malformed PARTS section"
            if after_raw.lower() == "none" or after_raw == "":
                after: list[int] = []
            else:
                after = []
                for item in after_raw.split(","):
                    item = item.strip()
                    if not item.isdigit():
                        return [], "malformed PARTS section"
                    after.append(int(item))
            current = PlanPart(n=n, title=title, files=files, after=after, judgment=judgment)
            body = []
            continue
        if current is None:
            if raw.strip():
                return [], "malformed PARTS section"
            continue
        body.append(raw)
    if current is not None:
        current.text = "\n".join(body).strip()
        parts.append(current)
    return parts, None


def _norm_file(path: str) -> str:
    return str(Path(path.strip()))


def _has_cycle(parts: list[PlanPart]) -> bool:
    by_n = {p.n: p for p in parts}
    white, grey, black = 0, 1, 2
    color = {p.n: white for p in parts}

    def dfs(n: int) -> bool:
        color[n] = grey
        for dep in by_n[n].after:
            if dep not in color:
                continue
            if color[dep] == grey:
                return True
            if color[dep] == white and dfs(dep):
                return True
        color[n] = black
        return False

    return any(color[p.n] == white and dfs(p.n) for p in parts)


def _independent_share_files(parts: list[PlanPart]) -> bool:
    by_n = {p.n: p for p in parts}

    def ancestors(n: int) -> set[int]:
        seen: set[int] = set()
        stack = list(by_n[n].after)
        while stack:
            dep = stack.pop()
            if dep in seen or dep not in by_n:
                continue
            seen.add(dep)
            stack.extend(by_n[dep].after)
        return seen

    ancs = {p.n: ancestors(p.n) for p in parts}
    for i, left in enumerate(parts):
        for right in parts[i + 1 :]:
            related = left.n in ancs[right.n] or right.n in ancs[left.n]
            if related:
                continue
            if set(left.files) & set(right.files):
                return True
    return False
