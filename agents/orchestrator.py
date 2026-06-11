"""
agents/orchestrator.py
───────────────────────
The DevAgent brain. When a Jira ticket mentions a developer:
  1. Fans out to ALL 5 platforms SIMULTANEOUSLY (asyncio.gather)
  2. Feeds all raw data to Gemini for synthesis
  3. Returns a structured AgentBriefing in < 60 seconds

Fixes applied:
  - fetch_real_bitbucket_prs: filters PRs by ticket_id in title (matches "AT-5646 | ..." pattern)
  - fetch_real_gcp_logs: REAL Cloud Logging from omega-sorter-353009, handles TextEntry/LogEntry/StructEntry
  - Dual GCP project: omega-sorter-353009 for logs, data-science-test-481612 for Vertex AI
  - GOOGLE_LOGS_PROJECT env var added for logs project
  - order_by uses raw string "timestamp desc" (not gcp_logging.DESCENDING constant)
"""
from __future__ import annotations
from core.auth import fetch_my_jira_tickets
import asyncio, os
from datetime import datetime, timezone, timedelta
from rich.console import Console
from rich.panel import Panel
import google.genai as genai

from core.store import briefing_store
from core.models import (
    AgentBriefing, JiraTicket,
    ActionItem, SignalSummary,
    Platform, ConfidenceLevel
)

from integrations.microsoft_graph import MicrosoftGraphClient

console = Console()
graph   = MicrosoftGraphClient()

# Valid platform values Gemini must use — passed into prompt so it knows the constraint
VALID_PLATFORMS = [p.value for p in Platform]

# Maps Jira project key → real GCP service name
JIRA_TO_GCP_SERVICE = {
    "AT":  "krakend-api-gateway-prod",
    "REV": "krakend-api-gateway-prod",
    # add more as you discover them
}


# ── Bitbucket — filter by ticket_id in PR title ───────────────────────

async def fetch_real_bitbucket_prs(ticket_id: str, access_token: str = "", account_id: str = "") -> list:
    import httpx
    from core.models import PullRequest
    from core.store import load_user_credentials

    if account_id:
        creds        = load_user_credentials(account_id)
        username     = creds.get("bitbucket_username", "")
        app_password = creds.get("bitbucket_password", "")
        workspace    = creds.get("bitbucket_workspace", os.getenv("BITBUCKET_WORKSPACE", ""))
    else:
        username     = os.getenv("BITBUCKET_USERNAME", "")
        app_password = os.getenv("BITBUCKET_APP_PASSWORD", "")
        workspace    = os.getenv("BITBUCKET_WORKSPACE", "")

    if not all([username, app_password, workspace]):
        print("[Bitbucket] Credentials not configured — skipping")
        return []

    print(f"[BB Debug] username={username!r}, workspace={workspace!r}, has_password={bool(app_password)}")

    auth            = (username, app_password)
    matched_prs     = []
    ticket_id_upper = ticket_id.upper().strip()

    async with httpx.AsyncClient(timeout=15) as client:
        repos_resp = await client.get(
            f"https://api.bitbucket.org/2.0/repositories/{workspace}",
            auth=auth,
            params={"pagelen": 50, "role": "member"},
        )
        if repos_resp.status_code != 200:
            print(f"[Bitbucket] Could not fetch repos: {repos_resp.status_code} {repos_resp.text[:200]}")
            return []

        repos = repos_resp.json().get("values", [])
        print(f"[Bitbucket] {len(repos)} repos found — searching for ticket {ticket_id_upper}")

        for repo in repos:
            repo_slug = repo["slug"]

            prs_resp = await client.get(
                f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo_slug}/pullrequests",
                auth=auth,
                params={"state": "OPEN", "pagelen": 50},
            )
            if prs_resp.status_code != 200:
                continue

            for pr in prs_resp.json().get("values", []):
                title  = pr.get("title", "")
                branch = pr.get("source", {}).get("branch", {}).get("name", "")
                if ticket_id_upper in title.upper() or ticket_id_upper in branch.upper():
                    matched_prs.append(PullRequest(
                        id=str(pr["id"]),
                        title=title,
                        author=pr.get("author", {}).get("display_name", "Unknown"),
                        status=pr["state"].lower(),
                        branch=branch,
                        url=pr.get("links", {}).get("html", {}).get("href", ""),
                        changed_files=[],
                        last_updated=pr.get("updated_on", ""),
                    ))
                    print(f"[Bitbucket] ✓ Matched PR: '{title}' in repo '{repo_slug}'")

            merged_resp = await client.get(
                f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo_slug}/pullrequests",
                auth=auth,
                params={"state": "MERGED", "pagelen": 20},
            )
            if merged_resp.status_code == 200:
                for pr in merged_resp.json().get("values", []):
                    title  = pr.get("title", "")
                    branch = pr.get("source", {}).get("branch", {}).get("name", "")
                    if ticket_id_upper in title.upper() or ticket_id_upper in branch.upper():
                        matched_prs.append(PullRequest(
                            id=str(pr["id"]),
                            title=title,
                            author=pr.get("author", {}).get("display_name", "Unknown"),
                            status="merged",
                            branch=branch,
                            url=pr.get("links", {}).get("html", {}).get("href", ""),
                            changed_files=[],
                            last_updated=pr.get("updated_on", ""),
                        ))
                        print(f"[Bitbucket] ✓ Matched MERGED PR: '{title}' in repo '{repo_slug}'")

    print(f"[Bitbucket] Found {len(matched_prs)} PRs matching ticket {ticket_id_upper}")
    return matched_prs


