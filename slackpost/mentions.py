"""Resolve human-friendly mentions (``@name``) to Slack's ``<@USERID>`` form.

Slack only treats ``<@U123>`` as a real mention (which notifies the user).
A literal ``@Abhijeet`` is rendered as plain text and notifies nobody.

This module builds a name -> user-id directory from ``users.list`` and
rewrites occurrences of ``@name`` / ``@Display Name`` into ``<@ID>``.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional

# Matches @token or @"Quoted Name"
_MENTION_RE = re.compile(r'@(?:"([^"]+)"|([A-Za-z0-9._\-]+))')


def _candidate_keys(user: dict) -> Iterable[str]:
    profile = user.get("profile") or {}
    names = {
        user.get("name"),
        user.get("real_name"),
        profile.get("display_name"),
        profile.get("real_name"),
        profile.get("display_name_normalized"),
        profile.get("real_name_normalized"),
    }
    for name in names:
        if not name:
            continue
        yield name
        # also allow matching on the first word (e.g. "Abhijeet" -> "Abhijeet Bendkhale")
        first = name.split()[0]
        if first != name:
            yield first


def build_directory(users: List[dict]) -> Dict[str, str]:
    """Return a case-insensitive map of name -> user id.

    Explicit ``name`` fields win over derived first names.
    """
    directory: Dict[str, str] = {}
    for user in users:
        uid = user.get("id")
        if not uid or user.get("deleted"):
            continue
        for key in _candidate_keys(user):
            directory.setdefault(key.lower(), uid)
    return directory


def resolve_text(text: str, directory: Dict[str, str]) -> str:
    """Replace known ``@name`` occurrences with ``<@USERID>``.

    Unknown ``@name`` tokens are left untouched so the caller can warn.
    """

    def repl(match: re.Match) -> str:
        name = match.group(1) or match.group(2)
        uid = directory.get(name.lower())
        return f"<@{uid}>" if uid else match.group(0)

    return _MENTION_RE.sub(repl, text)


def unresolved(text: str) -> List[str]:
    """Return remaining ``@tokens`` that were not converted.

    Already-resolved ``<@USERID>`` mentions are skipped.
    """
    found: List[str] = []
    for match in _MENTION_RE.finditer(text):
        # A leading '<' means this is part of an <@ID> mention - ignore it.
        if match.start() > 0 and text[match.start() - 1] == "<":
            continue
        found.append(match.group(0))
    return found
