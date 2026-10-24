"""C# analyzers.

``csharp-compiler`` (default): runs the Roslyn compiler (``csc``) that
ships with the installed .NET SDK directly -- *not* MSBuild. That means no
project build targets run and nothing is written to ``bin``/``obj``; output
goes to a private temp folder. Each ``.csproj`` is checked against the
SDK's reference assemblies for its target framework, with the SDK's
implicit usings, so real semantic errors (CS0103, CS0029, ...) are found.

When a project's dependencies can't be resolved this way (NuGet packages,
project references, WPF/XAML, Razor, .NET Framework), only *syntax* errors
are reported for it and the language is marked as partially analyzed.

``dotnet-build`` (opt-in): a full ``dotnet build``. It runs MSBuild, which
executes the project's build targets and writes ``bin``/``obj``, so it is
off unless the user enables it.
"""
from __future__ import annotations

import os
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

from ..project import SourceFile, is_within
from ..providers.build_tools import BuildToolSpec, parse_tool_output
from .base import Analyzer, AnalyzerRun, Availability, RunContext, which

CSHARP = "C#"

# Roslyn parser (syntax) error codes. In syntax-only mode, only these are
# reported, because without references every other error is noise.
SYNTAX_CODES = frozenset(
    f"CS{n:04d}" for n in (
        1001, 1002, 1003, 1004, 1007, 1009, 1010, 1011, 1012, 1013, 1014, 1015, 1017, 1018, 1019,
        1020, 1021, 1022, 1024, 1025, 1026, 1027, 1028, 1029, 1031, 1032, 1033, 1034, 1035, 1036,
        1037, 1038, 1039, 1040, 1041, 1043, 1044, 1055, 1056, 1057, 1073, 1513, 1514, 1515, 1517,
        1518, 1519, 1520, 1521, 1524, 1525, 1526, 1527, 1528, 1529, 1530, 1534, 1536, 1552, 1553,
        1554, 1576, 1597, 1609, 1611, 8124, 8635, 8641, 8803,
    )
)

_IMPLICIT_USINGS = {
    "Microsoft.NET.Sdk": ["System", "System.Collections.Generic", "System.IO", "System.Linq",
                          "System.Net.Http", "System.Threading", "System.Threading.Tasks"],
    "Microsoft.NET.Sdk.Web": ["System.Net.Http.Json", "Microsoft.AspNetCore.Builder", "Microsoft.AspNetCore.Hosting",
                              "Microsoft.AspNetCore.Http", "Microsoft.AspNetCore.Routing",
                              "Microsoft.Extensions.Configuration", "Microsoft.Extensions.DependencyInjection",
                              "Microsoft.Extensions.Hosting", "Microsoft.Extensions.Logging"],
    "WindowsForms": ["System.Drawing", "System.Windows.Forms"],
}


@dataclass
class CsProject:
    path: str
    sdk: str = ""
    target_framework: str = ""
    output_type: str = "Library"
    implicit_usings: bool = False
    nullable: str = ""
    allow_unsafe: bool = False
    use_wpf: bool = False
    use_winforms: bool = False
    default_compile_items: bool = True
    package_references: list[str] = field(default_factory=list)
    project_references: list[str] = field(default_factory=list)
    framework_references: list[str] = field(default_factory=list)
    other_references: list[str] = field(default_factory=list)
    usings_added: list[str] = field(default_factory=list)
    usings_removed: list[str] = field(default_factory=list)

    @property
    def directory(self) -> str:
        return os.path.dirname(self.path)


