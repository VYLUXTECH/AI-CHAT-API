// Harvest Qwen session (token + cookies + UA) from the local Chrome via CDP.
const http = require('http');
const WebSocket = require('/tmp/opencode/node_modules/ws');

function getJson(path) {
  return new Promise((res, rej) => {
    http.get({ host: '127.0.0.1', port: 9222, path }, (r) => {
      let b = ''; r.on('data', (c) => b += c); r.on('end', () => res(JSON.parse(b)));
    }).on('error', rej);
  });
}

(async () => {
  const targets = await getJson('/json');
  const page = targets.find(t => t.type === 'page' && t.url.includes('chat.qwen.ai'));
  if (!page) { console.error('no qwen page target', targets.map(t => t.url)); process.exit(1); }
  const ws = new WebSocket(page.webSocketDebuggerUrl, { maxPayload: 100 * 1024 * 1024 });
  let mid = 0;
  const call = (method, params) => new Promise((res, rej) => {
    const id = ++mid;
    ws.send(JSON.stringify({ id, method, params }));
    const onMsg = (d) => { const m = JSON.parse(d); if (m.id === id) { ws.off('message', onMsg); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } };
    ws.on('message', onMsg);
  });
  ws.on('open', async () => {
    try {
      const tok = await call('Runtime.evaluate', { expression: "localStorage.getItem('token') || ''", returnByValue: true });
      const ua = await call('Runtime.evaluate', { expression: 'navigator.userAgent', returnByValue: true });
      const ck = await call('Network.getCookies', { urls: ['https://chat.qwen.ai/'] });
      const out = {
        token: tok.result.value || '',
        ua: ua.result.value || '',
        cookies: (ck.cookies || []).filter(c => c.value).map(c => `${c.name}=${c.value}`).join('; '),
      };
      console.log(JSON.stringify(out, null, 1));
    } catch (e) { console.error('ERR', e.message); }
    ws.close(); process.exit(0);
  });
  ws.on('error', (e) => { console.error('WS ERR', e.message); process.exit(1); });
})();
