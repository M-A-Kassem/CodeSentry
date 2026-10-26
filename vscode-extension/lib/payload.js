'use strict';
// Pure functions that turn VS Code diagnostics into the bridge payload.
// Kept free of the `vscode` module so they can be unit-tested with plain
// Node. Only diagnostic metadata is included, never document text.

// vscode.DiagnosticSeverity: Error = 0, Warning = 1, Information = 2, Hint = 3
const SEVERITY_NAMES = ['error', 'warning', 'info', 'hint'];
const SUPPORTED_SCHEMES = new Set(['file', 'untitled', 'vscode-notebook-cell']);
const MAX_DIAGNOSTICS = 5000;
const MAX_MESSAGE_CHARS = 2000;

function severityName(severity) {
  return SEVERITY_NAMES[severity] || 'info';
}

function codeText(code) {
  if (code === undefined || code === null) return undefined;
  if (typeof code === 'object') return code.value === undefined ? undefined : String(code.value);
  return String(code);
}

/**
 * @param {Array<[{scheme: string, fsPath: string, toString(): string}, Array<object>]>} entries
 *   The result of `vscode.languages.getDiagnostics()`.
 * @param {object} options
 */
function buildPayload(entries, options) {
  const {
    clientId,
    appName = 'Visual Studio Code',
    workspace,
    focused = false,
    activeFile,
    includeWarnings = true,
    includeInformation = false,
    languageOf = () => undefined,
    maxDiagnostics = MAX_DIAGNOSTICS,
  } = options;

  const diagnostics = [];
  let truncated = false;
  for (const [uri, list] of entries) {
    if (!uri || !SUPPORTED_SCHEMES.has(uri.scheme)) continue; // e.g. git:, output:
    for (const d of list || []) {
      const severity = severityName(d.severity);
      if (severity === 'hint') continue;
      if (severity === 'info' && !includeInformation) continue;
      if (severity === 'warning' && !includeWarnings) continue;
      if (diagnostics.length >= maxDiagnostics) {
        truncated = true;
        break;
      }
      const start = (d.range && d.range.start) || { line: 0, character: 0 };
      diagnostics.push({
        file: uri.scheme === 'file' ? uri.fsPath : uri.toString(),
        line: start.line + 1, // VS Code positions are 0-based
        column: start.character + 1,
        severity,
        message: String(d.message || '').slice(0, MAX_MESSAGE_CHARS),
        source: d.source || undefined,
        code: codeText(d.code),
        language: languageOf(uri),
      });
    }
  }
  return {
    client: { id: clientId, app: 'vscode', appName, workspace, focused },
    activeFile,
    truncated,
    diagnostics,
  };
}

module.exports = { buildPayload, severityName, codeText, MAX_DIAGNOSTICS };