def parse_csproj(path: str) -> CsProject:
    """Read a .csproj as plain XML (it is never evaluated by MSBuild)."""
    project = CsProject(path=path)
    root = ET.parse(path).getroot()
    for element in root.iter():
        element.tag = element.tag.split("}")[-1]  # old-style csproj has a namespace
    project.sdk = (root.get("Sdk") or "").split("/")[0]
    for sdk_element in root.findall("Sdk"):
        project.sdk = project.sdk or (sdk_element.get("Name") or "")

    def prop(name: str) -> str:
        values = [e.text.strip() for e in root.iter(name) if e.text]
        return values[-1] if values else ""

    project.target_framework = prop("TargetFramework") or prop("TargetFrameworks").split(";")[0] or prop("TargetFrameworkVersion")
    project.output_type = prop("OutputType") or ("Exe" if project.sdk.endswith(".Web") else "Library")
    project.implicit_usings = prop("ImplicitUsings").lower() in ("enable", "true")
    project.nullable = prop("Nullable").lower()
    project.allow_unsafe = prop("AllowUnsafeBlocks").lower() == "true"
    project.use_wpf = prop("UseWPF").lower() == "true"
    project.use_winforms = prop("UseWindowsForms").lower() == "true"
    project.default_compile_items = prop("EnableDefaultCompileItems").lower() != "false"
    for tag, bucket in (("PackageReference", project.package_references), ("ProjectReference", project.project_references),
                        ("FrameworkReference", project.framework_references), ("Reference", project.other_references),
                        ("COMReference", project.other_references)):
        bucket.extend(e.get("Include") or "?" for e in root.iter(tag) if e.get("Include"))
    for element in root.iter("Using"):
        if element.get("Include"):
            project.usings_added.append(element.get("Include"))
        if element.get("Remove"):
            project.usings_removed.append(element.get("Remove"))
    return project


@lru_cache(maxsize=1)
def find_roslyn() -> Optional[tuple[str, str]]:
    """``(dotnet.exe, csc.dll)`` from the newest installed SDK, or None."""
    dotnet = which("dotnet")
    if not dotnet:
        return None
    try:
        listing = subprocess.run([dotnet, "--list-sdks"], capture_output=True, text=True, timeout=30,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    best = None
    for line in listing.splitlines():
        match = re.match(r"(\S+)\s+\[(.+)\]", line.strip())
        if not match:
            continue
        csc = os.path.join(match.group(2), match.group(1), "Roslyn", "bincore", "csc.dll")
        if os.path.isfile(csc):
            key = tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.-]", match.group(1))[:3])
            if best is None or key > best[0]:
                best = (key, csc)
    return (dotnet, best[1]) if best else None


def _version_key(text: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.-]", text)[:3])


def find_ref_pack(dotnet: str, pack: str, tfm: str) -> Optional[tuple[str, str]]:
    """``(ref folder, analyzers folder)`` for e.g. ``net8.0``. Prefers the
    exact framework; otherwise the nearest newer installed one."""
    match = re.fullmatch(r"net(\d+)\.(\d+)(-[\w.]+)?", tfm or "")
    if not match or int(match.group(1)) < 5:
        return None
    wanted = (int(match.group(1)), int(match.group(2)))
    base = os.path.join(os.path.dirname(dotnet), "packs", pack)
    try:
        versions = sorted(os.listdir(base), key=_version_key)
    except OSError:
        return None
    candidates = [v for v in versions if _version_key(v)[:2] >= wanted]
    for version in sorted(candidates, key=lambda v: (_version_key(v)[:2] != wanted, _version_key(v))):
        major_minor = ".".join(version.split(".")[:2])
        ref = os.path.join(base, version, "ref", f"net{major_minor}")
        if os.path.isdir(ref):
            return ref, os.path.join(base, version, "analyzers", "dotnet", "cs")
    return None


def unresolvable_reason(project: CsProject) -> Optional[str]:
    """Why the compiler can't be given this project's full references."""
    if project.package_references:
        return f"uses NuGet packages ({', '.join(project.package_references[:3])})"
    if project.project_references:
        return "references other projects"
    if project.other_references:
        return "has assembly/COM references"
    if not project.sdk.startswith("Microsoft.NET.Sdk"):
        return "is not an SDK-style .NET project (e.g. .NET Framework)"
    if project.sdk not in ("Microsoft.NET.Sdk", "Microsoft.NET.Sdk.Web"):
        return f"uses the {project.sdk} SDK"
    if project.use_wpf:
        return "uses WPF (XAML-generated code isn't available without a build)"
    if not project.default_compile_items:
        return "lists its compile items explicitly"
    return None


