'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');

const { buildPayload, severityName, codeText } = require('../lib/payload');
const { readDiscovery, defaultDiscoveryPath } = require('../lib/bridgeClient');
const { createFakeVscode, loadExtensionWith, uri, diagnostic } = require('./fakeVscode');

test('severity names map VS Code enum values', () => {
  assert.equal(severityName(0), 'error');
  assert.equal(severityName(1), 'warning');
  assert.equal(severityName(2), 'info');
  assert.equal(severityName(3), 'hint');
  assert.equal(severityName(99), 'info');
});

test('code text handles string, number and {value, target} forms', () => {
  assert.equal(codeText('E501'), 'E501');
  assert.equal(codeText(2322), '2322');
  assert.equal(codeText({ value: 'reportUndefinedVariable', target: {} }), 'reportUndefinedVariable');
  assert.equal(codeText(undefined), undefined);
});

test('payload converts positions to 1-based and filters by severity and scheme', () => {
  const file = uri('C:\\proj\\main.py');
  const payload = buildPayload(
    [
      [file, [
        diagnostic(0, 'undefined name x', 4, 2, { source: 'Pylance', code: 'reportUndefinedVariable' }),
        diagnostic(1, 'unused import', 0, 0),
        diagnostic(2, 'info message', 1, 0),
        diagnostic(3, 'hint', 1, 0),
      ]],
      [uri('C:\\proj\\main.py', 'git'), [diagnostic(0, 'from a git diff view', 0, 0)]],
    ],
    { clientId: 'c1', languageOf: () => 'python' },
  );
  assert.equal(payload.client.id, 'c1');
  assert.deepEqual(payload.diagnostics.map((d) => d.severity), ['error', 'warning']);
  const [error] = payload.diagnostics;
  assert.equal(error.line, 5);
  assert.equal(error.column, 3);
  assert.equal(error.file, 'C:\\proj\\main.py');
  assert.equal(error.code, 'reportUndefinedVariable');
  assert.equal(error.language, 'python');
});

test('payload can exclude warnings and caps the number of diagnostics', () => {
  const many = Array.from({ length: 10 }, (_, i) => diagnostic(0, `e${i}`, i, 0));
  const payload = buildPayload([[uri('C:\\a.ts'), [...many, diagnostic(1, 'w', 0, 0)]]], {
    clientId: 'c', includeWarnings: false, maxDiagnostics: 5,
  });
  assert.equal(payload.diagnostics.length, 5);
  assert.equal(payload.truncated, true);
  assert.ok(payload.diagnostics.every((d) => d.severity === 'error'));
});

test('payload never contains document text fields', () => {
  const payload = buildPayload([[uri('C:\\a.js'), [diagnostic(0, 'boom', 0, 0, { relatedInformation: [{ message: 'x' }] })]]], { clientId: 'c' });
  assert.deepEqual(Object.keys(payload.diagnostics[0]).sort(), ['code', 'column', 'file', 'language', 'line', 'message', 'severity', 'source']);
});

test('discovery file is read from LOCALAPPDATA and validated', () => {
  assert.equal(
    defaultDiscoveryPath({ LOCALAPPDATA: 'C:\\Users\\me\\AppData\\Local' }),
    path.join('C:\\Users\\me\\AppData\\Local', 'ScreenshotErrorLogger', 'vscode-bridge.json'),
  );
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-'));
  const file = path.join(dir, 'd.json');
  assert.equal(readDiscovery(file), null);
  fs.writeFileSync(file, JSON.stringify({ port: 'nope', token: 't' }));
  assert.equal(readDiscovery(file), null);
  fs.writeFileSync(file, JSON.stringify({ port: 1234, token: 'secret' }));
  assert.deepEqual(readDiscovery(file), { port: 1234, token: 'secret' });
});

test('activated extension posts diagnostics with the bearer token, then says goodbye', async () => {
  const received = [];
  const server = http.createServer((req, res) => {
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', () => {
      received.push({ path: req.url, auth: req.headers.authorization, origin: req.headers.origin, body: JSON.parse(body) });
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('{"ok":true}');
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-'));
  const discoveryFile = path.join(dir, 'vscode-bridge.json');
  fs.writeFileSync(discoveryFile, JSON.stringify({ port: server.address().port, token: 'tok123' }));

  const fake = createFakeVscode({
    config: { discoveryFile },
    diagnostics: [[uri('C:\\proj\\app.ts'), [diagnostic(0, "Type 'string' is not assignable to type 'number'.", 9, 4, { source: 'ts', code: 2322 })]]],
  });
  const extension = loadExtensionWith(fake);
  const subscriptions = [];
  extension.activate({ subscriptions });
  await new Promise((r) => setTimeout(r, 700)); // debounce is 400 ms

  fake._state.diagnostics = [];
  fake.fireDiagnosticsChanged();
  await new Promise((r) => setTimeout(r, 700));
  await extension.deactivate();
  server.close();

  const snapshots = received.filter((r) => r.path === '/v1/diagnostics');
  assert.ok(snapshots.length >= 2, `expected two snapshots, got ${snapshots.length}`);
  assert.equal(snapshots[0].auth, 'Bearer tok123');
  assert.equal(snapshots[0].origin, undefined);
  assert.equal(snapshots[0].body.diagnostics[0].code, '2322');
  assert.equal(snapshots[0].body.diagnostics[0].line, 10);
  assert.equal(snapshots.at(-1).body.diagnostics.length, 0, 'fixed errors must be reported as gone');
  assert.equal(received.at(-1).path, '/v1/goodbye');
  assert.match(fake._state.statusItem.text, /pulse/);
});
