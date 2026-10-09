"""Parse and validate the operator brief's structured JSON block.

The brief is a markdown body followed by one fenced json block. Approval acts on the
json, so a brief that is malformed, has no steps, or lists no changes must never reach
the operator: ``parse_brief`` raises ``ValueError`` instead.
"""

from __future__ import annotations

import json
import re

_JSON_FENCE = re.compile(r"```json[ \t]*\r?\n(.*?)```", re.DOTALL)
_STEP_KEYS = ("number", "action", "state_change", "exact_values", "verification")
_STEP_STRINGS = ("action", "exact_values", "verification")


def _json_block(text: str) -> str:
    blocks = _JSON_FENCE.findall(text)
    if not blocks:
        raise ValueError("brief has no fenced json block")
    # The prompt puts the schema block last and nothing after it; take the last one
    # so an example earlier in the body cannot shadow the real brief.
    return blocks[-1].strip()


def _validate(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("brief json must be an object")
    if data.get("version") != 1:
        raise ValueError("brief version must be 1")
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("brief summary is empty")

    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("brief steps are empty")
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise ValueError(f"step {i} is not an object")
        for key in _STEP_KEYS:
            if key not in step:
                raise ValueError(f"step {i} is missing {key}")
        if not isinstance(step["number"], int) or isinstance(step["number"], bool):
            raise ValueError(f"step {i} number must be an integer")
        if not isinstance(step["state_change"], bool):
            raise ValueError(f"step {i} state_change must be a boolean")
        for key in _STEP_STRINGS:
            if not isinstance(step[key], str):
                raise ValueError(f"step {i} {key} must be a string")
        if not step["action"].strip():
            raise ValueError(f"step {i} action is empty")

    changes = data.get("changes")
    if not isinstance(changes, list) or not changes:
        raise ValueError("brief changes are empty")
    for i, change in enumerate(changes, 1):
        if not isinstance(change, str) or not change.strip():
            raise ValueError(f"change {i} must be a non-empty string")
    return data


def parse_brief(text: str) -> dict:
    """Return the validated schema dict from a brief's fenced json block.

    Raises ``ValueError`` when the block is missing or malformed, when ``steps`` is
    empty, or when ``changes`` is empty or missing.
    """
    payload = _json_block(text)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"brief json is malformed: {exc}") from exc
    return _validate(data)


def render_operator_brief(brief: dict) -> str:
    """Render the approved schema as the operator's numbered instruction.

    The operator works from the steps David approved, not the prose brief, so the
    summary and each step's action, exact values and verification all travel with
    the instruction, in step order.
    """
    summary = str(brief.get("summary") or "").strip()
    lines = [summary, "", "Steps:"] if summary else ["Steps:"]
    steps = brief.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            number = step.get("number")
            action = str(step.get("action") or "").strip()
            values = str(step.get("exact_values") or "").strip()
            verification = str(step.get("verification") or "").strip()
            lines.append(f"{number}. {action} — {values}")
            lines.append(f"   Verify: {verification}")
    return "\n".join(lines) + "\n"
