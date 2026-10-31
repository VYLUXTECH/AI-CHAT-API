#!/usr/bin/env node
// AI-CHAT-API — container port of VYLUXTECH/AI-CHAT-API (aichatapi.py).
// Headless OpenAI-compatible bridge over chat.qwen.ai, zero npm deps.
//
// The Python original bootstrapped sessions through a visible Chrome (CDP).
// This port has no browser: the Qwen JWT (and optional cookie jar) are read
// from state.json / env — refresh them via SFTP or dashboard when they expire.
// ChatGPT is not portable headless (sentinel PoW needs a live page) — those
// model ids stay in the catalog but answer with a clear error.
//
// Requests flow (proven order — skipping the 2nd users/status => CHAT_NOT_FOUND):
//   1. POST /api/v2/users/status            (no chat id)
//   2. POST /api/v2/chats/new               -> data.id
//   3. POST /api/v2/users/status            (with the chat id)
//   4. POST /api/v2/chat/completions?chat_id= (SSE reply)

const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const PORT = parseInt(process.env.PORT || '21690', 10);
const API_KEY = (process.env.BRIDGE_API_KEY || '').trim(); // optional Bearer for OUR api
const STATE_FILE = path.join(__dirname, 'state.json');
const QWEN_MODEL = process.env.AICHAT_QWEN_MODEL || 'qwen3.7-plus';
const TIMEOUT = parseFloat(process.env.AICHAT_TIMEOUT || '240') * 1000;
const QWEN_BASE = 'https://chat.qwen.ai';
const UA = process.env.AICHAT_UA || 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36';

const VERSION = '0.3.27';
const ORGID_ARMS = {
  typarm1: 'web', typarm2: '9ecc74ad-09a0-4fad-8a44-663978aa37c0',
  typarm3: 'prod', typarm4: 'qwen_chat', typarm5: 'product', typarm6: '',
};

