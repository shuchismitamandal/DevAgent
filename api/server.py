"""
api/server.py — DevAgent with Atlassian OAuth 2.0 login
"""
from __future__ import annotations
import sys, os, asyncio, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query, Request, Cookie
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from agents.orchestrator import run_devagent
from core.models import AgentBriefing
from core.store import briefing_store
from core.ws_manager import ws_manager
from core.auth import (
    get_authorization_url, exchange_code_for_token,
    get_user_info, get_cloud_id,
    create_session_cookie, decode_session_cookie,
    verify_state, fetch_my_jira_tickets,
    CLIENT_ID,
)

BASE_DIR       = Path(__file__).parent.parent
DASHBOARD_PATH = BASE_DIR / "dashboard" / "agent_dashboard.html"
LOGIN_PATH     = BASE_DIR / "dashboard" / "login.html"

_user_poller_tasks: dict[str, asyncio.Task] = {}
_user_seen_tickets: dict[str, list[dict]] = {}

async def _keepalive_loop():
    while True:
        await asyncio.sleep(20)
        if ws_manager.active:
            await ws_manager.ping()

@asynccontextmanager
async def lifespan(app: FastAPI):
    keepalive_task = asyncio.create_task(_keepalive_loop())
    yield
    keepalive_task.cancel()
    for task in _user_poller_tasks.values():
        task.cancel()
    try:
        await keepalive_task
    except asyncio.CancelledError:
        pass