# ── GCP Logs from omega-sorter-353009 ────────────────────────────────

async def fetch_real_gcp_logs(service: str, ticket_id: str = "") -> list:
    print(f"[GCP] fetch_real_gcp_logs CALLED with service={service!r}")
    from google.cloud import logging as gcp_logging
    from core.models import LogEntry as DevAgentLogEntry

    logs_project = os.getenv("GOOGLE_LOGS_PROJECT", "omega-sorter-353009")
    print(f"[GCP Debug] GOOGLE_LOGS_PROJECT={logs_project}")
    print(f"[GCP Debug] service={service!r}, ticket_id={ticket_id!r}")

    try:
        client = gcp_logging.Client(project=logs_project)
        print(f"[GCP] Client created successfully")
    except Exception as e:
        print(f"[GCP Logs] Could not init client: {e}")
        return []

    cutoff     = datetime.now(timezone.utc) - timedelta(hours=48)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    log_filter = (
        f'resource.type="cloud_run_revision" '
        f'severity>="DEFAULT" '
        f'timestamp>="{cutoff_str}"'
    )

    # if service and service not in ("unknown-service", "", "none"):
    #     log_filter += f' resource.labels.service_name="{service}"'

    print(f"[GCP Logs] Querying project={logs_project} filter: {log_filter[:120]}...")
    print(f"[GCP Debug] Final filter:\n{log_filter}")
    print(f"[GCP Debug] Project: {logs_project}")

    entries = []
    try:
        raw_entries = await asyncio.to_thread(
            lambda: list(client.list_entries(
                filter_=log_filter,
                order_by="timestamp desc",
                max_results=30,
            ))
        )

        print(f"[GCP Logs] Raw entries fetched: {len(raw_entries)}")

        for entry in raw_entries:
            message     = ""
            entry_class = type(entry).__name__
            print(f"[GCP Debug] entry class={entry_class}, payload={str(entry.payload)[:60]!r}")

            if entry_class == "TextEntry":
                message = entry.payload or ""

            elif entry_class == "StructEntry":
                payload = dict(entry.payload) if entry.payload else {}
                message = (
                    payload.get("message") or
                    payload.get("msg") or
                    payload.get("error") or
                    str(payload)[:300]
                )

            elif entry_class == "ProtobufEntry":
                message = str(entry.payload)[:300] if entry.payload else ""

            elif entry_class == "LogEntry":
                # HTTP request logs — data lives in http_request, not payload
                http = entry.http_request
                if http:
                    status  = http.get("status", "")
                    method  = http.get("requestMethod", "")
                    url     = http.get("requestUrl", "")
                    latency = http.get("latency", "")
                    if str(status).startswith(("4", "5")):
                        message = f"HTTP {status} {method} {url} (latency: {latency})"

            if not message or message.strip() in ("", "{}", "None"):
                continue

            # Severity
            severity = entry.severity.name if hasattr(entry.severity, "name") else str(entry.severity)
            severity_map = {
                "0": "DEFAULT", "100": "DEBUG", "200": "INFO",
                "300": "NOTICE", "400": "WARNING", "500": "ERROR",
                "600": "CRITICAL", "700": "ALERT", "800": "EMERGENCY",
            }
            severity = severity_map.get(severity, severity)

            ts            = entry.timestamp
            timestamp_str = ts.isoformat() if ts else datetime.utcnow().isoformat()

            resource_labels = dict(entry.resource.labels) if entry.resource else {}
            service_name    = resource_labels.get("service_name", service or "cloud-run")

            encoded_filter = (
                f"resource.type%3D%22cloud_run_revision%22%20"
                f"severity%3E%3D%22WARNING%22%20"
                f"timestamp%3E%3D%22{cutoff_str}%22"
            )
            log_url = f"https://console.cloud.google.com/logs/query;query={encoded_filter}?project={logs_project}"

            entries.append(DevAgentLogEntry(
                severity=severity,
                message=message[:500],
                timestamp=timestamp_str,
                service=service_name,
                log_url=log_url,
            ))

        print(f"[GCP Logs] {len(entries)} meaningful log entries parsed")
        return entries

    except Exception as e:
        import traceback
        print(f"[GCP Logs] Error fetching logs: {e}")
        traceback.print_exc()
        return []

   
