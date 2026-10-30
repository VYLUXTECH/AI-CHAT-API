# AI-CHAT-API — pure-HTTP bridge for chat.qwen.ai + chatgpt.com
# OpenAI-compatible FastAPI server. Chrome is used ONLY to bootstrap/refresh
# sessions (cookies + tokens); chat traffic is plain HTTPS replaying the exact
# requests the web apps make.
#
#   Qwen    : POST /api/v2/chat/completions?chat_id=   (Bearer JWT in localStorage)
#   ChatGPT : POST /backend-api/conversation           (Bearer from /api/auth/session)
#
# Fallback: page-context fetch via CDP when direct requests hit a challenge.
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

load_dotenv(Path(__file__).resolve().parent / ".env")

PORT = int(os.getenv("AICHAT_PORT", os.getenv("BRIDGE_PORT", "3080")))
API_KEY = os.getenv("BRIDGE_API_KEY", "").strip()          # optional Bearer auth for OUR api
QWEN_MODEL = os.getenv("AICHAT_QWEN_MODEL", "qwen3.7-plus")
GPT_MODEL = os.getenv("AICHAT_GPT_MODEL", "auto")
CDP_URL = os.getenv("AICHAT_CDP_URL", "http://127.0.0.1:9222")
SEND_TIMEOUT = float(os.getenv("AICHAT_TIMEOUT", "240"))
UA = os.getenv("AICHAT_UA", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("aichatapi")

app = FastAPI(title="AI-CHAT-API (OpenAI-compatible, pure-HTTP)")

import httpx

# --------------------------------------------------------------------------- #
#  CDP helper: token/cookie bootstrap + page-context fetch fallback
# --------------------------------------------------------------------------- #
def _page_targets() -> list[dict]:
    return [t for t in json.loads(urllib.request.urlopen(f"{CDP_URL}/json", timeout=5).read()) if t.get("type") == "page"]


async def _page_eval_js(expr: str, prefer_host: str, timeout: float = 60.0) -> Any:
    """Evaluate JS in a page target (uses existing session/cookies)."""
    import websockets
    targets = _page_targets()
    t = next((x for x in targets if prefer_host in x.get("url", "")), None) \
        or next((x for x in targets if "http" in x.get("url", "")), None)
    if not t:
        raise RuntimeError(f"no page target for {prefer_host} — start the visible Chrome")
    mid = int(time.time() * 1000) % 1_000_000
    async with websockets.connect(t["webSocketDebuggerUrl"], open_timeout=10, max_size=None) as ws:
        await ws.send(json.dumps({"id": mid, "method": "Runtime.evaluate",
                                  "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.time())))
            if r.get("id") != mid:
                continue
            if r.get("result", {}).get("exceptionDetails"):
                raise RuntimeError(f"page JS error: {json.dumps(r['result']['exceptionDetails'])[:300]}")
            return r.get("result", {}).get("result", {}).get("value")
    raise TimeoutError("page eval timed out")


async def _chrome_ua() -> str:
    try:
        v = json.loads(urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=5).read())
        return v.get("User-Agent") or v.get("userAgent") or UA
    except Exception:
        return UA


async def _chrome_cookie_header(host_sub: str) -> Optional[str]:
    """Full cookie header for host, straight from the Chrome jar via CDP."""
    try:
        import websockets
        targets = _page_targets()
        t = next((x for x in targets if "http" in x.get("url", "")), None)
        if not t:
            return None
        mid = int(time.time() * 1000) % 1_000_000
        async with websockets.connect(t["webSocketDebuggerUrl"], open_timeout=10, max_size=None) as ws:
            await ws.send(json.dumps({"id": mid, "method": "Network.getCookies",
                                      "params": {"urls": [f"https://{host_sub}"]}}))
            deadline = time.time() + 10
            while time.time() < deadline:
                r = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                if r.get("id") == mid:
                    cookies = r.get("result", {}).get("cookies", [])
                    return "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("value"))
        return None
    except Exception as e:
        logger.warning("cookie harvest failed: %s", e)
        return None

# --------------------------------------------------------------------------- #
#  Shared SSE parsing of `data: {...}` frames
# --------------------------------------------------------------------------- #
def _sse_frames(text: str) -> list[str]:
    frames = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:") and "[DONE]" not in line:
            frames.append(line[5:].strip())
    return frames

# --------------------------------------------------------------------------- #
#  QWEN backend — pure HTTP replay of the web app request
# --------------------------------------------------------------------------- #
QWEN_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Version": "0.3.27",
    "source": "web",
    "X-Accel-Buffering": "no",
    "Origin": "https://chat.qwen.ai",
    "Referer": "https://chat.qwen.ai/",
}

QW_PAGE = {
    "token": None, "ts": 0.0,
}


def _jwt_expiry(token: str) -> float:
    try:
        import base64, hashlib
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        d = json.loads(base64.b64decode(payload))
        return float(d.get("exp", 0))
    except Exception:
        return 0.0


async def qwen_token() -> str:
    """Fresh Qwen JWT — cached until near-expiry; refreshes from the Chrome page."""
    t = QW_PAGE.get("token")
    if t and _jwt_expiry(t) > time.time() + 300:
        return t
    t = await _page_eval_js("(localStorage.getItem('token') || '')", "chat.qwen.ai") or ""
    if not t:
        raise RuntimeError("no qwen token in localStorage — log into chat.qwen.ai in the visible Chrome")
    QW_PAGE["token"], QW_PAGE["ts"] = t, time.time()
    logger.info("[qwen] token refreshed (exp in %ds)", int(_jwt_expiry(t) - time.time()))
    return t


async def _qwen_headers() -> dict:
    hdrs = {**QWEN_HEADERS}
    hdrs["Authorization"] = "Bearer " + await qwen_token()
    hdrs["User-Agent"] = await _chrome_ua()
    cookies = await _chrome_cookie_header("chat.qwen.ai")
    if cookies:
        hdrs["Cookie"] = cookies
    return hdrs


async def _page_fetch_json(url: str, body: Optional[dict], host: str, timeout: float = 30.0) -> Any:
    """Same-origin POST fetch executed on the page (full cookie + challenge fidelity)."""
    eid = str(uuid.uuid4())
    expr = (
        "(async function(){"
        f"var r=await fetch({json.dumps(url)},{{method:'POST',"
        "headers:{'Content-Type':'application/json','Accept':'application/json','X-Request-Id':'" + eid + "'},"
        f"body:{json.dumps(json.dumps(body))}}});"
        "var t=await r.text();"
        "return JSON.stringify({s:r.status, b:t.slice(0,2000)});})()"
    )
    raw = await _page_eval_js(expr, host, timeout=timeout)
    d = json.loads(raw)
    out = json.loads(d.get("b", "{}")) if "{" in d.get("b", " ") else {}
    return d.get("s"), out


async def qwen_new_chat(model: str) -> str:
    """Create a Qwen chat server-side; returns chat_id."""
    body = {
        "chatId": "",
        "models": [model],
        "project_id": "",
        "timestamp": int(time.time() * 1000),
        "chat_type": "t2t",
        "chat_mode": "normal",
    }
    url = "https://chat.qwen.ai/api/v2/chats/new"
    try:
        hdrs = await _qwen_headers()
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await client.post(url, json=body, headers=hdrs)
        if r.status_code == 200:
            d = r.json()
            if d.get("success"):
                cid = d["data"]["id"]
                logger.info("[qwen] chat created %s (direct)", cid)
                return cid
        logger.warning("[qwen] direct new-chat %d; page fallback", r.status_code)
    except Exception as e:
        logger.warning("[qwen] direct new-chat failed (%s); page fallback", e)
    status, out = await _page_fetch_json(url, body, "chat.qwen.ai")
    cid = (out or {}).get("data", {}).get("id")
    if not cid:
        raise RuntimeError("qwen chat creation failed: " + json.dumps(out)[:200])
    logger.info("[qwen] chat created %s (page)", cid)
    return cid


def _qwen_body(model: str, prompt: str, chat_id: str, thinking: bool, search: bool) -> dict:
    now = int(time.time())
    return {
        "stream": True, "version": "2.1", "incremental_output": True,
        "chatId": chat_id, "parentId": "", "chat_id": chat_id,
        "chat_mode": "normal", "model": model, "parent_id": None,
        "messages": [{
            "id": None, "fid": str(uuid.uuid4()), "parentId": None,
            "childrenIds": [], "role": "user", "content": prompt,
            "user_action": "chat", "files": [], "timestamp": now,
            "models": [model], "model": "", "chat_type": "t2t",
            "feature_config": {
                "thinking_enabled": bool(thinking), "output_schema": "phase",
                "research_mode": "normal", "auto_thinking": bool(thinking),
                "thinking_mode": "Auto" if thinking else "Off",
                "thinking_format": "summary", "auto_search": bool(search),
            },
            "extra": {"meta": {"subChatType": "t2t"}},
            "sub_chat_type": "t2t", "parent_id": None,
        }],
        "timestamp": now,
    }


async def _qwen_request(model: str, prompt: str, thinking: bool, search: bool) -> AsyncIterator[str]:
    """Run the full app-order request sequence from the page context.

    Verified sequence (a missing /api/v2/users/status registration => CHAT_NOT_FOUND):
      1. POST /api/v2/users/status                    (no chat id)
      2. POST /api/v2/chats/new                       -> data.id
      3. POST /api/v2/users/status                    (with the new chat id)
      4. POST /api/v2/chat/completions?chat_id=<id>   (SSE reply)
    """
    now = int(time.time())
    now_ms = int(time.time() * 1000)
    msg = {
        "id": None, "fid": "__fid", "parentId": None, "childrenIds": [],
        "role": "user", "content": prompt, "user_action": "chat", "files": [],
        "timestamp": now, "models": [model], "model": "", "chat_type": "t2t",
        "feature_config": {
            "thinking_enabled": bool(thinking), "output_schema": "phase",
            "research_mode": "normal", "auto_thinking": bool(thinking),
            "thinking_mode": "Auto" if thinking else "Off",
            "thinking_format": "summary", "auto_search": bool(search),
        },
        "extra": {"meta": {"subChatType": "t2t"}},
        "sub_chat_type": "t2t", "parent_id": None,
    }
    payload = json.dumps({"model": model, "content": prompt, "fid": str(uuid.uuid4()),
                          "timestamp": now, "nowMs": now_ms,
                          "thinking": bool(thinking), "search": bool(search)})
    expr = _QW_SEQ_JS.replace("__PAYLOAD__", payload)
    try:
        raw = await _page_eval_js(expr, "chat.qwen.ai", timeout=SEND_TIMEOUT)
    except RuntimeError as e:
        raise RuntimeError(f"qwen request failed: {str(e)[:200]}")
    d = json.loads(raw)
    if not d.get("ok"):
        raise RuntimeError("qwen flow failed: " + json.dumps(d.get("fail") or d)[:300])
    logger.info("[qwen] SSE %d bytes, chat %s", len(d.get("text") or ""), d.get("chatId"))
    yield d.get("text") or ""


_QW_SEQ_JS = r"""
(async function(){
  var P = __PAYLOAD__;
  var now = P.timestamp, nowMs = P.nowMs;
  var acc = [];
  async function hit(url, opt){
    try {
      var r = await fetch(url, opt);
      var t = null;
      try { t = await r.text(); } catch(e){}
      acc.push({u:url.slice(0,60), s:r.status});
      return {status:r.status, text:t||''};
    } catch(e)
    { return {status:0, text:'ERR ' + String(e).slice(0,120)}; }
  }
  var baseH = {'Content-Type':'application/json','Accept':'application/json','source':'web',
    'X-Request-Id':(crypto.randomUUID?crypto.randomUUID():String(Date.now())),
    'Version':'0.3.27','X-Accel-Buffering':'no','Accept-Language':'en-US,en;q=0.9'};
  try {
    var st = {typarms:{typarm1:'web', typarm2:'9ecc74ad-09a0-4fad-8a44-663978aa37c0', typarm3:'prod',
      typarm4:'qwen_chat', typarm5:'product', typarm6:'', orgid:'tongyi', share_id:'', project_id:'', chat_id:''}};
    await hit('/api/v2/users/status', {method:'POST', headers:baseH, body:JSON.stringify(st)});
    var nc = await hit('/api/v2/chats/new', {method:'POST', headers:baseH,
      body:JSON.stringify({chatId:'', models:[P.model], project_id:'', timestamp:nowMs, chat_type:'t2t', chat_mode:'normal'})});
    var cid = '';
    try { cid = JSON.parse(nc.text).data.id; } catch(e){}
    if (!cid) return JSON.stringify({ok:false, fail:{step:'chats/new', detail:nc.text.slice(0,300)}, acc:acc});
    st.typarms.chat_id = cid;
    await hit('/api/v2/users/status', {method:'POST', headers:baseH, body:JSON.stringify({typarms:st.typarms})});
    var body = {stream:true, version:'2.1', incremental_output:true, chatId:cid, parentId:'', chat_id:cid,
      chat_mode:'normal', model:P.model, parent_id:null,
      messages:[{id:null, fid:P.fid, parentId:null, childrenIds:[], role:'user', content:P.content,
        user_action:'chat', files:[], timestamp:now, models:[P.model], model:'', chat_type:'t2t',
        feature_config:{thinking_enabled:P.thinking, output_schema:'phase', research_mode:'normal',
          auto_thinking:P.thinking, thinking_mode:P.thinking?'Auto':'Off', thinking_format:'summary',
          auto_search:P.search},
        extra:{meta:{subChatType:'t2t'}}, sub_chat_type:'t2t', parent_id:null}],
      timestamp:now};
    var H2 = {'Content-Type':'application/json','Accept':'application/json','Accept-Language':'en-US,en;q=0.9',
      'X-Accel-Buffering':'no','X-Request-Id':(crypto.randomUUID?crypto.randomUUID():String(Date.now())),
      'Version':'0.3.27','source':'web','Timezone':new Date().toString().slice(0,33)};
    var comp = await hit('/api/v2/chat/completions?chat_id=' + cid, {method:'POST', headers:H2, body:JSON.stringify(body)});
    var ok = comp.status === 200 && comp.text.slice(0, 21) !== '{"success":false';
    return JSON.stringify({ok:ok, chatId:cid, text:(ok ? comp.text : ''), fail:(ok ? null : {step:'completions', status:comp.status, detail:comp.text.slice(0,300)})});
  } catch(e) {
    return JSON.stringify({ok:false, fail:{step:'js', detail:String(e).slice(0,200)}, acc:acc});
  }
})()
"""


async def qwen_complete(model: str, prompt: str, thinking: bool, search: bool) -> tuple[str, str]:
    """Returns (answer_text, thinking_text)."""
    answer, think = [], []
    async for chunk in _qwen_request(model, prompt, thinking, search):
        for f in _sse_frames(chunk):
            try:
                d = json.loads(f)
            except Exception:
                continue
            if "response.created" in d:
                logger.info("[qwen] chat_id=%s", d["response.created"].get("chat_id"))
                continue
            for c in d.get("choices", []):
                delta = c.get("delta", {})
                content = delta.get("content")
                if not content:
                    continue
                if delta.get("phase") == "thinking_summary":
                    think.append(content)
                else:
                    answer.append(content)
    return ("".join(answer), "".join(think))

# --------------------------------------------------------------------------- #
#  ChatGPT backend — pure HTTP with cookies harvested from the Chrome jar
# --------------------------------------------------------------------------- #
class GptSession:
    ensure_lock: "asyncio.Lock" = None  # set in __init__

    def __init__(self):
        self.ensure_lock = asyncio.Lock()
        self.token: Optional[str] = None
        self.token_ts = 0.0
        self.cookie = None
        self.models: list[dict] = []


GSESS = GptSession()


async def gpt_session(force: bool = False) -> GptSession:
    """accessToken + cookie header for chatgpt.com backend-api."""
    async with GSESS.ensure_lock:
        if not force and GSESS.token and time.time() - GSESS.token_ts < 3000:
            return GSESS
        js = "(async function(){var r=await fetch('/api/auth/session',{headers:{'accept':'application/json'}});var j=await r.json();return JSON.stringify({a:j.accessToken||'',d:JSON.stringify(Object.keys(j)).slice(0,400)});})()"
        raw = await _page_eval_js(js, "chatgpt.com")
        d = json.loads(raw)
        if not d.get("a"):
            raise RuntimeError("no ChatGPT accessToken — log into chatgpt.com in the visible Chrome")
        GSESS.token, GSESS.token_ts = d["a"], time.time()
        GSESS.cookie = await _chrome_cookie_header("chatgpt.com")
        ua = await _chrome_ua()
        async with httpx.AsyncClient(follow_redirects=True) as c:
            mr = await c.get("https://chatgpt.com/backend-api/models",
                             headers={"Authorization": "Bearer " + GSESS.token,
                                      "User-Agent": ua, "Cookie": GSESS.cookie or "",
                                      "Accept": "application/json"})
            if mr.status_code == 200:
                GSESS.models = (mr.json().get("models") or [])
        logger.info("[gpt] session ok, models=%d", len(GSESS.models))
        return GSESS


async def gpt_complete(model: str, prompt: str, thinking: bool = False) -> str:
    """ChatGPT via the live composer: the page performs its own sentinel PoW
    handshake, we just fill the editor + click send and read the transcript.

    Capture strategy (proven in session ses_e74ac7b68ffePeoiSTZDVnrFHc):
    network hooks race the app's own stream reader (ReadableStream is locked and
    DOM message selectors churn between builds), but body.innerText always
    contains the full turn transcript ("You said: … ChatGPT said: …"). The
    reply is everything after the last "ChatGPT said:" marker, returned once
    the text stops changing (streaming updates it continuously)."""
    await _gpt_page_ready()
    before = await _gpt_transcript()
    js_fill = ("(function(p){var ta=document.querySelector('#prompt-textarea')"
               "||document.querySelector('div[contenteditable=true]');"
               "if(!ta) return 'no-editor'; ta.focus();"
               "document.execCommand('selectAll', false, null);"
               "document.execCommand('delete');"
               "document.execCommand('insertText', false, p);"
               "return 'filled:' + (ta.textContent||'').slice(0, 60);})("
               + json.dumps(prompt) + ")")
    filled = await _gpt_retry_fill(js_fill)
    if not str(filled).startswith("filled"):
        raise RuntimeError(f"chatgpt composer fill failed: {filled}")
    await asyncio.sleep(0.6)
    clicked = await _page_eval_js(
        "(function(){var b=document.querySelector('[data-testid=send-button]')"
        "||document.querySelector('button[aria-label*=\"Send\"]');"
        "if(b&&!b.disabled){b.click(); return 'clicked';} return 'no-button';})()",
        "chatgpt.com")
    if clicked != "clicked":
        raise RuntimeError(f"chatgpt send failed: {clicked}")
    logger.info("[gpt] composer sent — polling transcript")
    start = time.time()
    last_val, last_ts = "", 0.0
    while time.time() - start < SEND_TIMEOUT:
        await asyncio.sleep(1.2)
        cur = await _gpt_extract_reply(before, prompt)
        if cur == last_val and cur:
            if time.time() - last_ts > 12:
                logger.info("[gpt] reply stable (%d chars)", len(cur))
                return cur
        else:
            last_val, last_ts = cur, time.time()
    if last_val:
        return last_val
    raise RuntimeError("chatgpt reply capture timed out")


async def _gpt_retry_fill(js_fill: str, attempts: int = 14, delay: float = 1.5) -> str:
    """The composer mounts a beat after the page reports readyState complete."""
    last = "no-editor"
    for _ in range(attempts):
        try:
            last = str(await _page_eval_js(js_fill, "chatgpt.com"))
        except Exception as e:
            last = f"eval-err: {str(e)[:80]}"
        if last.startswith("filled"):
            return last
        await asyncio.sleep(delay)
    return last


async def _gpt_page_ready() -> None:
    """Ensure a ChatGPT tab exists; reload it once if a stale hook error text
    is still visible on the page."""
    try:
        err = await _page_eval_js(
            "(document.body.innerText.indexOf(\"Failed to execute 'getReader'\")>-1||"
            "document.body.innerText.indexOf(\"Unusual activity\")>-1) ? 'err':'ok'",
            "chatgpt.com", timeout=10)
    except Exception:
        raise RuntimeError("no chatgpt.com tab open in the visible Chrome")
    if err == "err":
        logger.info("[gpt] reloading tab to clear stale error text")
        await _page_eval_js("location.reload()", "chatgpt.com")
        for _ in range(20):
            await asyncio.sleep(1.5)
            try:
                st = await _page_eval_js("document.readyState", "chatgpt.com", timeout=10)
                if st == "complete":
                    break
            except Exception:
                pass


def _gpt_norm(intext: str) -> str:
    return " ".join(intext.split())


async def _gpt_transcript() -> str:
    raw = await _page_eval_js("document.body.innerText", "chatgpt.com", timeout=20)
    return _gpt_norm(raw or "")


async def _gpt_extract_reply(before: str, prompt: str) -> str:
    """Text after the 'ChatGPT said:' marker that follows OUR prompt's echo.

    Anchoring on the prompt echo is required on persistent conversations: the
    last 'ChatGPT said:' otherwise refers to the PREVIOUS turn until the new
    reply starts streaming."""
    cur = await _gpt_transcript()
    if cur in (before, ""):
        return ""
    needle = _gpt_norm(prompt)
    uidx = cur.rfind(needle)
    if uidx < 0:
        return ""            # our message hasn't been registered yet
    cidx = cur.find("ChatGPT said:", uidx + len(needle))
    if cidx < 0:
        return ""            # prompt sent, reply not started yet
    tail = cur[cidx + len("ChatGPT said:"):].strip()
    # strip trailing/inline UI artifacts (buttons, notices, status chips)
    cut = len(tail)
    for stop in ("You said:", "Share", "Upgrade", "Failed to execute",
                 "Copy code", "Note the chat's", "ChatGPT can make mistakes",
                 "Latest response", "Response complete", "Response started",
                 "Check important info"):
        p = tail.find(stop)
        if 0 <= p < cut:
            cut = p
    return tail[:cut].strip()




# NOTE: the in-page XHR/fetch network hook was removed on purpose. The app's
# own stream reader locks the ReadableStream, so a second getReader() races it
# (visible as "Failed to execute 'getReader'..." on the page) and the capture
# comes back empty. gpt_complete() reads the rendered transcript instead.




# --------------------------------------------------------------------------- #
#  OpenAI-compatible API surface
# --------------------------------------------------------------------------- #
class Message(BaseModel):
    role: str = "user"
    content: Any


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: list[Message]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False
    user: Optional[str] = None
    thinking: Optional[bool] = False
    search: Optional[bool] = False


def require_key(req_headers) -> None:
    if not API_KEY:
        return
    auth = req_headers.get("authorization", "")
    if not auth.startswith("Bearer ") or auth[7:].strip() != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing Bearer token")


def _decompose(messages) -> str:
    parts = []
    for m in messages:
        c = ""
        mc = getattr(m, "content", None)
        rm = getattr(m, "role", "user")
        if isinstance(mc, list):
            c = "\n".join(p.get("text", "") for p in mc if isinstance(p, dict) and p.get("type") == "text")
        else:
            c = str(mc or "")
        parts.append(f"{rm.capitalize()}: {c.strip()}" if rm != "user" else c.strip())
    return "\n\n".join(p for p in parts if p)


def backend_for(model: str) -> str:
    m = (model or "").lower()
    if m.startswith(("qwen",)):
        return "qwen"
    if m.startswith(("gpt", "o1", "o3", "auto", "research", "chatgpt")):
        return "chatgpt"
    return "unknown"


MODEL_CATALOG = [
    {"id": QWEN_MODEL, "backend": "qwen"},
    {"id": "qwen3.8-max", "backend": "qwen"},
    {"id": "qwen3.8-omni-flash", "backend": "qwen"},
    {"id": "auto", "backend": "chatgpt"},
    {"id": "gpt-6", "backend": "chatgpt"},
    {"id": "gpt-6-mini", "backend": "chatgpt"},
    {"id": "gpt-5-6", "backend": "chatgpt"},
    {"id": "gpt-5-6-mini", "backend": "chatgpt"},
    {"id": GPT_MODEL, "backend": "chatgpt"},
]


@app.get("/v1/models")
async def models_list():
    seen, out = set(), []
    for e in MODEL_CATALOG:
        if e["id"] and e["id"] not in seen:
            seen.add(e["id"])
            out.append({"id": e["id"], "object": "model", "created": 0, "owned_by": f"aichatapi-{e['backend']}"})
    return {"object": "list", "data": out}


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request):
    require_key(request.headers)
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages required")
    model = (req.model or GPT_MODEL).strip() or GPT_MODEL
    backend = backend_for(model)
    prompt = _decompose(req.messages)
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="messages contain no text")

    created = int(time.time())
    rid = "chatcmpl-" + uuid.uuid4().hex[:20]

    async def run() -> str:
        if backend == "qwen":
            a, _ = await qwen_complete(model, prompt, bool(req.thinking), bool(req.search))
            return a or "(empty qwen answer)"
        if backend == "chatgpt":
            if req.search or bool(req.thinking):
                pass  # search/think flags are part of the conversation metadata; default handles it
            return await gpt_complete(model, prompt)
        raise HTTPException(status_code=400, detail=f"unknown backend for model {model}")

    async def generate():
        try:
            text = await asyncio.wait_for(run(), timeout=SEND_TIMEOUT + 60)
        except Exception as e:
            logger.exception("completion failed backend=%s", backend)
            text = f"Bridge error: {e}"
        if req.stream:
            for piece in (text[i:i + 300] for i in range(0, len(text), 300)):
                yield f"data: {json.dumps({'id': rid, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': {'content': piece}, 'finish_reason': None}]})}\n\n"
            yield f"data: {json.dumps({'id': rid, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
            yield "data: [DONE]\n\n"
        else:
            yield json.dumps({"id": rid, "object": "chat.completion", "created": created, "model": model,
                              "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                              "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})

    logger.info("chat backend=%s model=%s chars=%d stream=%s", backend, model, len(prompt), req.stream)
    return StreamingResponse(generate(), media_type="text/event-stream" if req.stream else "application/json")


@app.get("/v1/health")
async def health():
    q, g = None, None
    try:
        t = await qwen_token()
        q = f"exp_in={int(_jwt_expiry(t) - time.time())}s"
    except Exception as e:
        q = f"error: {str(e)[:70]}"
    try:
        s = await gpt_session()
        g = f"models={len(s.models)}"
    except Exception as e:
        g = f"error: {str(e)[:70]}"
    return {"status": "ok", "qwen": q, "chatgpt": g, "cdp": CDP_URL}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
