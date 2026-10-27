"""Best-effort, local explanations of diagnostics.

Rule-based only: each rule matches a diagnostic (by code or message) and
may look at the relevant source lines for evidence. Nothing is sent to any
service. Every explanation states its basis:

- ``high``: a plain restatement of what the diagnostic itself says.
- ``medium``: a likely cause supported by evidence found in the source
  (e.g. an unclosed bracket on line 12, a similarly named variable).
- ``low``: a common cause for this kind of message, not verified.

If no rule has enough to go on, :func:`explain` returns ``None`` and the UI
says so -- a cause is never invented.
"""
from __future__ import annotations

import difflib
import keyword
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional

from .diagnostic import Diagnostic
from .project import is_within

HIGH, MEDIUM, LOW = "high", "medium", "low"
CONFIDENCE_LABELS = {
    HIGH: "Based directly on the diagnostic message",
    MEDIUM: "Likely — supported by the source code, please verify",
    LOW: "Uncertain — a common cause of this message, not verified",
}
MAX_SOURCE_BYTES = 2_000_000


@dataclass
class Explanation:
    summary: str
    confidence: str
    details: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return CONFIDENCE_LABELS[self.confidence]


@dataclass
class SourceContext:
    lines: list[str]  # whole file
    line: Optional[int]  # 1-based line of the diagnostic

    def get(self, number: int) -> str:
        return self.lines[number - 1] if 1 <= number <= len(self.lines) else ""

    @property
    def current(self) -> str:
        return self.get(self.line or 0)

    def snippet(self, radius: int = 2) -> list[tuple[int, str]]:
        if not self.line:
            return []
        start, end = max(1, self.line - radius), min(len(self.lines), self.line + radius)
        return [(n, self.lines[n - 1]) for n in range(start, end + 1)]


def load_source(diagnostic: Diagnostic, project_root: Optional[str],
                buffers: Optional[dict[str, str]] = None) -> Optional[SourceContext]:
    """Read the diagnostic's file -- only if it lies inside the project.
    Unsaved editor text (``buffers``) is used in place of the disk copy."""
    path = diagnostic.file
    if not path or not project_root or not is_within(path, project_root):
        return None
    for buffer_path, text in (buffers or {}).items():
        if os.path.normcase(os.path.abspath(buffer_path)) == os.path.normcase(os.path.abspath(path)):
            return SourceContext(text.splitlines(), diagnostic.line) if len(text) <= MAX_SOURCE_BYTES else None
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_SOURCE_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_SOURCE_BYTES:
        return None
    text = data.decode("utf-8-sig", errors="replace")
    return SourceContext(text.splitlines(), diagnostic.line)


# -- helpers -------------------------------------------------------------------------

_QUOTED = r"""['"`\u2018\u2019]([A-Za-z_][\w]*)['"`\u2018\u2019]"""
_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_]\w*\b")
_PAIRS = {")": "(", "]": "[", "}": "{"}


def _quoted_name(message: str) -> Optional[str]:
    match = re.search(_QUOTED, message)
    return match.group(1) if match else None


def _similar_names(name: str, source: Optional[SourceContext]) -> list[tuple[str, int]]:
    """Names in the file that look like a typo of ``name``, with the line
    where each first appears."""
    if source is None:
        return []
    first_seen: dict[str, int] = {}
    for number, text in enumerate(source.lines, 1):
        for word in _IDENTIFIER_RE.findall(_strip_strings_and_comments(text)):
            if word != name and not keyword.iskeyword(word):
                first_seen.setdefault(word, number)
    matches = difflib.get_close_matches(name, list(first_seen), n=3, cutoff=0.8)
    return [(m, first_seen[m]) for m in matches]


def _strip_strings_and_comments(text: str) -> str:
    text = re.sub(r"(\"(\\.|[^\"\\])*\"|'(\\.|[^'\\])*')", '""', text)
    return re.split(r"#|//", text, maxsplit=1)[0]


def _unclosed_bracket(source: SourceContext, up_to: Optional[int] = None) -> Optional[tuple[str, int]]:
    """The innermost bracket opened but never closed, as (char, line)."""
    stack: list[tuple[str, int]] = []
    limit = up_to or len(source.lines)
    for number in range(1, limit + 1):
        for char in _strip_strings_and_comments(source.get(number)):
            if char in "([{":
                stack.append((char, number))
            elif char in _PAIRS:
                if stack and stack[-1][0] == _PAIRS[char]:
                    stack.pop()
                else:
                    return None  # mismatched rather than unclosed: don't guess
    return stack[-1] if stack else None


# -- rules ---------------------------------------------------------------------------

Rule = Callable[[Diagnostic, Optional[SourceContext]], Optional[Explanation]]
RULES: list[Rule] = []


def rule(func: Rule) -> Rule:
    RULES.append(func)
    return func


_UNDEFINED_RE = re.compile(
    r"undefined name|is not defined|The name .* does not exist in the current context|Cannot find name|"
    r"cannot find symbol|was not declared in this scope|undeclared identifier|use of undeclared",
    re.I,
)


