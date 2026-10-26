'use strict';
// Talks to the tray app's local bridge. The port and token are re-read
// from the discovery file on every send, because the tray app picks a new
// port and token each time monitoring starts.
const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');

function defaultDiscoveryPath(env = process.env) {
  const base = env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local');
  return path.join(base, 'ScreenshotErrorLogger', 'vscode-bridge.json');
}

function readDiscovery(file) {
  try {
    const data = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (Number.isInteger(data.port) && data.port > 0 && typeof data.token === 'string' && data.token) {
      return { port: data.port, token: data.token };
    }
  } catch (err) {
    // Missing file = tray app not monitoring. Not an error worth logging.
  }
  return null;
}

function postJson(discovery, route, body, timeoutMs = 3000) {
  return new Promise((resolve) => {
    const data = Buffer.from(JSON.stringify(body), 'utf8');
    const request = http.request(
      {
        host: '127.0.0.1', // loopback only, never a hostname lookup
        port: discovery.port,
        path: route,
        method: 'POST',
        timeout: timeoutMs,
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': data.length,
          Authorization: `Bearer ${discovery.token}`,
        },
      },
      (response) => {
        let text = '';
        response.setEncoding('utf8');
        response.on('data', (chunk) => { text += chunk; });
        response.on('end', () => resolve({ ok: response.statusCode === 200, status: response.statusCode, body: text }));
      },
    );
    request.on('timeout', () => request.destroy(new Error('timeout')));
    request.on('error', (err) => resolve({ ok: false, status: 0, body: String(err.message || err) }));
    request.end(data);
  });
}

class BridgeClient {
  constructor(discoveryFile = defaultDiscoveryPath()) {
    this.discoveryFile = discoveryFile;
  }

  async send(route, payload) {
    const discovery = readDiscovery(this.discoveryFile);
    if (!discovery) return { ok: false, status: 0, body: 'tray app is not monitoring (no discovery file)' };
    return postJson(discovery, route, payload);
  }
}

module.exports = { BridgeClient, defaultDiscoveryPath, readDiscovery, postJson };
