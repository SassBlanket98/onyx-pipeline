"""Pick the investigator for a job before it runs.

Composer is fast and fine for clear, low-risk tasks. Anything where a missed fact or a
made-up one is expensive (credentials, public exposure, data loss, infrastructure, a vague
or sprawling spec) goes to Grok. The score is deterministic, so the reason can be shown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Weight 4 alone sends a task to Grok: a missed or invented fact is the costly kind here
# (access control, money, wide-reaching changes, hunting for a cause). Weight 2 needs
# company: two of them, or one with a long or multi-part task.
ACCESS = "credentials or access control"
SIGNALS: tuple[tuple[str, int, re.Pattern[str]], ...] = (
    (
        ACCESS,
        4,
        re.compile(
            r"\b(auth(?:n|z|entic\w*|oriz\w*)?|log[- ]?ins?|passwords?|credentials?|tokens?|"
            r"secrets?|api keys?|oauth|permissions?|roles?|encrypt\w*|accounts?)\b",
            re.I,
        ),
    ),
    (
        "money",
        4,
        re.compile(r"\b(payments?|billing|invoices?|stripe|refunds?|pricing)\b", re.I),
    ),
    (
        "public exposure",
        2,
        re.compile(r"\b(public|funnel|internet|production|prod|live|exposed|https?://)\b", re.I),
    ),
    (
        "database or schema",
        2,
        re.compile(r"\b(schema|migrations?|database|db|sql|postgres\w*|backups?|restore)\b", re.I),
    ),
    (
        "destructive change",
        2,
        re.compile(
            r"\b(delete|remove|drop|wipe|purge|truncate|overwrite|rm -rf|force[- ]push)\b", re.I
        ),
    ),
    (
        "infrastructure",
        2,
        re.compile(
            r"\b(systemd|caddy|nginx|docker|firewall|dns|cron|sudo|tailscale|network)\b", re.I
        ),
    ),
    (
        "cross-cutting change",
        4,
        re.compile(
            r"\b(refactor\w*|rewrite|migrate|architecture|redesign|"
            r"(across|every|all)( the)? (files|routes|pages|modules|services))\b",
            re.I,
        ),
    ),
    (
        "open-ended or hard to pin down",
        4,
        re.compile(
            r"\b(figure out|best way|root cause|intermittent|sometimes|flaky|race|concurren\w*|"
            r"performance|security|audit|why (does|is|did))\b",
            re.I,
        ),
    ),
)
# A plainly small task cancels a little of the score.
SMALL = re.compile(
    r"\b(docstring|typo|comment|readme|one[- ]line|rename|log message|spelling)\b", re.I
)


@dataclass(frozen=True)
class Triage:
    deep: bool
    score: int
    reasons: tuple[str, ...]


def triage_task(task: str, threshold: int = 4) -> Triage:
    score = 0
    reasons: list[str] = []
    heavy: list[str] = []
    for name, weight, pattern in SIGNALS:
        if pattern.search(task):
            score += weight
            reasons.append(name)
            if weight >= 4:
                heavy.append(name)
    parts = len(re.findall(r"(?:^|\s)(?:\d+[.)]|[-*])\s", task)) + task.count(" and ") // 2
    if parts >= 3:
        score += 1
        reasons.append("several requirements")
    if len(task) > 1000:
        score += 2
        reasons.append("very long task")
    elif len(task) > 600:
        score += 1
        reasons.append("long task")
    # A typo or docstring fix near a login page is small. An audit or a money change that
    # also mentions the README is not, so only access-control wording can be discounted.
    if SMALL.search(task) and len(task) < 200 and all(h == ACCESS for h in heavy):
        score -= 2
        reasons.append("looks small")
    return Triage(deep=score >= threshold, score=score, reasons=tuple(reasons))