// state.json: { token: "eyJ...", cookies: "cf_clearance=...; ...", ua: "" }
// Read lazily on every session build so an SFTP refresh needs no restart.
function loadState() {
  try { return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')) || {}; } catch { return {}; }
}
function qwenToken() {
  const st = loadState();
  return String((st.token || process.env.QWEN_TOKEN || '').trim());
}
function cookiesFor() {
  const st = loadState();
  return String(st.cookies || process.env.QWEN_COOKIES || '').trim();
}
function uaFor() {
  const st = loadState();
  return (st.ua ? String(st.ua) : UA);
}

function jwtExpiry(token) {
  try {
    const p = token.split('.')[1];
    const d = JSON.parse(Buffer.from(p, 'base64').toString('utf8'));
    return Number(d.exp || 0);
  } catch { return 0; }
}

function baseHeaders() {
  const h = {
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
    'Content-Type': 'application/json',
    'Version': VERSION,
    'source': 'web',
    'X-Accel-Buffering': 'no',
    'Origin': 'https://chat.qwen.ai',
    'Referer': 'https://chat.qwen.ai/',
    'User-Agent': uaFor(),
    'X-Request-Id': crypto.randomUUID(),
  };
  const token = qwenToken();
  if (token) h['Authorization'] = 'Bearer ' + token;
  const cookies = cookiesFor();
  if (cookies) h['Cookie'] = cookies;
  return h;
}

// Small GET/POST helper against the Qwen origin.
function qPost(pathname, body, extraHeaders) {
  return fetch(QWEN_BASE + pathname, {
    method: 'POST', headers: { ...baseHeaders(), ...(extraHeaders || {}) },
    body: JSON.stringify(body),
  }).then(async (r) => ({ status: r.status, text: await r.text() }));
}

function okStatus(status, text) { return status === 200 && !String(text).startsWith('{"success":false'); }

// Full Qwen chat round: returns {ok, answer, thinking, chatId, error}
async function qwenComplete(model, prompt, thinking, search) {
  const token = qwenToken();
  if (!token) return { ok: false, error: 'no qwen token in state.json — paste localStorage.token from chat.qwen.ai' };
  if (jwtExpiry(token) && jwtExpiry(token) < Date.now() / 1000 + 300) {
    console.log('qwen token near expiry (exp in ' + Math.max(0, Math.round(jwtExpiry(token) - Date.now() / 1000)) + 's) — attempting anyway');
  }
  const now = Math.floor(Date.now() / 1000);
  const nowMs = Date.now();
  const statusBody = { typarms: { ...ORGID_ARMS, orgid: 'tongyi', share_id: '', project_id: '', chat_id: '' } };

  // 1. users/status (register)
  const s1 = await qPost('/api/v2/users/status', statusBody);
  if (!okStatus(s1.status, s1.text)) return { ok: false, error: 'users/status failed: ' + s1.status + ' ' + s1.text.slice(0, 160) };

  // 2. chats/new -> chat id
  const nc = await qPost('/api/v2/chats/new', {
    chatId: '', models: [model], project_id: '', timestamp: nowMs,
    chat_type: 't2t', chat_mode: 'normal',
  });
  let cid = '';
  try { const j = JSON.parse(nc.text); cid = (j.data && j.data.id) || ''; } catch {}
  if (!cid) return { ok: false, error: 'chats/new failed: ' + nc.status + ' ' + nc.text.slice(0, 160) };

  // 3. users/status with the chat id (REQUIRED — else CHAT_NOT_FOUND)
  const s2 = await qPost('/api/v2/users/status', { typarms: { ...ORGID_ARMS, orgid: 'tongyi', share_id: '', project_id: '', chat_id: cid } });
  if (!okStatus(s2.status, s2.text)) return { ok: false, error: 'users/status(chat) failed: ' + s2.status + ' ' + s2.text.slice(0, 160) };

  // 4. chat completions (SSE)
  const compBody = {
    stream: true, version: '2.1', incremental_output: true,
    chatId: cid, parentId: '', chat_id: cid, chat_mode: 'normal',
    model: model, parent_id: null,
    messages: [{
      id: null, fid: crypto.randomUUID(), parentId: null, childrenIds: [],
      role: 'user', content: prompt, user_action: 'chat', files: [],
      timestamp: now, models: [model], model: '', chat_type: 't2t',
      feature_config: {
        thinking_enabled: !!thinking, output_schema: 'phase',
        research_mode: 'normal', auto_thinking: !!thinking,
        thinking_mode: thinking ? 'Auto' : 'Off', thinking_format: 'summary',
        auto_search: !!search,
      },
      extra: { meta: { subChatType: 't2t' } },
      sub_chat_type: 't2t', parent_id: null,
    }],
    timestamp: now,
  };
  const tz = new Date().toString().slice(0, 33);
  const r4 = await qPost('/api/v2/chat/completions?chat_id=' + cid, compBody, { Timezone: tz });
  if (!okStatus(r4.status, r4.text)) return { ok: false, error: 'completions failed: ' + r4.status + ' ' + r4.text.slice(0, 160), chatId: cid };

  // Parse `data: {...}` SSE frames -> answer + thinking_summary
  const answer = [], think = [];
  for (const line of r4.text.split(/\r?\n/)) {
    const t = line.trim();
    if (!t.startsWith('data:') || t.includes('[DONE]')) continue;
    let d; try { d = JSON.parse(t.slice(5).trim()); } catch { continue; }
    if (d.response && d.response.created) continue;
    for (const c of (d.choices || [])) {
      const delta = c.delta || {};
      if (!delta.content) continue;
      if (delta.phase === 'thinking_summary') think.push(delta.content);
      else answer.push(delta.content);
    }
  }
  console.log('qwen chat ' + cid + ' — answer ' + answer.join('').length + ' chars, think ' + think.join('').length + ' chars');
  return { ok: true, chatId: cid, answer: answer.join(''), thinking: think.join('') };
}

// ── OpenAI-compatible surface ────────────────────────────────────────────────
const MODEL_CATALOG = [
  { id: QWEN_MODEL, backend: 'qwen' },
  { id: 'qwen3.8-max', backend: 'qwen' },
  { id: 'qwen3.8-omni-flash', backend: 'qwen' },
  { id: 'auto', backend: 'chatgpt' },
  { id: 'gpt-6', backend: 'chatgpt' },
  { id: 'gpt-6-mini', backend: 'chatgpt' },
  { id: 'gpt-5-6', backend: 'chatgpt' },
  { id: 'gpt-5-6-mini', backend: 'chatgpt' },
];
const GPT_NOTE = 'chatgpt backend needs a live browser session (sentinel PoW) — unavailable in the headless container';

function backendFor(model) {
  const m = (model || '').toLowerCase();
  if (m.startsWith('qwen')) return 'qwen';
  if (m.startsWith('gpt') || m.startsWith('o1') || m.startsWith('o3') || m.startsWith('auto') || m.startsWith('chatgpt')) return 'chatgpt';
  return 'unknown';
}

function decompose(messages) {
  const parts = [];
  for (const m of (messages || [])) {
    let c;
    if (Array.isArray(m.content)) c = m.content.filter(p => p && p.type === 'text').map(p => p.text || '').join('\n');
    else c = String(m.content != null ? m.content : '');
    const role = String(m.role || 'user');
    parts.push(role === 'user' ? c.trim() : role[0].toUpperCase() + role.slice(1) + ': ' + c.trim());
  }
  return parts.filter(Boolean).join('\n\n');
}

function requireKey(req) {
  if (!API_KEY) return;
  const a = String(req.headers.authorization || '');
  if (!a.startsWith('Bearer ') || a.slice(7).trim() !== API_KEY) throw badRequest(401, 'Invalid or missing Bearer token');
}
function badRequest(code, detail) {
  const e = new Error(detail); e.code = code; return e;
}

function sendJson(res, code, obj) {
  const b = JSON.stringify(obj);
  res.writeHead(code, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
  res.end(b);
}

const server = http.createServer(async (req, res) => {
  const p = (req.url || '').split('?')[0];
  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Authorization, Content-Type',
    });
    return res.end();
  }
  try {
    // GET /v1/models
    if (p === '/v1/models' && req.method === 'GET') {
      const seen = new Set(); const out = [];
      for (const e of MODEL_CATALOG) {
        if (seen.has(e.id)) continue;
        seen.add(e.id);
        out.push({ id: e.id, object: 'model', created: 0, owned_by: 'aichatapi-' + e.backend });
      }
      return sendJson(res, 200, { object: 'list', data: out });
    }

    // GET /v1/health
    if (p === '/v1/health' && req.method === 'GET') {
      let q;
      const t = qwenToken();
      if (!t) q = 'error: no token in state.json';
      else q = 'exp_in=' + Math.max(0, Math.round(jwtExpiry(t) - Date.now() / 1000)) + 's' + (cookiesFor() ? ' +cookies' : ' +bearer-only');
      return sendJson(res, 200, { status: 'ok', qwen: q, chatgpt: 'error: ' + GPT_NOTE, mode: 'headless-node' });
    }

    // POST /v1/chat/completions
    if (p === '/v1/chat/completions' && req.method === 'POST') {
      requireKey(req);
      let body = '';
      for await (const c of req) body += c;
      let j;
      try { j = JSON.parse(body || '{}'); } catch { throw badRequest(400, 'invalid JSON body'); }
      if (!Array.isArray(j.messages) || !j.messages.length) throw badRequest(400, 'messages required');
      const model = String(j.model || QWEN_MODEL).trim() || QWEN_MODEL;
      const backend = backendFor(model);
      const prompt = decompose(j.messages);
      if (!prompt.trim()) throw badRequest(400, 'messages contain no text');
      if (backend !== 'qwen') throw badRequest(400, 'model ' + model + ': ' + GPT_NOTE);

      const created = Math.floor(Date.now() / 1000);
      const rid = 'chatcmpl-' + crypto.randomBytes(10).toString('hex');
      console.log('chat ' + model + ' chars=' + prompt.length + ' stream=' + !!j.stream);
      const r = await qwenComplete(model, prompt, !!j.thinking, !!j.search);
      const text = r.ok ? (r.answer || '(empty qwen answer)') : 'Bridge error: ' + r.error;

      if (j.stream) {
        res.writeHead(200, {
          'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache',
          'Connection': 'keep-alive', 'Access-Control-Allow-Origin': '*',
        });
        for (let i = 0; i < text.length; i += 300) {
          const piece = text.slice(i, i + 300);
          res.write('data: ' + JSON.stringify({ id: rid, object: 'chat.completion.chunk', created, model, choices: [{ index: 0, delta: { content: piece }, finish_reason: null }] }) + '\n\n');
        }
        res.write('data: ' + JSON.stringify({ id: rid, object: 'chat.completion.chunk', created, model, choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] }) + '\n\n');
        res.write('data: [DONE]\n\n');
        return res.end();
      }
      return sendJson(res, 200, {
        id: rid, object: 'chat.completion', created, model,
        choices: [{ index: 0, message: { role: 'assistant', content: text }, finish_reason: 'stop' }],
        usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
        ...(r.ok ? { qwen: { chatId: r.chatId, thinking: r.thinking || undefined } } : {}),
      });
    }
    throw badRequest(404, 'not found');
  } catch (e) {
    const code = e.code || 500;
    if (code >= 500) console.log('error: ' + (e.stack || e.message));
    sendJson(res, code, { error: e.message });
  }
});

server.listen(PORT, '0.0.0.0', () => console.log('WEB serving on :' + PORT + ' (headless bridge; state.json for qwen token)'));