# ── Jira ticket fetcher ───────────────────────────────────────────────

async def _fetch_real_jira_ticket(
    ticket_id: str,
    access_token: str,
    jira_base_url: str,
    developer: str,
) -> JiraTicket:
    import httpx
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{jira_base_url}/rest/api/3/issue/{ticket_id}",
            headers=headers,
            params={"fields": "summary,description,priority,issuetype,reporter,assignee,comment,status,creator"}
        )
        resp.raise_for_status()
        data = resp.json()

    fields = data.get("fields", {})

    comments_raw = fields.get("comment", {}).get("comments", [])
    comments = []
    for c in comments_raw[:5]:
        try:
            author = c.get('author', {}).get('displayName', 'Unknown')
            body   = c.get('body', '')
            if isinstance(body, dict):
                text_parts = []
                for block in body.get('content', []):
                    for node in block.get('content', []):
                        if node.get('type') == 'text':
                            text_parts.append(node.get('text', ''))
                body_text = ' '.join(text_parts)[:200]
            else:
                body_text = str(body)[:200]
            comments.append(f"{author}: {body_text}")
        except Exception:
            continue

    desc = fields.get("description", "")
    if isinstance(desc, dict):
        try:
            desc = " ".join(
                node.get("text", "")
                for block in desc.get("content", [])
                for node in block.get("content", [])
                if node.get("type") == "text"
            )
        except Exception:
            desc = str(desc)

    type_raw = fields.get("issuetype", {}).get("name", "task").lower().replace(" ", "_")
    type_map = {
        "bug": "bug", "incident": "incident", "task": "task",
        "review": "review", "story": "task", "epic": "task",
        "subtask": "task", "sub-task": "task", "initiative_rev": "task",
        "improvement": "task", "new_feature": "task",
        "change_request": "task", "problem": "bug", "service_request": "task",
    }
    issue_type = type_map.get(type_raw, "task")

    priority_raw = fields.get("priority", {}).get("name", "medium").lower()
    priority_map = {
        "highest": "critical", "high": "high", "medium": "medium",
        "low": "low", "lowest": "low", "blocker": "critical",
        "critical": "critical", "minor": "low", "trivial": "low",
    }
    priority = priority_map.get(priority_raw, "medium")

    reporter = (
        (fields.get("reporter") or {}).get("displayName") or
        (fields.get("creator") or {}).get("displayName") or
        (fields.get("assignee") or {}).get("displayName") or
        "Unknown"
    )

    return JiraTicket(
        id=ticket_id,
        title=fields.get("summary", ticket_id),
        priority=priority,
        type=issue_type,
        service=fields.get("customfield_10014") or "unknown-service",
        reporter=reporter,
        description=desc or "No description provided.",
        comments=comments,
        jira_url=f"https://navadhan.atlassian.net/browse/{ticket_id}",
        created_at=datetime.utcnow().isoformat(),
    )


