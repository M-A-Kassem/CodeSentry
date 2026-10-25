"""Project detection and scanning.

A *project* is one folder the user selected (or that was detected from the
file open in their editor). Scanning only ever walks that folder:

- ignored directories (``.git``, ``.venv``, ``node_modules``, ``bin``,
  ``obj``, build output, ... plus user-configured names) are skipped;
- symbolic links and junctions are never followed, so the walk cannot
  leave the project;
- drive roots, the home folder and system folders are refused as project
  roots, so the whole computer is never scanned;
- only file *metadata* is read here (names, sizes, modification times).
  File contents are read later, and only by the analyzers that need them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .diagnostic import language_for_path

DEFAULT_EXCLUDED_DIRS = frozenset(
    name.lower()
    for name in (
        ".git", ".hg", ".svn", ".vs", ".vscode", ".idea",
        ".venv", "venv", "env", ".env", ".tox", ".nox", "__pycache__",
        ".mypy_cache", ".ruff_cache", ".pytest_cache", ".ipynb_checkpoints",
        "node_modules", "bower_components", ".next", ".nuxt", ".parcel-cache",
        "bin", "obj", "build", "dist", "out", "target", "packages", ".gradle",
        "coverage", "htmlcov", "site-packages", ".eggs", "TestResults",
    )
)

# Files that identify a project root, and what kind of project they mean.
MANIFESTS: dict[str, str] = {
    "pyproject.toml": "Python (pyproject.toml)",
    "setup.py": "Python (setup.py)",
    "setup.cfg": "Python (setup.cfg)",
    "requirements.txt": "Python (requirements.txt)",
    "Pipfile": "Python (Pipfile)",
    "package.json": "Node.js (package.json)",
    "tsconfig.json": "TypeScript (tsconfig.json)",
    "pom.xml": "Java (Maven)",
    "build.gradle": "Java/Kotlin (Gradle)",
    "build.gradle.kts": "Kotlin (Gradle)",
    "CMakeLists.txt": "C/C++ (CMake)",
    "Makefile": "Make",
    "go.mod": "Go module",
    "Cargo.toml": "Rust (Cargo)",
    "composer.json": "PHP (Composer)",
    "Gemfile": "Ruby (Bundler)",
}
MANIFEST_SUFFIXES: dict[str, str] = {
    ".sln": ".NET solution",
    ".csproj": "C# project",
    ".vbproj": "VB.NET project",
    ".fsproj": "F# project",
    ".vcxproj": "C++ (MSBuild) project",
    ".pyproj": "Python (Visual Studio) project",
}

# Extensions treated as source files worth tracking even when no language
# name is known for them (kept small: the scanner is about code).
EXTRA_TRACKED = frozenset({".props", ".targets", ".config", ".resx", ".nuspec", ".vcxproj", ".fsproj"})

DEFAULT_MAX_FILES = 20_000


class UnsafeProjectRoot(ValueError):
    """Raised for a folder that must never be scanned as a project."""


@dataclass(frozen=True)
class SourceFile:
    path: str
    language: str
    size: int
    mtime: float


@dataclass
class ProjectProfile:
    root: str
    files: dict[str, SourceFile] = field(default_factory=dict)
    manifests: list[str] = field(default_factory=list)
    project_types: list[str] = field(default_factory=list)
    skipped_dirs: int = 0
    truncated: bool = False

    def files_for(self, language: str) -> list[SourceFile]:
        return [f for f in self.files.values() if f.language == language]

    def languages(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for source in self.files.values():
            counts[source.language] = counts.get(source.language, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: -item[1]))

    def contains(self, path: str) -> bool:
        return is_within(path, self.root)


def is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))]) == os.path.normcase(os.path.abspath(root))
    except ValueError:  # different drives
        return False


def _forbidden_roots() -> set[str]:
    candidates = [Path.home(), os.environ.get("SystemRoot"), os.environ.get("ProgramFiles"),
                  os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramData"), os.environ.get("USERPROFILE")]
    return {os.path.normcase(os.path.abspath(str(c))) for c in candidates if c}


def validate_project_root(root: str) -> str:
    """Return the normalised root, or raise :class:`UnsafeProjectRoot`."""
    if not root:
        raise UnsafeProjectRoot("no folder given")
    path = os.path.abspath(root)
    if not os.path.isdir(path):
        raise UnsafeProjectRoot(f"folder does not exist: {path}")
    if os.path.dirname(path.rstrip("\\/")) == path.rstrip("\\/") or len(Path(path).parts) <= 1:
        raise UnsafeProjectRoot(f"refusing to scan a whole drive: {path}")
    if os.path.normcase(path) in _forbidden_roots():
        raise UnsafeProjectRoot(f"refusing to scan a system or home folder: {path}")
    return path


def _is_link(entry: os.DirEntry) -> bool:
    if entry.is_symlink():
        return True
    # Directory junctions are not symlinks to Python but still leave the tree.
    try:
        return bool(getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400)  # REPARSE_POINT
    except OSError:
        return True


def scan_project(
    root: str,
    extra_excludes: Iterable[str] = (),
    max_files: int = DEFAULT_MAX_FILES,
) -> ProjectProfile:
    """Walk the project and return its source files (metadata only)."""
    root = validate_project_root(root)
    excluded = DEFAULT_EXCLUDED_DIRS | {name.lower() for name in extra_excludes}
    profile = ProjectProfile(root=root)
    types: dict[str, None] = {}
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            try:
                if _is_link(entry):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if name.lower() in excluded or name.lower().endswith(".egg-info"):
                        profile.skipped_dirs += 1
                    else:
                        stack.append(entry.path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            suffix = os.path.splitext(name)[1].lower()
            if name in MANIFESTS:
                profile.manifests.append(entry.path)
                types[MANIFESTS[name]] = None
            elif suffix in MANIFEST_SUFFIXES:
                profile.manifests.append(entry.path)
                types[MANIFEST_SUFFIXES[suffix]] = None
            language = language_for_path(name) or ("XML" if suffix in EXTRA_TRACKED else None)
            if language is None:
                continue
            if len(profile.files) >= max_files:
                profile.truncated = True
                stack.clear()
                break
            stat = entry.stat(follow_symlinks=False)
            profile.files[entry.path] = SourceFile(entry.path, language, stat.st_size, stat.st_mtime)
    profile.manifests.sort()
    profile.project_types = list(types)
    return profile


ROOT_MARKERS = tuple(MANIFESTS) + (".git",)


def find_project_root(path: str, max_levels: int = 8) -> Optional[str]:
    """Guess the project folder for a file open in an editor.

    Walks upward: the first folder containing ``.git`` wins; otherwise the
    highest folder (within ``max_levels``) containing a manifest or
    solution/project file. Returns ``None`` if nothing safe is found.
    """
    if not path:
        return None
    current = os.path.abspath(path)
    if os.path.isfile(current):
        current = os.path.dirname(current)
    best: Optional[str] = None
    for _ in range(max_levels):
        try:
            names = os.listdir(current)
        except OSError:
            break
        if ".git" in names:
            best = current
            break
        if any(n in MANIFESTS or os.path.splitext(n)[1].lower() in MANIFEST_SUFFIXES for n in names):
            best = current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    if best is None:
        return None
    try:
        return validate_project_root(best)
    except UnsafeProjectRoot:
        return None