@rule
def undefined_name(d: Diagnostic, source):
    if not (_UNDEFINED_RE.search(d.message) or d.code in ("F821", "CS0103", "TS2304", "reportUndefinedVariable")):
        return None
    name = _quoted_name(d.message)
    if not name:
        return Explanation("A name is used that the compiler cannot find in this scope.", HIGH)
    details = []
    similar = _similar_names(name, source)
    if similar:
        candidate, line = similar[0]
        return Explanation(
            f"`{name}` is not defined here. A similar name, `{candidate}`, appears on line {line} — this may be a typo.",
            MEDIUM,
            [f"Other similar names: {', '.join(f'`{n}`' for n, _ in similar[1:])}"] if len(similar) > 1 else [],
        )
    if d.language == "Python" and name in getattr(sys, "stdlib_module_names", ()):
        return Explanation(f"`{name}` is a Python standard-library module, but it is not imported in this file. "
                           f"An `import {name}` is probably missing.", MEDIUM)
    if d.language == "C#":
        details.append("If it is a type or method from another namespace, a `using` directive may be missing.")
    elif d.language == "Python":
        details.append("Common causes: a missing import, a misspelling, or using the name before it is assigned.")
    return Explanation(f"`{name}` is used but is not defined, declared, or imported where it is used.", HIGH, details)


_CONVERT_RE = re.compile(
    r"Cannot implicitly convert type '(?P<a>[^']+)' to '(?P<b>[^']+)'|Type '(?P<a2>[^']+)' is not assignable to type '(?P<b2>[^']+)'|"
    r"Incompatible types in assignment \(expression has type \"(?P<a3>[^\"]+)\", variable has type \"(?P<b3>[^\"]+)\"\)|"
    r"incompatible types: (?P<a4>\S+) cannot be converted to (?P<b4>\S+)"
)
_PARSE_HINTS = {
    ("string", "int"): "use `int.Parse(...)` or `int.TryParse(...)` to convert text to a number",
    ("string", "double"): "use `double.Parse(...)` or `double.TryParse(...)`",
    ("str", "int"): "use `int(...)` to convert text to a number",
    ("string", "number"): "use `Number(...)` or `parseInt(...)` to convert text to a number",
}


@rule
def type_mismatch(d: Diagnostic, source):
    match = _CONVERT_RE.search(d.message)
    if not match:
        return None
    groups = match.groupdict()
    actual = next(groups[k] for k in ("a", "a2", "a3", "a4") if groups.get(k))
    expected = next(groups[k] for k in ("b", "b2", "b3", "b4") if groups.get(k))
    details = []
    hint = _PARSE_HINTS.get((actual.lower(), expected.lower()))
    if hint:
        details.append(f"If the text is meant to be a number, {hint}.")
    if source and source.current:
        details.append(f"Line {source.line}: {source.current.strip()}")
    return Explanation(f"A value of type `{actual}` is used where `{expected}` is required, and there is no "
                       f"automatic conversion between them.", HIGH, details)


@rule
def missing_member(d: Diagnostic, source):
    match = re.search(r"does not contain a definition for '(\w+)'|Property '(\w+)' does not exist on type '([^']+)'|"
                      r"has no attribute '(\w+)'|Cannot access attribute \"(\w+)\"", d.message)
    if not match:
        return None
    member = next(g for g in match.groups() if g)
    similar = _similar_names(member, source)
    if similar:
        return Explanation(f"`{member}` is not a member of that type. `{similar[0][0]}` (line {similar[0][1]}) has a "
                           "similar name — possibly a typo.", MEDIUM)
    return Explanation(f"The code uses `{member}`, but the object's type does not define a member with that name.", HIGH,
                       ["Check the spelling and the type of the object it is called on."])


_EOF_RE = re.compile(r"was never closed|unexpected EOF|end of file|'\}' expected|Unexpected end of input|"
                     r"reached end of file while parsing|EOF while scanning", re.I)
_EXPECTED_CLOSER_RE = re.compile(r"""[Ee]xpected\s*[`'"]?([)\]}])[`'"]?|[`'"]([)\]}])[`'"] expected""")
_UNEXPECTED_RE = re.compile(r"Unexpected (token|identifier)|invalid syntax|Expected an expression", re.I)


@rule
def unclosed_bracket(d: Diagnostic, source):
    eof = bool(_EOF_RE.search(d.message)) or d.code in ("CS1513", "CS1026")
    closer = _EXPECTED_CLOSER_RE.search(d.message)
    vague = bool(_UNEXPECTED_RE.search(d.message))
    if not (eof or closer or vague):
        return None
    # Whole-file balance only: up to the error line, an ordinary open block
    # (e.g. a function body) would look "unclosed" and mislead.
    found = _unclosed_bracket(source) if source else None
    if found:
        char, line = found
        closing = {"(": ")", "[": "]", "{": "}"}[char]
        return Explanation(f"The `{char}` opened on line {line} is never closed with `{closing}`, so the code after it "
                           "is read as part of that bracket.", MEDIUM, [f"Line {line}: {source.get(line).strip()}"])
    if closer:
        wanted = closer.group(1) or closer.group(2)
        return Explanation(f"The parser expected `{wanted}` at this point — a closing `{wanted}` is probably missing.", HIGH)
    if eof:
        return Explanation("The file ends before a bracket, brace, or block was closed.", LOW)
    return None  # a vague "unexpected token" with no evidence: don't guess