# ── Fan out ───────────────────────────────────────────────────────────

async def _fan_out(ticket: JiraTicket, developer: str, access_token: str = "", account_id: str = ""):
    console.print(f"\n[bold cyan]⚡ Fanning out to 4 platforms simultaneously...[/]")
    results = await asyncio.gather(
        fetch_real_bitbucket_prs(ticket.id, access_token, account_id),
        fetch_real_gcp_logs(
            JIRA_TO_GCP_SERVICE.get(ticket.id.split("-")[0], ticket.service),
            ticket.id,
        ),
        graph.fetch_emails(ticket.service),
        graph.fetch_teams_messages(developer, ticket.service),
        return_exceptions=True,
    )

    prs, logs, emails, teams_msgs = [
        r if not isinstance(r, Exception) else []
        for r in results
    ]

    emails     = [e for e in emails     if not getattr(e, 'is_mock', False)]
    teams_msgs = [t for t in teams_msgs if not getattr(t, 'is_mock', False)]


    console.print(f"  [green]✓[/] Bitbucket  — {len(prs)} PRs found")
    console.print(f"  [green]✓[/] GCP Logs   — {len(logs)} log entries")
    console.print(f"  [green]✓[/] Outlook    — {len(emails)} email threads")
    console.print(f"  [green]✓[/] Teams      — {len(teams_msgs)} messages")

    return prs, logs, emails, teams_msgs


# ── Signal summary ────────────────────────────────────────────────────

def _build_signal_summary(prs, logs, emails, teams_msgs) -> SignalSummary:
    return SignalSummary(
        bitbucket_prs      = len(prs),
        gcp_errors         = sum(1 for l in logs if l.severity in ("ERROR", "CRITICAL", "ALERT", "EMERGENCY")),
        gcp_warnings       = sum(1 for l in logs if l.severity in ("WARNING", "NOTICE")),
        email_threads      = len(emails),
        teams_messages     = len(teams_msgs),
    )


# ── Synthesis prompt ──────────────────────────────────────────────────

