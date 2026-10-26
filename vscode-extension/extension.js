'use strict';
// Error Counter Bridge: publishes VS Code's diagnostics -- exactly what the
// Problems panel shows, from every installed language extension -- to the
// local Screenshot & Error Logger tray app. Only diagnostic metadata
// (severity, message, file path, position, source, code) is sent, only to
// 127.0.0.1, and only when the tray app's discovery file exists.
const vscode = require('vscode');
const { buildPayload } = require('./lib/payload');
const { BridgeClient, defaultDiscoveryPath } = require('./lib/bridgeClient');

const DEBOUNCE_MS = 400;
const HEARTBEAT_MS = 15000; // the tray app marks a window stale after 45 s

let client;
let clientId;
let output;
let statusItem;
let timer;
let heartbeat;
let lastConnected;

function settings() {
  return vscode.workspace.getConfiguration('errorCounterBridge');
}

function log(message) {
  if (output) output.appendLine(`[${new Date().toISOString()}] ${message}`);
}

function languageLookup() {
  const languages = new Map();
  for (const document of vscode.workspace.textDocuments) {
    languages.set(document.uri.toString(), document.languageId);
  }
  return (uri) => languages.get(uri.toString());
}

async function sendSnapshot() {
  if (!client || !settings().get('enabled', true)) return;
  const editor = vscode.window.activeTextEditor;
  const payload = buildPayload(vscode.languages.getDiagnostics(), {
    clientId,
    appName: vscode.env.appName,
    workspace: vscode.workspace.name,
    focused: vscode.window.state.focused,
    activeFile: editor && editor.document.uri.scheme === 'file' ? editor.document.uri.fsPath : undefined,
    includeWarnings: settings().get('includeWarnings', true),
    includeInformation: settings().get('includeInformation', false),
    languageOf: languageLookup(),
  });
  const result = await client.send('/v1/diagnostics', payload);
  updateStatus(result.ok, result.ok ? `${payload.diagnostics.length} diagnostic(s) sent` : result.body);
}

function updateStatus(connected, detail) {
  if (connected !== lastConnected) log(connected ? 'Connected to tray app.' : `Not connected: ${detail}`);
  lastConnected = connected;
  statusItem.text = connected ? '$(pulse) Errors→Tray' : '$(debug-disconnect) Errors→Tray';
  statusItem.tooltip = connected
    ? `Error Counter Bridge: connected (${detail})`
    : `Error Counter Bridge: not connected — start monitoring in the tray app. (${detail})`;
}

function schedule() {
  clearTimeout(timer);
  timer = setTimeout(() => sendSnapshot().catch((err) => log(`send failed: ${err}`)), DEBOUNCE_MS);
}

function activate(context) {
  output = vscode.window.createOutputChannel('Error Counter Bridge');
  // One id per window: each window has its own extension host process.
  clientId = `${vscode.env.sessionId}-${process.pid}`;
  client = new BridgeClient(settings().get('discoveryFile') || defaultDiscoveryPath());
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 0);
  statusItem.command = 'errorCounterBridge.showLog';
  updateStatus(false, 'starting');
  statusItem.show();

  context.subscriptions.push(
    output,
    statusItem,
    vscode.languages.onDidChangeDiagnostics(schedule),
    vscode.window.onDidChangeWindowState(schedule),
    vscode.window.onDidChangeActiveTextEditor(schedule),
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (event.affectsConfiguration('errorCounterBridge')) {
        client = new BridgeClient(settings().get('discoveryFile') || defaultDiscoveryPath());
        schedule();
      }
    }),
    vscode.commands.registerCommand('errorCounterBridge.sendNow', () => sendSnapshot()),
    vscode.commands.registerCommand('errorCounterBridge.showLog', () => output.show()),
  );
  heartbeat = setInterval(schedule, HEARTBEAT_MS);
  context.subscriptions.push({ dispose: () => clearInterval(heartbeat) });
  schedule();
}

function deactivate() {
  clearTimeout(timer);
  clearInterval(heartbeat);
  if (client) return client.send('/v1/goodbye', { client: { id: clientId } });
  return undefined;
}

module.exports = { activate, deactivate };
