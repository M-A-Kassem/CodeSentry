'use strict';
// Used by the Python test suite (tests/test_vscode_bridge.py): activates
// the real extension.js against a fake `vscode` module and lets it talk to
// the real Python bridge whose discovery file is given as argv[2].
//
// Interactive so one "window" can be checked mid-way: it sends errors,
// prints "SENT <status bar text>", waits for a line on stdin, then
// simulates the user fixing the code, deactivates (goodbye) and exits.
const readline = require('readline');
const { createFakeVscode, loadExtensionWith, uri, diagnostic } = require('./fakeVscode');

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const withErrors = [[uri('C:\\proj\\main.py'), [
    diagnostic(0, '"undefined_thing" is not defined', 3, 4, { source: 'Pylance', code: { value: 'reportUndefinedVariable' } }),
    diagnostic(1, 'Import "os" is not accessed', 0, 7, { source: 'Pylance' }),
  ]]];
  // "clean-first": the window connects with no problems and the user then
  // introduces an error, so it counts as new rather than pre-existing.
  const cleanFirst = process.argv[3] === 'clean-first';
  const fake = createFakeVscode({
    config: { discoveryFile: process.argv[2] },
    diagnostics: cleanFirst ? [] : withErrors,
  });
  const extension = loadExtensionWith(fake);
  extension.activate({ subscriptions: [] });
  await wait(800);
  if (cleanFirst) {
    fake._state.diagnostics = withErrors;
    fake.fireDiagnosticsChanged();
    await wait(800);
  }
  process.stdout.write(`SENT ${fake._state.statusItem.text}\n`);

  const lines = readline.createInterface({ input: process.stdin });
  await new Promise((resolve) => lines.once('line', resolve));
  fake._state.diagnostics = [[uri('C:\\proj\\main.py'), [
    diagnostic(1, 'Import "os" is not accessed', 0, 7, { source: 'Pylance' }),
  ]]];
  fake.fireDiagnosticsChanged();
  await wait(800);
  process.stdout.write('FIXED\n');

  await new Promise((resolve) => lines.once('line', resolve));
  await extension.deactivate();
  process.stdout.write('BYE\n');
  process.exit(0);
}

main();