app = FastAPI(title="DevAgent", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


def get_current_user(cookie_value: Optional[str]) -> dict | None:
    if not cookie_value:
        return None
    return decode_session_cookie(cookie_value)


async def _poll_for_user(user: dict):
    """Background polling task for one logged-in user."""
    account_id    = user["account_id"]
    display_name  = user["display_name"]
    access_token  = user["access_token"]
    jira_base_url = user["jira_base_url"]
    seen_ids: set[str] = set()
    seen_tickets: list[dict] = []
    is_first   = True
    poll_count = 0

    print(f"[Poller:{display_name}] Started — polling every 30s")

    while True:
        try:
            poll_count += 1
            tickets = await fetch_my_jira_tickets(
                access_token, jira_base_url, display_name, is_first
            )
            new_tickets = [t for t in tickets if t["id"] not in seen_ids]

            new_tickets = [
                t for t in new_tickets
                if t.get("reporter", "").lower() != display_name.lower()
            ]

            if new_tickets:
                print(f"[Poller:{display_name}] Poll #{poll_count} — {len(new_tickets)} ticket(s)")
                for ticket in new_tickets:
                    seen_ids.add(ticket["id"])
                    seen_tickets.append(ticket) 
                    _user_seen_tickets[account_id] = seen_tickets
                    await ws_manager.broadcast_to_user(account_id,"new_mention", {
                        "id":       ticket["id"],
                        "title":    ticket["title"],
                        "priority": ticket["priority"],
                        "type":     ticket["type"],
                        "reporter": ticket["reporter"],
                        "time": ticket.get("created", ""),
                        "status":   "investigating",
                        "jira_url": ticket["jira_url"],
                        "isNew":    not is_first,
                        "for_user": account_id,
                    })
                    asyncio.create_task(
                        _run_agent_for_user(
                            ticket["id"], display_name, account_id,
                            access_token=access_token, jira_base_url=jira_base_url,
                        )
                    )
            else:
                print(f"[Poller:{display_name}] Poll #{poll_count} — no new mentions")

            is_first = False

        except PermissionError:
            print(f"[Poller:{display_name}] Token expired — stopping")
            break
        except Exception as e:
            print(f"[Poller:{display_name}] Error: {e}")

        await asyncio.sleep(30)


async def _run_agent_for_user(
    ticket_id: str, developer: str, account_id: str,
    access_token: str = "", jira_base_url: str = ""
):
    try:
        briefing = await run_devagent(
            ticket_id, developer, force_refresh=True,
            access_token=access_token, jira_base_url=jira_base_url,
            account_id=account_id,
        )
        if briefing is None:  # ← ADD THIS
            print(f"[Agent] ⏭ {ticket_id} — skipped (reporter is developer)")
            return
        
        await ws_manager.broadcast_to_user(account_id, "briefing_ready", {
            "ticket_id":      ticket_id,
            "summary":        briefing.summary,
            "severity_score": briefing.severity_score,
            "confidence":     briefing.confidence.value,
            "confidence_pct": briefing.confidence_pct,
            "action_count":   len(briefing.action_items),
            "for_user":       account_id,
        })
        print(f"[Agent] ✓ {ticket_id} — severity {briefing.severity_score}/10")
    except Exception as e:
        print(f"[Agent] ✗ {ticket_id}: {e}")
        await ws_manager.broadcast_to_user(account_id, "briefing_error", {
            "ticket_id": ticket_id,
            "error":     str(e),
        })


# ── Auth routes ────────────────────────────────────────────────────────

@app.get("/auth/login")
async def auth_login():
    if not CLIENT_ID:
        return HTMLResponse("<h2>Set ATLASSIAN_CLIENT_ID in .env</h2>", status_code=500)
    url, _ = get_authorization_url()
    return RedirectResponse(url=url)

@app.get("/debug/token")
async def debug_token(request: Request):
    user = get_current_user(request.cookies.get("devagent_session"))
    if not user:
        return {"error": "not logged in"}
    return {"token": user["access_token"][:50] + "..."}

@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    user = get_current_user(request.cookies.get("devagent_session"))
    if not user:
        return RedirectResponse(url="/login")
    # Check if already configured
    from core.store import load_user_credentials
    creds = load_user_credentials(user["account_id"])
    if creds.get("bitbucket_password"):
        return RedirectResponse(url="/dashboard")
    return HTMLResponse(content=_inline_setup_html())

class CredentialsRequest(BaseModel):
    bitbucket_password: str

@app.post("/setup/save")
async def save_credentials(request: Request, creds: CredentialsRequest):
    user = get_current_user(request.cookies.get("devagent_session"))
    if not user:
        raise HTTPException(status_code=401)
    
    from core.store import save_user_credentials
    save_user_credentials(user["account_id"], {
        "bitbucket_username": user["email"],
        "bitbucket_password": creds.bitbucket_password,
        "bitbucket_workspace": os.getenv("BITBUCKET_WORKSPACE", "navadhan"),
    })
    return {"status": "saved"}

@app.get("/auth/cb")
async def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse(url=f"/login?error={error}")
    if not code:
        return RedirectResponse(url="/login?error=no_code")
    if not verify_state(state):
        return RedirectResponse(url="/login?error=invalid_state")

    try:
        token_data   = await exchange_code_for_token(code)
        access_token = token_data["access_token"]
        user_info    = await get_user_info(access_token)
        display_name = user_info.get("name", user_info.get("displayName", "Developer"))
        email        = user_info.get("email", "")
        account_id   = user_info.get("account_id", "")
        cloud_id, jira_base_url = await get_cloud_id(access_token)

        session_data = {
            "access_token":  access_token,
            "display_name":  display_name,
            "email":         email,
            "account_id":    account_id,
            "cloud_id":      cloud_id,
            "jira_base_url": jira_base_url,
        }
        cookie_value = create_session_cookie(session_data)

        if account_id not in _user_poller_tasks or _user_poller_tasks[account_id].done():
            _user_poller_tasks[account_id] = asyncio.create_task(
                _poll_for_user(session_data)
            )
            print(f"[Auth] Started poller for {display_name}")

        response = RedirectResponse(url="/dashboard", status_code=302)
        response.set_cookie(
            key="devagent_session",
            value=cookie_value,
            httponly=True,
            max_age=86400 * 7,
            samesite="lax",
        )
        print(f"[Auth] ✓ {display_name} ({email}) logged in")
        return response

    except Exception as e:
        print(f"[Auth] Callback error: {e}")
        return RedirectResponse(url="/login?error=auth_failed")
    
    


@app.get("/auth/logout")
async def auth_logout():
    response = RedirectResponse(url="/login")
    response.delete_cookie("devagent_session")
    return response


@app.get("/auth/me")
async def auth_me(request: Request):
    user = get_current_user(request.cookies.get("devagent_session"))
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {
        "display_name":  user["display_name"],
        "email":         user["email"],
        "account_id":    user["account_id"],
        "jira_base_url": user["jira_base_url"],
    }


# ── Page routes ────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root(request: Request):
    user = get_current_user(request.cookies.get("devagent_session"))
    return RedirectResponse(url="/dashboard" if user else "/login")


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page():
    if LOGIN_PATH.exists():
        return HTMLResponse(content=LOGIN_PATH.read_text(encoding="utf-8"))
    return HTMLResponse(content=_inline_login_html())


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request):
    user = get_current_user(request.cookies.get("devagent_session"))
    if not user:
        return RedirectResponse(url="/login")
    if not DASHBOARD_PATH.exists():
        return HTMLResponse("<h2>Dashboard not found</h2>", status_code=404)
    return HTMLResponse(content=DASHBOARD_PATH.read_text(encoding="utf-8"))


# ── API routes ─────────────────────────────────────────────────────────

class TriggerRequest(BaseModel):
    ticket_id: str
    developer: str


