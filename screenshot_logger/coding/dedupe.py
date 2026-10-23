"""Merge the same problem reported by different tools.

Two diagnostics are treated as one problem when they are in the same file,
have the same severity, are on the same line (syntax errors: within one
line, since parsers disagree on where a broken statement "is"), and
either carry the same code, are both syntax errors, or name the same
identifier (``'foo'`` / ``"foo"`` / `` `foo` ``). Anything less certain is
kept separate -- two real problems on one line must not be merged away.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from .diagnostic import Diagnostic, normalize_path

SYNTAX_CODES = frozenset({"syntax", "invalid-syntax", "E999"})
_QUOTED_RE = re.compile(r"""['"`‘’“”]([A-Za-z_][\w.]*)['"`‘’“”]""")


def is_syntax(diagnostic: Diagnostic) -> bool:
    if diagnostic.code in SYNTAX_CODES:
        return True
    from .analyzers.csharp import SYNTAX_CODES as CSHARP_SYNTAX

    return diagnostic.code in CSHARP_SYNTAX or diagnostic.message.startswith(("SyntaxError", "IndentationError", "TabError"))


def identifiers(message: str) -> set[str]:
    return set(_QUOTED_RE.findall(message))


def same_problem(a: Diagnostic, b: Diagnostic) -> bool:
    if not a.file or not b.file or normalize_path(a.file) != normalize_path(b.file):
        return False
    if a.severity != b.severity or a.line is None or b.line is None:
        return False
    both_syntax = is_syntax(a) and is_syntax(b)
    if abs(a.line - b.line) > (1 if both_syntax else 0):
        return False
    if both_syntax:
        return True
    if a.code and a.code == b.code:
        return True
    shared = identifiers(a.message) & identifiers(b.message)
    return bool(shared)


def merge_cross_tool(diagnostics: Iterable[Diagnostic], priority: Optional[dict[str, int]] = None) -> list[Diagnostic]:
    """Collapse duplicates from *different* tools, keeping the report of the
    highest-priority tool and noting the others in ``source``."""
    priority = priority or {}
    ordered = sorted(diagnostics, key=lambda d: -priority.get((d.source or "").split(" ")[0], 0))
    kept: list[Diagnostic] = []
    # Only diagnostics in the same file with the same severity can match.
    buckets: dict[tuple[str, str], list[int]] = {}
    for diagnostic in ordered:
        bucket = buckets.setdefault((normalize_path(diagnostic.file), diagnostic.severity), [])
        for index in bucket:
            existing = kept[index]
            if existing.source != diagnostic.source and same_problem(existing, diagnostic):
                tool = (diagnostic.source or "?").split(" ")[0]
                if tool not in (existing.source or ""):
                    kept[index] = existing.with_changes(source=f"{existing.source} (also: {tool})")
                break
        else:
            bucket.append(len(kept))
            kept.append(diagnostic)
    return kept