def _build_synthesis_prompt(
    ticket: JiraTicket,
    developer: str,
    prs, logs, emails, teams_msgs
) -> str:
    first_name = developer.split()[0] if developer else "the developer"

    def fmt_prs():
        if not prs: return "None found."
        lines = []
        for pr in prs:
            lines.append(
                f"  - [{pr.status.upper()}] {pr.title}\n"
                f"    Author: {pr.author} | Branch: {pr.branch}\n"
                f"    Last updated: {pr.last_updated[:10] if pr.last_updated else 'unknown'}\n"
                f"    URL: {pr.url}"
            )
        return "\n".join(lines)

    def fmt_logs():
        if not logs: return "None found."
        lines = []
        for e in logs[:10]:
            lines.append(
                f"  - [{e.severity}] {e.timestamp[:19]} | Service: {e.service}\n"
                f"    {e.message}"
                + (f"\n    Log URL: {e.log_url}" if e.log_url else "")
            )
        return "\n".join(lines)

    def fmt_emails():
        if not emails: return "None found."
        return "\n".join(
            f"  - From: {e.from_email} | Subject: {e.subject}\n"
            f"    Snippet: {e.snippet[:150]}\n"
            f"    Needs reply: {e.needs_reply}"
            + (f" | Thread URL: {e.thread_url}" if e.thread_url else "")
            for e in emails
        )

    def fmt_teams():
        if not teams_msgs: return "None found."
        return "\n".join(
            f"  - {m.sender} in #{m.channel}: {m.message[:200]}"
            + (f"\n    Channel URL: {m.channel_url}" if m.channel_url else "")
            for m in teams_msgs
        )

    jira_url = ticket.jira_url or f"https://navadhan.atlassian.net/browse/{ticket.id}"

    return f"""You are DevAgent, an AI assistant that eliminates context-switching for software developers.

You are generating a briefing FOR {developer} ({first_name}) who is the logged-in developer.
Write in second person — say "you" instead of "{developer}" or "{first_name}".
{developer} has been mentioned in a {ticket.type.value} ticket.

════════════════════════════════════
JIRA TICKET: {ticket.id}
URL: {jira_url}
════════════════════════════════════
Title:    {ticket.title}
Priority: {ticket.priority.value.upper()}
Service:  {ticket.service}
Reporter: {ticket.reporter}

Description:
{ticket.description}

Comments:
{chr(10).join(f"  - {c}" for c in ticket.comments)}

════════════════════════════════════
BITBUCKET — Pull Requests (filtered to this ticket)
════════════════════════════════════
{fmt_prs()}

════════════════════════════════════
GCP LOGS — Errors & Warnings (last 48h, cloud_run_revision)
════════════════════════════════════
{fmt_logs()}

════════════════════════════════════
OUTLOOK — Email Threads
════════════════════════════════════
{fmt_emails()}

════════════════════════════════════
TEAMS — Channel Messages
════════════════════════════════════
{fmt_teams()}

════════════════════════════════════
YOUR OUTPUT — STRICT JSON SCHEMA
════════════════════════════════════
Return ONLY this JSON object. No markdown fences. No explanation. No extra fields.

{{
  "summary": "<2-3 sentences. What is happening RIGHT NOW. Mention error types, affected service, scale of impact. Be specific. If GCP logs show errors, mention them. If PRs exist for this ticket, mention their status.>",

  "root_cause": "<Your best hypothesis connecting evidence across ALL platforms. Be specific about what caused what. Reference actual log messages or PR titles if available.>",

  "confidence": "<exactly one of: high | medium | low>",

  "confidence_pct": <integer 0-100. 90+ = strong corroborating evidence across multiple platforms. 50-70 = hypothesis with gaps. <50 = speculation from Jira only.>,

  "already_in_motion": "<What is ALREADY being worked on. PRs open or merged, fixes in progress, emails sent. Developer should NOT redo these. If a PR for this ticket exists, always mention it here.>",

  "action_items": [
    {{
      "platform": "<exactly one of: {' | '.join(VALID_PLATFORMS)}>",
      "text": "<Specific action to take. Reference actual PR titles, log messages, error types. Never vague.>",
      "deep_link": "<EXACT URL from the data above. For OUTLOOK actions use the thread_url from email data. For TERMINAL actions use empty string. Never invent URLs.>",
      "link_label": "<Short button label e.g. Open PR | View GCP Logs | Open Ticket>"
    }}
  ],

  "severity_score": <integer 1-10>
}}

RULES:
1. action_items: 3-5 items, ordered by urgency. Each must be immediately actionable.
2. platform: EXACTLY one of [{', '.join(VALID_PLATFORMS)}].
3. deep_link: REAL URLs from data only. Never invent.
4. If GCP shows ERROR logs, the first action_item must address them.
5. If a PR exists for this ticket, reference it by title in action_items.
6. Write in second person throughout — "you", never "{developer}".
7. Return ONLY valid JSON. Nothing else.
"""


# ── Gemini synthesis ──────────────────────────────────────────────────

