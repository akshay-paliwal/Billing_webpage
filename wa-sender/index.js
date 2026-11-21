// Sends WhatsApp messages through WhatsApp Web, linked to your phone like any
// linked device. Started automatically by app.py; listens on localhost only.
//   GET  /status -> { state: "starting" | "qr" | "linking" | "ready" | "disconnected", qr?, me? }
//   POST /send   { to: "919876543210", text } -> { ok, error? }
//   POST /logout  unlink this WhatsApp account and show a new QR code
const fs = require('fs');
const http = require('http');
const path = require('path');
const QRCode = require('qrcode');
const { Client, LocalAuth } = require('whatsapp-web.js');

const PORT = Number(process.env.WA_SENDER_PORT || 3001);
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome';

const PROFILE = path.join(__dirname, '.wwebjs_auth', 'session');

// "show_browser": true in whatsapp_config.json runs Chrome as a normal window (opened once).
const SHOW_BROWSER = (() => {
  try { return !!JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'whatsapp_config.json'))).show_browser; }
  catch { return false; }
})();

// If the app was closed abruptly last time, its hidden Chrome may still hold the
// login profile. Find it through Chrome's lock file and stop it.
function killLeftoverChrome() {
  try {
    const pid = Number(fs.readlinkSync(path.join(PROFILE, 'SingletonLock')).split('-').pop());
    const cmd = fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8');
    if (cmd.includes('chrome') && cmd.includes(PROFILE)) {
      process.kill(pid, 'SIGTERM');
      console.log(`[wa] stopped leftover Chrome (pid ${pid})`);
    }
  } catch { /* no lock or process already gone */ }
}

let state = 'starting';
let qrDataUrl = null;
let me = null;

let client = null;
let restarting = false;

function startClient() {
  state = 'starting';
  qrDataUrl = null;
  me = null;
  const c = new Client({
    // keeps the login in wa-sender/.wwebjs_auth so the QR is scanned only once
    authStrategy: new LocalAuth({ dataPath: path.join(__dirname, '.wwebjs_auth') }),
    puppeteer: { headless: !SHOW_BROWSER, executablePath: CHROME, args: ['--no-sandbox', '--disable-dev-shm-usage'] },
  });
  c.on('qr', async qr => {
    state = 'qr';
    qrDataUrl = await QRCode.toDataURL(qr, { margin: 1, width: 280 });
    console.log('[wa] scan the QR code on the billing page');
  });
  c.on('authenticated', () => { state = 'linking'; qrDataUrl = null; console.log('[wa] QR scanned, logging in…'); });
  c.on('loading_screen', (pct, msg) => console.log(`[wa] loading chats ${pct}% ${msg || ''}`));
  c.on('change_state', st => console.log('[wa] connection state:', st));
  c.on('ready', () => {
    state = 'ready';
    qrDataUrl = null;
    me = c.info?.wid?.user || null;
    console.log(`[wa] ready, sending as +${me}`);
  });
  c.on('auth_failure', m => { state = 'qr'; console.log('[wa] auth failure:', m); });
  c.on('disconnected', reason => {
    console.log('[wa] disconnected:', reason);
    if (!restarting) restartClient(reason === 'LOGOUT');  // e.g. unlinked from the phone
  });
  client = c;
  c.initialize().catch(e => { state = 'disconnected'; console.log('[wa] failed to start:', e.message); });
}

// Close the current session and start fresh; with forget=true the saved login is
// removed so a new QR code (for any WhatsApp account) is shown.
async function restartClient(forget) {
  if (restarting) return;
  restarting = true;
  state = 'starting';
  me = null;
  const old = client;
  if (forget) { try { await old.logout(); } catch {} }  // also removes it from the phone's Linked devices
  try { await old.destroy(); } catch {}
  if (forget) fs.rmSync(PROFILE, { recursive: true, force: true });
  restarting = false;
  startClient();
}

async function send(to, text) {
  if (state !== 'ready') return { ok: false, error: 'WhatsApp is not connected (scan the QR code on the billing page)' };
  const id = await client.getNumberId(to);
  if (!id) return { ok: false, error: `+${to} is not on WhatsApp` };
  await client.sendMessage(id._serialized, text);
  return { ok: true };
}

function reply(res, code, body) {
  res.writeHead(code, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(body));
}

http.createServer((req, res) => {
  if (req.method === 'GET' && req.url === '/status') {
    return reply(res, 200, { state, qr: qrDataUrl, me });
  }
  if (req.method === 'POST' && req.url === '/logout') {
    console.log('[wa] switching account: unlinking +' + me);
    restartClient(true);
    return reply(res, 200, { ok: true });
  }
  if (req.method === 'POST' && req.url === '/send') {
    let body = '';
    req.on('data', c => { body += c; });
    req.on('end', async () => {
      try {
        const { to, text } = JSON.parse(body);
        reply(res, 200, await send(String(to).replace(/\D/g, ''), text));
      } catch (e) {
        reply(res, 200, { ok: false, error: e.message });
      }
    });
    return;
  }
  reply(res, 404, { error: 'not found' });
}).listen(PORT, '127.0.0.1', () => console.log(`[wa] helper on http://127.0.0.1:${PORT} (${SHOW_BROWSER ? 'visible window' : 'hidden'})`));

// Close Chrome with us, and quit if the billing app that started us goes away.
let stopping = false;
async function shutdown() {
  if (stopping) return;
  stopping = true;
  try { await client?.destroy(); } catch {}
  process.exit(0);
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
const parent = process.ppid;
setInterval(() => { try { process.kill(parent, 0); } catch { shutdown(); } }, 3000);

// errors thrown inside the WhatsApp library (e.g. while logging out) must not kill the helper
process.on('unhandledRejection', e => console.log('[wa] ignored error:', e?.message || e));

killLeftoverChrome();
setTimeout(startClient, 1000);
