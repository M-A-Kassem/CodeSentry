# Error Counter Bridge (VS Code companion extension)

Sends VS Code's diagnostics, exactly what the **Problems** panel shows,
to the Screenshot & Error Logger tray app so its `ERROR n` overlay counts
them. Diagnostics come from whatever language extensions you already use
(Pylance, the C# Dev Kit, TypeScript, ESLint, rust-analyzer, ...); this
extension adds no language support of its own.

**What is sent:** severity, message, file path, line/column, source
(e.g. `Pylance`), code, and language id. **Never** file contents.
**Where:** only to `127.0.0.1`, authenticated with a random token that the
tray app writes to `%LOCALAPPDATA%\ScreenshotErrorLogger\vscode-bridge.json`
each time monitoring starts. If the tray app isn't monitoring, the
extension does nothing (the status bar shows a disconnected icon).

## Install

No dependencies and no build step. From this folder:

```powershell
# Option A: package and install (needs Node.js)
npx @vscode/vsce package --allow-missing-repository --skip-license
code --install-extension error-counter-bridge-0.1.0.vsix

# Option B: try it without installing
code --extensionDevelopmentPath="$PWD"
```

The same `.vsix` should also work in VS Code forks that run VS Code
extensions (Cursor, VSCodium, Windsurf), but that has not been tested.

## Settings

| Setting | Default | |
|---|---|---|
| `errorCounterBridge.enabled` | `true` | Turn sending on/off |
| `errorCounterBridge.includeWarnings` | `true` | Errors are always sent |
| `errorCounterBridge.includeInformation` | `false` | Hints are never sent |
| `errorCounterBridge.discoveryFile` | `""` | Override the discovery file path |

Commands: **Error Counter Bridge: Send Diagnostics Now**, **Error Counter
Bridge: Show Log**. Click the `Errors→Tray` status bar item to see the log.

## Tests

```powershell
npm test     # node --test; runs extension.js against a fake `vscode` module
```