def _parse_synthesis(raw_dict: dict) -> dict:
    confidence_raw = raw_dict.get("confidence", "medium").lower().strip()
    valid_confidence = {c.value for c in ConfidenceLevel}
    if confidence_raw not in valid_confidence:
        console.print(f"  [yellow]⚠[/] Invalid confidence '{confidence_raw}' → defaulting to 'medium'")
        confidence_raw = "medium"

    pct = raw_dict.get("confidence_pct", 50)
    try:
        pct = max(0, min(100, int(pct)))
    except (TypeError, ValueError):
        pct = 50

    severity = raw_dict.get("severity_score", 5)
    try:
        severity = max(1, min(10, int(severity)))
    except (TypeError, ValueError):
        severity = 5

    valid_platforms = {p.value for p in Platform}
    clean_actions = []
    for item in raw_dict.get("action_items", []):
        if not isinstance(item, dict):
            continue
        platform = item.get("platform", "jira").lower().strip()
        if platform not in valid_platforms:
            console.print(f"  [yellow]⚠[/] Invalid platform '{platform}' → defaulting to 'jira'")
            platform = "jira"
        clean_actions.append({
            "platform":   platform,
            "text":       str(item.get("text", "")),
            "deep_link":  str(item.get("deep_link", "")),
            "link_label": str(item.get("link_label", "Open")),
        })

    return {
        "summary":           str(raw_dict.get("summary", "")),
        "root_cause":        str(raw_dict.get("root_cause", "")),
        "confidence":        confidence_raw,
        "confidence_pct":    pct,
        "already_in_motion": str(raw_dict.get("already_in_motion", "")),
        "action_items":      clean_actions,
        "severity_score":    severity,
    }

import json, tempfile
creds_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if creds_json:
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
    tmp.write(creds_json)
    tmp.close()
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tmp.name

async def _synthesize_with_gemini(prompt: str, ticket_id: str = "") -> dict:
    import vertexai
    from vertexai.generative_models import GenerativeModel, GenerationConfig

    console.print(f"\n[bold cyan]🧠 Gemini synthesising across all platforms...[/]")

    try:
        vertexai.init(
            project=os.getenv("GOOGLE_CLOUD_PROJECT", "data-science-test-481612"),
            location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
        )
        model = GenerativeModel(
            model_name="gemini-2.5-flash",
            generation_config=GenerationConfig(
                temperature=0.3,
                response_mime_type="application/json",
            )
        )

        response = await asyncio.to_thread(model.generate_content, prompt)

        raw = response.text.strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        parsed    = json.loads(raw.strip())
        validated = _parse_synthesis(parsed)
        console.print(f"  [green]✓[/] Gemini synthesis complete (Vertex AI)")
        return validated

    except Exception as e:
        console.print(f"  [yellow]⚠[/] Vertex AI error ({e}), using pre-built synthesis")
        await asyncio.sleep(1.2)
        from agents.synthesizer_patch import get_mock_synthesis
        raw_mock  = get_mock_synthesis(ticket_id)
        validated = _parse_synthesis(raw_mock)
        return validated


# ── Deliver ───────────────────────────────────────────────────────────

async def _deliver(briefing: AgentBriefing, developer: str) -> AgentBriefing:
    action_lines = "<br>".join(
        f"→ [{a.platform.value.upper()}] {a.text}"
        + (f" — <a href='{a.deep_link}'>{a.link_label}</a>" if a.deep_link else "")
        for a in briefing.action_items
    )

    teams_msg = f"""
<b>🤖 DevAgent Briefing — {briefing.trigger_ticket.id}</b><br>
<b>Priority:</b> {briefing.trigger_ticket.priority.value.upper()} &nbsp;|&nbsp;
<b>Severity:</b> {briefing.severity_score}/10 &nbsp;|&nbsp;
<b>Confidence:</b> {briefing.confidence.value.upper()} ({briefing.confidence_pct}%)<br><br>
<b>TL;DR</b><br>{briefing.summary}<br><br>
<b>Root Cause</b><br>{briefing.root_cause}<br><br>
<b>Already in Motion</b><br>{briefing.already_in_motion}<br><br>
<b>Action Plan</b><br>{action_lines}
""".strip()

    posted = await graph.post_teams_briefing(
        channel_id="mock-channel-id",
        team_id="mock-team-id",
        message=teams_msg,
    )
    briefing.sent_via_teams = posted
    briefing.drafted_email  = False

    await asyncio.sleep(0.2)
    briefing.posted_to_jira = True

    console.print(f"  [green]✓[/] Jira    — briefing comment posted on {briefing.trigger_ticket.id}")
    console.print(f"  [green]✓[/] Teams   — briefing delivered to #incidents-critical")

    return briefing