class CSharpCompilerAnalyzer(Analyzer):
    name = "csharp-compiler"
    display_name = "C# compiler check (Roslyn csc, no MSBuild)"
    languages = (CSHARP,)
    kind = "compiler"
    priority = 70
    reads_unsaved = True
    install_hint = "install the .NET SDK (https://dot.net)"

    def availability(self, profile) -> Availability:
        found = find_roslyn()
        return Availability(found is not None, found[1] if found else "no .NET SDK with the Roslyn compiler found")

    def run(self, context: RunContext, files: list[SourceFile]) -> AnalyzerRun:
        dotnet, csc = find_roslyn()  # type: ignore[misc]
        profile = context.profile
        projects = [parse_csproj(p) for p in profile.manifests if p.lower().endswith(".csproj")]
        result = AnalyzerRun()
        partial: list[str] = []
        assigned: set[str] = set()
        # Innermost project owns a file (handles nested project folders).
        for project in sorted(projects, key=lambda p: -len(p.directory)):
            own = [f.path for f in files if f.path not in assigned and is_within(f.path, project.directory)]
            assigned.update(own)
            if not own:
                continue
            reason = unresolvable_reason(project)
            references, analyzers, usings = self._references(dotnet, project)
            if reason is None and references is None:
                reason = f"targets {project.target_framework or 'an unknown framework'}, whose reference pack isn't installed"
            if reason is not None:
                partial.append(f"{os.path.basename(project.path)}: syntax only, because it {reason}")
            self._compile(context, dotnet, csc, own, project, references or [], analyzers or [], usings, reason is not None, result)
        loose = [f.path for f in files if f.path not in assigned]
        if loose:
            partial.append(f"{len(loose)} .cs file(s) outside any .csproj: syntax only")
            self._compile(context, dotnet, csc, loose, None, [], [], [], True, result)
        if partial:
            result.partial_reason = "; ".join(partial)
        return result

    @staticmethod
    def _references(dotnet: str, project: CsProject):
        packs = ["Microsoft.NETCore.App.Ref"]
        if project.sdk == "Microsoft.NET.Sdk.Web" or "Microsoft.AspNetCore.App" in project.framework_references:
            packs.append("Microsoft.AspNetCore.App.Ref")
        if project.use_winforms:
            packs.append("Microsoft.WindowsDesktop.App.Ref")
        references, analyzers = [], []
        for pack in packs:
            found = find_ref_pack(dotnet, pack, project.target_framework.replace("-windows", ""))
            if found is None:
                return None, None, []
            ref_dir, analyzer_dir = found
            references += [os.path.join(ref_dir, n) for n in sorted(os.listdir(ref_dir)) if n.endswith(".dll")]
            if os.path.isdir(analyzer_dir):
                # Microsoft's source generators from the SDK pack (regex,
                # JSON, interop); never analyzers from the project.
                analyzers += [os.path.join(analyzer_dir, n) for n in sorted(os.listdir(analyzer_dir)) if n.endswith(".dll")]
        usings: list[str] = []
        if project.implicit_usings:
            usings = list(_IMPLICIT_USINGS["Microsoft.NET.Sdk"])
            if project.sdk == "Microsoft.NET.Sdk.Web":
                usings += _IMPLICIT_USINGS["Microsoft.NET.Sdk.Web"]
            if project.use_winforms:
                usings += _IMPLICIT_USINGS["WindowsForms"]
        usings = [u for u in usings + project.usings_added if u not in project.usings_removed]
        return references, analyzers, usings

    def _compile(self, context, dotnet, csc, sources, project, references, analyzers, usings, syntax_only, result) -> None:
        temp = context.temp_dir
        os.makedirs(temp, exist_ok=True)
        name = os.path.splitext(os.path.basename(project.path))[0] if project else "loose"
        args = ["-nologo", f"-out:{os.path.join(temp, name + '.dll')}", "-nowarn:CS1591",
                "-t:exe" if (project and project.output_type.lower() in ("exe", "winexe")) else "-t:library",
                "-define:DEBUG;TRACE;NET;NETCOREAPP"]
        if project and project.nullable in ("enable", "warnings", "annotations"):
            args.append(f"-nullable:{project.nullable}")
        if project and project.allow_unsafe:
            args.append("-unsafe+")
        if references:
            args.append("-nostdlib+")
            args += [f"-r:{r}" for r in references]
            args += [f"-analyzer:{a}" for a in analyzers]
        if usings:
            global_usings = os.path.join(temp, f"{name}.GlobalUsings.g.cs")
            with open(global_usings, "w", encoding="utf-8") as handle:
                handle.write("".join(f"global using global::{u};\n" for u in dict.fromkeys(usings)))
            args.append(global_usings)
        args += [context.materialize(p) for p in sources]  # unsaved text -> temp copy
        response = os.path.join(temp, f"{name}.rsp")
        with open(response, "w", encoding="utf-8") as handle:
            handle.write("\n".join(_response_arg(a) for a in args))
        # -noconfig only works on the command line (not in a response file).
        completed = context.runner([dotnet, "exec", csc, "-noconfig", f"@{response}"], context.profile.root, context.timeout)
        output = completed.output
        spec = BuildToolSpec(name=self.name, command=["csc"], parser="msbuild", cwd=context.profile.root)
        parsed = parse_tool_output(spec, output)
        if completed.returncode != 0 and not parsed:
            # The compiler failed without reporting any source error (bad
            # option, crash): that is a failed check, never "no errors".
            result.error = (output.strip().splitlines() or ["csc failed"])[-1][:300]
            return
        for diagnostic in (d.with_changes(file=context.real_path(d.file)) for d in parsed):
            if diagnostic.file and not is_within(diagnostic.file, context.profile.root):
                continue  # e.g. the generated global-usings file
            if syntax_only and diagnostic.code not in SYNTAX_CODES:
                continue
            result.diagnostics.append(diagnostic.with_changes(source=self.name, language=CSHARP, application=None))
        result.checked_files.update(sources)


