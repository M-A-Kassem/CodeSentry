# CodeSentry

> A local Windows desktop utility that watches your code, your editor, and
> your screen — surfacing coding errors in real time with a floating
> counter, a tray app, and a structured incident log.

CodeSentry runs quietly in your system tray while you code. It analyzes
the source files in your project with the compilers and linters already
installed on your machine, reads diagnostics from Visual Studio and VS
Code, and (optionally) OCRs the foreground editor window — then shows
a single number: how many errors are open right now.

Everything stays on your machine. No cloud, no account, no telemetry.

---

## Features

- **Project source analysis.** Runs installed analyzers (`compile()`,
  Ruff, `csc`, `node --check`, `tsc`, `javac`, `gofmt`, …) on the source
  files of one project folder — the one you pick, or the one detected
  from the file open in your editor.
- **Visual Studio integration.** Reads the Error List of every running
  Visual Studio instance through EnvDTE automation (COM), out of
  process. No extension needed.
- **VS Code integration.** A companion extension forwards
  `vscode.languages.getDiagnostics()` (the Problems panel) over a
  localhost-only, token-authenticated bridge.
- **Unsaved buffers in Visual Studio.** The text of documents with
  unsaved changes is read read-only through the VS automation API and
  analyzed instead of the file on disk.
- **Screen detection (opt-in).** OCRs the foreground editor window for
  editors without a direct integration (JetBrains IDEs, Sublime,
  Notepad++, Eclipse, Zed). Pattern matching, best effort.
- **Floating overlay.** A small always-on-top counter: `ERROR 2 · W 3 ·
  +1 new`. Click to open the issue list, drag to move, right-click for
  menu.
- **System tray.** Grey/green/red icon reflecting monitoring state and
  error count. Hover for a tooltip; right-click for the menu.
- **Incident logger (original mode).** Captures a screenshot plus a
  structured JSON record when an error happens — manually, from a global
  hotkey, from a Python script, or from a Windows Event Log entry.
- **Rule-based explanations.** Each issue gets a short, local explanation
  tagged with its confidence (`[likely]`, `[uncertain]`, or none).
- **Never modifies your code.** Compilers write only to a private temp
  folder; Ruff runs with `--no-cache`; nothing is installed on your
  behalf.

---

## Requirements

- **Windows 10/11** (uses Win32 APIs, EnvDTE, the Windows Event Log).
- **Python 3.10+**.
- **Optional analyzers** (used only if present on `PATH`): the .NET SDK
  (`csc`, `dotnet`), Ruff, Pyright, Node.js (`node --check`), TypeScript
  (`tsc`), `javac`, `gofmt`, clang/gcc.
- **Optional OCR**: Tesseract via `pytesseract` (see
  [`requirements-ocr.txt`](requirements-ocr.txt)).

---

## Install

```powershell
git clone https://github.com/<your-user>/CodeSentry.git
cd CodeSentry

py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt

# Optional: screen-detection / OCR dependencies
pip install -r requirements-ocr.txt

# Optional: dev tooling (ruff, pytest)
pip install -r requirements-dev.txt
```

Copy the example configuration (first run also generates one):

```powershell
copy config.example.json config.json
```

---

## Run

```powershell
# Tray app with the coding error monitor + overlay (default)
python main.py

# Original incident window only, no tray
python main.py --classic

# Headless Windows Event Log monitor
python main.py --monitor

# Ask a running --monitor process to stop
python main.py --stop-monitor
```

### VS Code companion extension

Install once (requires Node.js):

```powershell
cd vscode-extension
npx @vscode/vsce package --allow-missing-repository --skip-license
code --install-extension error-counter-bridge-0.1.0.vsix
```

See [`vscode-extension/README.md`](vscode-extension/README.md) for the
bridge security model (loopback-only, exclusive-port bind, per-session
token, no file content transmitted).

---

## Configuration

All settings live in `config.json` at the project root. A full example
with every supported key is in
[`config.example.json`](config.example.json). The highlights:

| Key | What it does |
|---|---|
| `project_path` | Project folder to analyze. `null` → auto-detect from the editor foreground file. |
| `project_exclude_dirs` | Extra directory names to skip (on top of the built-in `.git`, `node_modules`, `bin`, `obj`, …). |
| `project_poll_seconds` | How often file timestamps are rescanned (default `3.0`). |
| `analyzers` | Toggle individual analyzers (e.g. `{"ruff": false, "dotnet-build": true}`). |
| `build_tools` | Extra command-line tools to run (compilers, linters). Argument lists, no shell. |
| `vscode_bridge_enabled` | Accept diagnostics from the VS Code companion extension. |
| `visual_studio_enabled` | Poll the Visual Studio Error List over EnvDTE. |
| `screen_detection_enabled` | OCR the foreground editor window (opt-in). |
| `overlay_enabled` | Show the floating error counter. |
| `auto_monitor_enabled` | Start monitoring whenever a known editor becomes foreground. |
| `screenshot_on_new_coding_error` | Save a screenshot into incident history on each new error. |

---

## Architecture

```
screenshot_logger/
├── config.py              # Load/save config, logging setup, data dirs
├── storage.py             # JSON incident storage
├── screenshot_service.py  # pyautogui screen capture
├── incident_logger.py     # Record a manual / scripted incident
├── hotkey_service.py      # Global hotkey (optional, via keyboard pkg)
├── event_monitor.py       # Windows Event Log polling
├── win_display.py         # DPI, single-instance, HWND helpers
├── gui.py                 # Classic incident window (Tk)
├── overlay.py             # Floating always-on-top counter
├── tray_app.py            # pystray tray icon, settings window
├── issues_window.py       # Issue list (Current / New / History)
└── coding/
    ├── controller.py      # Orchestrates providers, debounces, dedupes
    ├── issue_tracker.py   # Current / new / stale bookkeeping
    ├── dedupe.py          # Collapse the same issue reported twice
    ├── explain.py         # Rule-based explanations
    ├── diagnostic.py      # Normalized Issue dataclass
    ├── project.py         # Scan project, respect excludes
    ├── app_detector.py    # Which editor is foreground
    ├── analyzers/         # python, csharp, native, web, data, …
    └── providers/
        ├── project_analysis.py   # Primary: runs analyzers on files
        ├── visual_studio.py      # EnvDTE / COM, out of process
        ├── vscode_bridge.py      # Localhost HTTP bridge
        ├── build_tools.py        # User-configured CLIs
        └── screen_detector.py    # OCR + pattern matching
```

---

## Tests

```powershell
pytest -q
```

The suite covers the issue tracker, controller, project analysis, build
tools, Visual Studio and VS Code providers, screen detection, unsaved
buffers, and the incident logger. Analyzers that need external toolchains
not installed on the CI are mocked.

---

## Privacy & security

- **No network use** except the loopback bridge for the VS Code
  extension — bound to `127.0.0.1`, exclusive port, random per-session
  token, non-loopback or browser-originated requests refused.
- **No file contents transmitted.** The bridge forwards diagnostic
  metadata only.
- **No shells, no interpreter calls.** Analyzer and build-tool commands
  are argument lists, never shell strings.
- **Nothing is installed on your behalf.** Missing tools are reported,
  never auto-fetched.

---

## License

MIT — see [`LICENSE`](LICENSE).

