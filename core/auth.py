"""
core/auth.py
─────────────
Atlassian OAuth 2.0 (3-legged) authentication for DevAgent.

Flow:
  1. GET /auth/login      → redirect to Atlassian consent page
  2. GET /auth/callback   → exchange code for token, get user info, set session
  3. GET /auth/logout     → clear session
  4. GET /auth/me         → return current user info (for dashboard)

Session is stored in a signed cookie — no database needed.
Each session contains:
  - access_token   : Atlassian OAuth token (used for Jira API calls)
  - display_name   : developer's real name from Atlassian
  - email          : developer's email
  - account_id     : Atlassian account ID
  - cloud_id       : Jira cloud instance ID (needed for API calls)
  - jira_base_url  : full Jira URL for this user's instance
"""
from __future__ import annotations
import os, secrets, json
import httpx
from urllib.parse import urlencode
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

# ── OAuth config ───────────────────────────────────────────────────────
CLIENT_ID       = os.getenv("ATLASSIAN_CLIENT_ID", "")
CLIENT_SECRET   = os.getenv("ATLASSIAN_CLIENT_SECRET", "")
CALLBACK_URL    = os.getenv("ATLASSIAN_CALLBACK_URL", "http://localhost:8001/auth/callback")
SECRET_KEY      = os.getenv("SESSION_SECRET", secrets.token_hex(32))

# Atlassian OAuth endpoints
AUTH_URL        = "https://auth.atlassian.com/authorize"
TOKEN_URL       = "https://auth.atlassian.com/oauth/token"
ACCESSIBLE_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
USER_INFO_URL   = "https://api.atlassian.com/me"

# Scopes needed
SCOPES = "read:jira-work read:jira-user read:me read:account offline_access repository pullrequest account"
# Session serializer — signs cookies so they can't be tampered with
_serializer = URLSafeTimedSerializer(SECRET_KEY)

# In-memory state store to prevent CSRF (state param)
_pending_states: set[str] = set()


# ── Step 1: Build authorization URL ───────────────────────────────────

def get_authorization_url() -> tuple[str, str]:
    """
    Build the Atlassian OAuth authorization URL.
    Returns (url, state) — state must be stored and verified in callback.
    """
    state = secrets.token_urlsafe(32)
    _pending_states.add(state)

    params = {
        "audience":      "api.atlassian.com",
        "client_id":     CLIENT_ID,
        "scope":         SCOPES,
        "redirect_uri":  CALLBACK_URL,
        "state":         state,
        "response_type": "code",
        "prompt":        "consent",
    }
    url = f"{AUTH_URL}?{urlencode(params)}"
    return url, state


# ── Step 2: Exchange code for token ───────────────────────────────────

async def exchange_code_for_token(code: str) -> dict:
    """Exchange authorization code for access token."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(TOKEN_URL, json={
            "grant_type":    "authorization_code",
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code":          code,
            "redirect_uri":  CALLBACK_URL,
        })
        resp.raise_for_status()
        return resp.json()
    
    # ADD THESE TWO LINES:
    print("TOKEN EXCHANGE STATUS:", resp.status_code)
    print("TOKEN EXCHANGE BODY:", resp.text)
    resp.raise_for_status()
    return resp.json()


# ── Step 3: Get user info ──────────────────────────────────────────────

async def get_user_info(access_token: str) -> dict:
    """
    Fetch the logged-in user's profile from Atlassian.
    Returns display_name, email, account_id.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(USER_INFO_URL, headers=headers)
        resp.raise_for_status()
        return resp.json()


# ── Step 4: Get accessible Jira resources ─────────────────────────────

async def get_cloud_id(access_token: str) -> tuple[str, str]:
    """
    Get the user's Jira Cloud instance ID and URL.
    Returns (cloud_id, jira_base_url).
    Needed for all Jira API calls via api.atlassian.com.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(ACCESSIBLE_RESOURCES_URL, headers=headers)
        resp.raise_for_status()
        resources = resp.json()

    if not resources:
        raise ValueError("No accessible Jira resources found for this account")

    # Use the first accessible Jira cloud instance
    resource     = resources[0]
    cloud_id     = resource["id"]
    jira_base_url = f"https://api.atlassian.com/ex/jira/{cloud_id}"
    return cloud_id, jira_base_url


# ── Session management ─────────────────────────────────────────────────

def create_session_cookie(user_data: dict) -> str:
    """
    Serialize user data into a signed cookie value.
    user_data should contain: access_token, display_name, email,
                               account_id, cloud_id, jira_base_url
    """
    return _serializer.dumps(user_data)


def decode_session_cookie(cookie_value: str, max_age: int = 86400 * 7) -> dict | None:
    """
    Decode and verify a session cookie.
    Returns user data dict, or None if invalid/expired.
    max_age: 7 days by default.
    """
    try:
        return _serializer.loads(cookie_value, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


def verify_state(state: str) -> bool:
    """Verify OAuth state param to prevent CSRF. Returns True if valid."""
    if state in _pending_states:
        _pending_states.discard(state)
        return True
    return False


# ── Jira API calls using OAuth token ──────────────────────────────────

async def fetch_my_jira_tickets(
    access_token: str,
    jira_base_url: str,
    display_name: str,
    is_first_poll: bool = True,
) -> list[dict]:
    from datetime import datetime, timedelta

    if is_first_poll:
        jql = (
            '(assignee = currentUser() OR mention = currentUser()) '
            'AND statusCategory != Done '
            'ORDER BY updated DESC'
        )
    else:
        minutes_ago = (datetime.utcnow() - timedelta(seconds=35)).strftime("%Y-%m-%d %H:%M")
        jql = (
            '(assignee = currentUser() OR mention = currentUser()) '
            f'AND updated >= "{minutes_ago}" '
            'ORDER BY updated DESC'
        )

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept":        "application/json",
    }

    print(f"[Jira JQL] {jql}")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{jira_base_url}/rest/api/3/search/jql",
            json={
                "jql":        jql,
                "maxResults": 100,
                "fields":     ["summary", "assignee", "description", "comment",
                               "priority", "issuetype", "status", "reporter", "created"],
            },
            headers={**headers, "Content-Type": "application/json"},
        )
        print(f"[Jira] Status: {resp.status_code}")
        print(f"[Jira] Response: {resp.text[:500]}")

        if resp.status_code == 401:
            raise PermissionError("Jira token expired — user needs to log in again")

        resp.raise_for_status()
        issues = resp.json().get("issues", [])

    results = []
    for issue in issues:
        fields = issue.get("fields", {})
        results.append({
            "id":       issue["key"],
            "title":    fields.get("summary", issue["key"]),
            "priority": (fields.get("priority") or {}).get("name", "medium").lower(),
            "type":     (fields.get("issuetype") or {}).get("name", "task").lower(),
            "reporter": (fields.get("reporter") or {}).get("displayName", "Unknown"),
            "jira_url": f"https://navadhan.atlassian.net/browse/{issue['key']}",
            "status":   (fields.get("status") or {}).get("name", ""),
            "created":  fields.get("created", ""),
        })

    return results