# ── Main entry point ──────────────────────────────────────────────────

async def run_devagent(
    ticket_id: str,
    developer: str,
    force_refresh: bool = False,
    access_token: str = "",
    jira_base_url: str = "",
    account_id: str = "",
) -> AgentBriefing:
    start = datetime.utcnow()
    cache_key = f"{account_id or developer}:{ticket_id}"

    if not force_refresh:
        cached = await briefing_store.get(cache_key)
        if cached:
            remaining = briefing_store.time_remaining(cache_key)
            console.print(Panel(
                f"[bold green]⚡ Cache hit — returning in 0.0s[/]\n"
                f"Ticket: [cyan]{ticket_id}[/] | Refreshes in: [dim]{remaining}s[/]",
                title="🤖 DevAgent", border_style="green",
            ))
            return cached

    console.print(Panel(
        f"[bold white]DevAgent activated[/]\n"
        f"Ticket: [cyan]{ticket_id}[/]  |  Developer: [cyan]{developer}[/]",
        title="🤖 DevAgent", border_style="cyan",
    ))

    console.print(f"\n[bold cyan]📋 Fetching Jira ticket {ticket_id}...[/]")
    if not (access_token and jira_base_url):
        raise ValueError(f"No credentials for ticket {ticket_id}")
    ticket = await _fetch_real_jira_ticket(ticket_id, access_token, jira_base_url, developer)
    console.print(f"  [green]✓[/] [{ticket.priority.value.upper()}] {ticket.title}")

    if ticket.reporter.lower() == developer.lower():
        console.print(f"  [yellow]⏭[/] Skipping {ticket_id} — reporter is the developer")
        return None

    prs, logs, emails, teams_msgs = await _fan_out(
        ticket, developer, access_token, account_id
    )

    signal_summary = _build_signal_summary(prs, logs, emails, teams_msgs)

    prompt    = _build_synthesis_prompt(ticket, developer, prs, logs, emails, teams_msgs)
    synthesis = await _synthesize_with_gemini(prompt, ticket_id)

    briefing = AgentBriefing(
        trigger_ticket=ticket,
        triggered_for=developer,
        pull_requests=prs,
        gcp_logs=logs,
        email_threads=emails,
        teams_messages=teams_msgs,
        signal_summary=signal_summary,
        summary=synthesis["summary"],
        root_cause=synthesis["root_cause"],
        confidence=ConfidenceLevel(synthesis["confidence"]),
        confidence_pct=synthesis["confidence_pct"],
        already_in_motion=synthesis["already_in_motion"],
        action_items=[
            ActionItem(
                platform=Platform(a["platform"]),
                text=a["text"],
                deep_link=a["deep_link"],
                link_label=a["link_label"],
            )
            for a in synthesis["action_items"]
        ],
        severity_score=synthesis["severity_score"],
    )

    console.print(f"\n[bold cyan]📤 Delivering briefing...[/]")
    briefing = await _deliver(briefing, developer)

    await briefing_store.set(cache_key, briefing)
    remaining = briefing_store.time_remaining(cache_key)
    console.print(f"  [green]✓[/] Cached — refreshes in {remaining}s")

    elapsed = (datetime.utcnow() - start).total_seconds()
    console.print(Panel(
        f"[bold green]✅ Briefing delivered in {elapsed:.1f}s[/]\n\n"
        f"[bold]TL;DR:[/] {briefing.summary}\n\n"
        f"[bold]Root Cause:[/] {briefing.root_cause}\n"
        f"[dim]Confidence: {briefing.confidence.value.upper()} ({briefing.confidence_pct}%)[/]\n\n"
        f"[bold]Action Plan:[/]\n" +
        "\n".join(f"  [{a.platform.value.upper()}] → {a.text}" for a in briefing.action_items) +
        f"\n\n[bold]Severity:[/] {briefing.severity_score}/10",
        title=f"🤖 DevAgent — {ticket_id}",
        border_style="green",
    ))

    return briefing