@app.get("/health")
async def health():
    return {
        "status":         "ok",
        "agent":          "DevAgent v2.0",
        "active_pollers": len([t for t in _user_poller_tasks.values() if not t.done()]),
        "cache":          briefing_store.stats(),
        "websockets":     ws_manager.stats(),
    }


@app.get("/me")
async def get_me(request: Request):
    user = get_current_user(request.cookies.get("devagent_session"))
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {
        "developer":     user["display_name"],
        "account_id": user["account_id"],
        "email":         user["email"],
        "jira_base_url": user["jira_base_url"]
    }


@app.post("/trigger", response_model=AgentBriefing)
async def trigger_agent(
    request: Request,
    req: TriggerRequest,
    force_refresh: bool = Query(False),
):
    user      = get_current_user(request.cookies.get("devagent_session"))
    developer = user["display_name"] if user else req.developer
    briefing  = await run_devagent(
        req.ticket_id,
        developer,
        force_refresh=force_refresh,
        access_token=user.get("access_token", "") if user else "",
        jira_base_url=user.get("jira_base_url", "") if user else "",
        account_id=user.get("account_id", "") if user else "",
    )
    account_id = user.get("account_id", "") if user else ""
    await ws_manager.broadcast_to_user(account_id, "briefing_ready", {
        "ticket_id":      req.ticket_id,
        "summary":        briefing.summary,
        "severity_score": briefing.severity_score,
        "confidence":     briefing.confidence.value,
        "confidence_pct": briefing.confidence_pct,
        "action_count":   len(briefing.action_items),
        "for_user":       account_id,
    })
    return briefing


@app.get("/cache/stats")
async def cache_stats():
    return briefing_store.stats()




@app.delete("/cache/{ticket_id}")
async def invalidate_cache(ticket_id: str):
    removed = await briefing_store.invalidate(ticket_id)
    if removed:
        return {"status": "invalidated", "ticket_id": ticket_id}
    return JSONResponse(status_code=404, content={"status": "not_found"})


# ── WebSocket ──────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    ws_user = get_current_user(websocket.cookies.get("devagent_session"))
    account_id = ws_user.get("account_id", "") if ws_user else ""
    await ws_manager.connect(websocket, account_id)
    await ws_manager.send_to(websocket, "connected", {"message": "DevAgent live feed connected"})
    
    if account_id and account_id in _user_seen_tickets:
        for ticket in _user_seen_tickets[account_id]:
            await ws_manager.send_to(websocket, "new_mention", {
                "id":       ticket["id"],
                "title":    ticket["title"],
                "priority": ticket["priority"],
                "type":     ticket["type"],
                "reporter": ticket["reporter"],
                "time":     "recently",
                "status":   "ready",
                "jira_url": ticket["jira_url"],
                "isNew":    False,
                "for_user": account_id,
            })

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data   = json.loads(raw)
                action = data.get("action")
                if action == "trigger":
                    ticket_id = data.get("ticket_id", "")
                    developer = data.get("developer", "")
                    # Try to get user session from the WebSocket request cookies
                    ws_user = get_current_user(websocket.cookies.get("devagent_session"))
                    asyncio.create_task(_run_agent_for_user(
                        ticket_id,
                        developer,
                        ws_user.get("account_id", "") if ws_user else "ws_client",
                        access_token=ws_user.get("access_token", "") if ws_user else "",
                        jira_base_url=ws_user.get("jira_base_url", "") if ws_user else "",
                    ))
                elif action == "ping":
                    await ws_manager.send_to(websocket, "pong", {})
            except Exception as e:
                await ws_manager.send_to(websocket, "error", {"message": str(e)})
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)