class DotnetBuildAnalyzer(Analyzer):
    name = "dotnet-build"
    display_name = "dotnet build (full MSBuild)"
    languages = (CSHARP,)
    kind = "compiler"
    priority = 90
    default_enabled = False
    consent_reason = "runs MSBuild, which executes the project's build targets and writes bin/ and obj/"
    min_interval_seconds = 120.0

    def availability(self, profile) -> Availability:
        path = which("dotnet")
        return Availability(path is not None, path or "the .NET SDK is not installed")

    def run(self, context: RunContext, files) -> AnalyzerRun:
        targets = [p for p in context.profile.manifests if p.lower().endswith(".sln")] or \
                  [p for p in context.profile.manifests if p.lower().endswith(".csproj")]
        result = AnalyzerRun()
        for target in targets[:1] if targets and targets[0].lower().endswith(".sln") else targets:
            output = context.runner([which("dotnet") or "dotnet", "build", target, "-nologo", "-clp:NoSummary", "-v:q"],
                                    context.profile.root, max(context.timeout, 600)).output
            spec = BuildToolSpec(name=self.name, command=["dotnet"], parser="msbuild", cwd=context.profile.root)
            result.diagnostics += [d.with_changes(source=self.name, language=CSHARP, application=None)
                                   for d in parse_tool_output(spec, output) if d.language == CSHARP]
        result.checked_files = {f.path for f in files}
        return result


def _response_arg(arg: str) -> str:
    """Quote one csc response-file argument (paths may contain spaces)."""
    if " " not in arg:
        return arg
    if arg.startswith("-") and ":" in arg:
        option, value = arg.split(":", 1)
        return f'{option}:"{value}"'
    return f'"{arg}"'
