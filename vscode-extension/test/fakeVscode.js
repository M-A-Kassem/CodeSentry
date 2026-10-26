'use strict';
// A minimal stand-in for the `vscode` module: just enough API surface for
// extension.js, so the real extension code can be activated in plain Node.
// This verifies our own wiring; it is NOT a substitute for running inside
// a real VS Code (see README for the manual check).
const Module = require('module');

function uri(fsPath, scheme = 'file') {
  return { scheme, fsPath, toString: () => `${scheme}:///${fsPath.replace(/\\/g, '/')}` };
}

function createFakeVscode({ diagnostics = [], config = {}, documents = [] } = {}) {
  const listeners = { diagnostics: [], windowState: [], editor: [], config: [] };
  const on = (key) => (fn) => {
    listeners[key].push(fn);
    return { dispose() {} };
  };
  const vscode = {
    _state: { diagnostics, config, documents, listeners, outputLines: [], statusItem: null },
    DiagnosticSeverity: { Error: 0, Warning: 1, Information: 2, Hint: 3 },
    StatusBarAlignment: { Left: 1, Right: 2 },
    env: { sessionId: 'test-session', appName: 'Visual Studio Code (fake)' },
    languages: {
      getDiagnostics: () => vscode._state.diagnostics,
      onDidChangeDiagnostics: on('diagnostics'),
    },
    window: {
      state: { focused: true },
      activeTextEditor: undefined,
      onDidChangeWindowState: on('windowState'),
      onDidChangeActiveTextEditor: on('editor'),
      createOutputChannel: () => ({
        appendLine: (line) => vscode._state.outputLines.push(line),
        show() {},
        dispose() {},
      }),
      createStatusBarItem: () => {
        const item = { text: '', tooltip: '', command: '', show() {}, dispose() {} };
        vscode._state.statusItem = item;
        return item;
      },
    },
    workspace: {
      name: 'fake-workspace',
      get textDocuments() { return vscode._state.documents; },
      getConfiguration: () => ({ get: (key, fallback) => (key in vscode._state.config ? vscode._state.config[key] : fallback) }),
      onDidChangeConfiguration: on('config'),
    },
    commands: { registerCommand: () => ({ dispose() {} }) },
    fireDiagnosticsChanged() {
      for (const fn of listeners.diagnostics) fn({ uris: [] });
    },
  };
  return vscode;
}

/** Load ../extension.js with `require('vscode')` resolved to `fake`. */
function loadExtensionWith(fake) {
  const originalLoad = Module._load;
  Module._load = function patched(request, parent, isMain) {
    if (request === 'vscode') return fake;
    return originalLoad.call(this, request, parent, isMain);
  };
  try {
    const path = require.resolve('../extension.js');
    delete require.cache[path];
    return require(path);
  } finally {
    Module._load = originalLoad;
  }
}

function diagnostic(severity, message, line, character, extra = {}) {
  return { severity, message, range: { start: { line, character } }, ...extra };
}

module.exports = { createFakeVscode, loadExtensionWith, uri, diagnostic };