@rule
def statements_on_one_line(d: Diagnostic, source):
    if "Simple statements must be separated by newlines or semicolons" not in d.message:
        return None
    details = [f"Line {source.line}: {source.current.strip()}"] if source and source.current.strip() else []
    return Explanation("Two statements are written on the same line with nothing separating them. Python needs a line "
                       "break (or `;`) between statements, so something extra or missing near this column — e.g. a "
                       "stray word, a missing operator, or a missing quote — makes the line read as two statements.",
                       HIGH, details)


@rule
def missing_colon(d: Diagnostic, source):
    if "expected ':'" not in d.message:
        return None
    details = [f"Line {source.line}: {source.current.strip()}"] if source and source.current else []
    return Explanation("A Python block header (if/for/while/def/class/else/try/with ...) must end with a colon `:`.", HIGH, details)


@rule
def missing_semicolon(d: Diagnostic, source):
    if d.code != "CS1002" and "';' expected" not in d.message and "expected ';'" not in d.message:
        return None
    details = []
    if source and source.line:
        previous = source.get(source.line - 1).strip() if (source.current.strip() == "" or (d.column or 0) <= 1) else source.current.strip()
        if previous:
            details.append(f"Check the end of: {previous}")
    return Explanation("A statement is not terminated with `;`.", HIGH, details)


@rule
def indentation(d: Diagnostic, source):
    if not re.search(r"IndentationError|TabError|unexpected indent|unindent does not match|expected an indented block", d.message):
        return None
    if source:
        nearby = [source.get(n) for n in range(max(1, (source.line or 1) - 5), (source.line or 1) + 1)]
        uses_tabs = any(t.startswith("\t") for t in nearby)
        uses_spaces = any(t.startswith(" ") for t in nearby)
        if uses_tabs and uses_spaces:
            return Explanation("Tabs and spaces are mixed for indentation near this line, which Python rejects.", MEDIUM)
    return Explanation("The indentation of this line doesn't match the surrounding block structure.", HIGH)


@rule
def json_trailing_comma(d: Diagnostic, source):
    if d.source != "json-syntax":
        return None
    if source and source.line and re.search(r"Expecting (property name|value)", d.message):
        # The error points at the closing bracket; look at the line before it.
        number = source.line
        if source.current.strip()[:1] in ("}", "]"):
            number -= 1
        while number > 0 and not source.get(number).strip():
            number -= 1
        if number > 0 and source.get(number).rstrip().endswith(","):
            return Explanation(f"Line {number} ends with a trailing comma, which JSON doesn't allow before `}}` or `]`.",
                               MEDIUM, [f"Line {number}: {source.get(number).strip()}"])
    if "Expecting property name enclosed in double quotes" in d.message:
        return Explanation("JSON keys must be in double quotes; single quotes, comments, and trailing commas are not allowed.", LOW)
    return None


@rule
def unresolved_import(d: Diagnostic, source):
    match = re.search(r"type or namespace name '(\w+)' could not be found|Import \"([\w.]+)\" could not be resolved|"
                      r"No module named '([\w.]+)'|Cannot find module '([^']+)'|package ([\w.]+) does not exist", d.message)
    if not match:
        return None
    name = next(g for g in match.groups() if g)
    if d.language == "C#":
        return Explanation(f"`{name}` isn't found: a `using` directive, a NuGet package, or a project reference is probably "
                           "missing (or the name is misspelled).", LOW)
    return Explanation(f"`{name}` can't be resolved: the package may not be installed in the environment the analyzer used, "
                       "or the module name is misspelled.", LOW)


@rule
def unused(d: Diagnostic, source):
    if d.code in ("F401", "F841", "CS0168", "CS0219", "CS8019", "TS6133") or re.search(r"imported but unused|is assigned but its value is never used|is declared but (its value is )?never (read|used)|never used", d.message):
        return Explanation("Something is declared or imported but never used. This is harmless at runtime but is often "
                           "left over from editing — remove it or use it.", HIGH)
    return None


@rule
def nullable(d: Diagnostic, source):
    if d.code in ("CS8600", "CS8602", "CS8603", "CS8604", "CS8618", "CS8625") or "possibly 'None'" in d.message or "is possibly 'null'" in d.message:
        return Explanation("A value that may be null/None is used where a non-null value is required. Check for null first, "
                           "or make the type nullable if null is allowed.", HIGH)
    return None


def explain(diagnostic: Diagnostic, project_root: Optional[str] = None,
            buffers: Optional[dict[str, str]] = None) -> Optional[Explanation]:
    """Best-effort explanation, or ``None`` when the evidence is insufficient."""
    source = load_source(diagnostic, project_root, buffers)
    for check in RULES:
        try:
            result = check(diagnostic, source)
        except Exception:  # a buggy rule must never break the issue list
            continue
        if result is not None:
            return result
    return None