def _inline_setup_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>DevAgent — Setup</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #07090F; color: #C4D4E8; font-family: 'JetBrains Mono', monospace;
    display: flex; align-items: center; justify-content: center; min-height: 100vh; }
  .card { background: #0C1018; border: 1px solid #243550; border-radius: 12px;
    padding: 40px 36px; width: 420px; }
  h1 { font-size: 20px; font-weight: 800; color: #fff; margin-bottom: 8px; }
  .sub { color: #6B88A8; font-size: 12px; margin-bottom: 28px; line-height: 1.6; }
  label { font-size: 11px; color: #6B88A8; display: block; margin-bottom: 6px; }
  input { width: 100%; padding: 10px 12px; background: #07090F; border: 1px solid #243550;
    border-radius: 6px; color: #C4D4E8; font-family: inherit; font-size: 13px; margin-bottom: 20px; }
  .btn { width: 100%; padding: 12px; background: #2D7DD2; color: #fff; border: none;
    border-radius: 8px; font-size: 13px; font-weight: 700; cursor: pointer; font-family: inherit; }
  .btn:hover { opacity: .85; }
  .skip { text-align: center; margin-top: 16px; font-size: 11px; color: #344D68; }
  .skip a { color: #6B88A8; text-decoration: none; }
  .success { background: rgba(34,197,94,.12); border: 1px solid rgba(34,197,94,.3);
    border-radius: 6px; padding: 10px; color: #22C55E; font-size: 12px; display: none; margin-bottom: 16px; }
</style>
</head>
<body>
<div class="card">
  <h1>⚡ One-time Setup</h1>
  <p class="sub">Connect your Bitbucket account to see real pull requests in your briefings.
  You'll only need to do this once.</p>
  
  <div class="success" id="success">✓ Credentials saved! Redirecting...</div>
  
  <label>BITBUCKET APP PASSWORD</label>
  <input type="password" id="bbPassword" placeholder="Paste your Bitbucket app password" />
  
  <p class="sub" style="margin-top:-12px; margin-bottom:20px;">
    Generate one at bitbucket.org → Personal Settings → App Passwords<br>
    Required permissions: <strong>Repositories: Read, Pull requests: Read</strong>
  </p>
  
  <button class="btn" onclick="save()">Save & Continue to Dashboard</button>
  <div class="skip"><a href="/dashboard">Skip for now →</a></div>
</div>
<script>
async function save() {
  const password = document.getElementById('bbPassword').value.trim();
  if (!password) { alert('Please enter your Bitbucket app password'); return; }
  
  const resp = await fetch('/setup/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ bitbucket_password: password })
  });
  
  if (resp.ok) {
    document.getElementById('success').style.display = 'block';
    setTimeout(() => window.location.href = '/dashboard', 1500);
  } else {
    alert('Failed to save. Please try again.');
  }
}
</script>
</body>
</html>"""

def _inline_login_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DevAgent — Login</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #07090F; --bg2: #0C1018; --border2: #243550;
    --text2: #6B88A8; --text3: #344D68;
    --accent: #2D7DD2; --purple: #8B68F0;
  }
  html, body { height: 100%; background: var(--bg); color: #C4D4E8;
    font-family: 'JetBrains Mono', monospace; font-size: 13px; }
  body { display: flex; align-items: center; justify-content: center; }
  .card {
    background: var(--bg2); border: 1px solid var(--border2);
    border-radius: 12px; padding: 40px 36px; width: 380px; text-align: center;
  }
  .logo-mark {
    width: 52px; height: 52px; border-radius: 12px; margin: 0 auto 20px;
    background: linear-gradient(135deg, var(--accent), var(--purple));
    display: flex; align-items: center; justify-content: center; font-size: 26px;
  }
  h1 { font-size: 22px; font-weight: 800; color: #fff; margin-bottom: 8px; }
  .sub { color: var(--text2); font-size: 12px; margin-bottom: 32px; line-height: 1.6; }
  .login-btn {
    display: flex; align-items: center; justify-content: center; gap: 10px;
    width: 100%; padding: 13px 20px; border-radius: 8px; border: none;
    background: var(--accent); color: #fff; font-size: 13px; font-weight: 700;
    cursor: pointer; text-decoration: none; transition: opacity .15s; font-family: inherit;
  }
  .login-btn:hover { opacity: .85; }
  .footer { margin-top: 24px; font-size: 10px; color: var(--text3); }
  .error-msg {
    background: rgba(232,64,64,.12); border: 1px solid rgba(232,64,64,.3);
    border-radius: 6px; padding: 10px 14px; margin-bottom: 20px;
    color: #E84040; font-size: 11px;
  }
</style>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700;800&display=swap" rel="stylesheet">
</head>
<body>
<div class="card">
  <div class="logo-mark">⚡</div>
  <h1>Dev<span style="color:var(--accent)">Agent</span></h1>
  <p class="sub">Your AI-powered developer briefing assistant.<br>
  Login with your Jira account to see your tickets and mentions.</p>
  <div id="errorMsg" class="error-msg" style="display:none"></div>
  <a href="/auth/login" class="login-btn">Login with Jira</a>
  <p class="footer">Secure login via Atlassian OAuth 2.0<br>We never store your password</p>
</div>
<script>
  const error = new URLSearchParams(window.location.search).get('error');
  if (error) {
    const el = document.getElementById('errorMsg');
    const m = { access_denied:'Login cancelled.', auth_failed:'Auth failed. Try again.',
                invalid_state:'Security error. Try again.', no_code:'No code received.' };
    el.textContent = m[error] || 'Login error: ' + error;
    el.style.display = 'block';
  }
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.server:app", host="0.0.0.0", port=8001, reload=True)