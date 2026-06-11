"""
agents/synthesizer_patch.py
────────────────────────────
Pre-built mock synthesis responses — used when ANTHROPIC_API_KEY is not set.
Updated to match the new structured format:
  - action_items are objects with platform/text/deep_link/link_label
  - confidence + confidence_pct added
"""

MOCK_SYNTHESIS = {
    "PAY-4821": {
        "summary": "Payment-service has a 12% transaction failure rate since the v2.4.1 deployment at 14:15 IST. GCP logs show Razorpay gateway timeouts (5s limit) causing a NullPointerException when transaction_id is None, which tripped the circuit breaker. 3 enterprise clients including Reliance Retail have escalated via Boldesk.",
        "root_cause": "v2.4.1 added retry logic but did not increase the gateway timeout (still 5s). Razorpay is responding slowly, causing timeouts → NoneType on transaction_id → circuit breaker trips. The DB migration at 13:45 adding a NOT NULL constraint on gateway_ref compounds failures on retry — retried transactions lack this field.",
        "confidence": "high",
        "confidence_pct": 87,
        "already_in_motion": "Rahul Mehta has PR-381 open (fix/razorpay-timeout) increasing timeout from 5s to 15s — directly addresses root cause. Priya Sharma is managing stakeholder comms. PagerDuty alert active and team engaged in #incidents-critical.",
        "action_items": [
            {
                "platform": "bitbucket",
                "text": "Review and merge PR-381 (fix/razorpay-timeout) — increases Razorpay timeout from 5s to 15s, directly resolves the circuit breaker trigger",
                "deep_link": "https://bitbucket.org/company/payment-service/pull-requests/381",
                "link_label": "Open PR-381"
            },
            {
                "platform": "gcp",
                "text": "Verify DB migration impact — query transactions WHERE gateway_ref IS NULL AND created_at > '2024-01-15 13:45' to confirm if NOT NULL constraint is blocking retries",
                "deep_link": "https://console.cloud.google.com/logs",
                "link_label": "View GCP Logs"
            },
            {
                "platform": "jira",
                "text": "If error rate doesn't drop within 10 min of PR-381 merge, initiate rollback of v2.4.1 to v2.4.0 and update ticket status",
                "deep_link": "https://company.atlassian.net/browse/PAY-4821",
                "link_label": "Open Jira"
            },
            {
                "platform": "outlook",
                "text": "Reply to Priya Sharma's escalation email with ETA — 3 enterprise clients are waiting and she needs an update for stakeholders",
                "deep_link": "",
                "link_label": "Reply Email"
            },
        ],
        "severity_score": 9
    },
    "AUTH-1092": {
        "summary": "Auth-service token refresh is failing for ~8,000 iOS 17.4 users. WebKit's new strict SameSite cookie policy blocks refresh tokens from being sent cross-origin, causing 401s and unexpected logouts. HDFC Broker Portal (corporate client) has escalated.",
        "root_cause": "iOS 17.4 WebKit enforces stricter SameSite cookie policies — refresh tokens stored as SameSite=None cookies are blocked cross-origin. The token_refresh endpoint returns 401 when the cookie is absent instead of falling back to an Authorization header check. Session expiry rate is 7x above baseline confirming scale.",
        "confidence": "high",
        "confidence_pct": 92,
        "already_in_motion": "You already have PR-204 open (fix/ios-17-cookie-policy) modifying cookie_handler.py and token_refresh.py. Sneha confirmed reproduction on iPhone 14 iOS 17.4.1. Android unaffected.",
        "action_items": [
            {
                "platform": "bitbucket",
                "text": "Complete and merge your PR-204 (fix/ios-17-cookie-policy) — adds SameSite=None fallback handling for iOS 17.4 WebKit",
                "deep_link": "https://bitbucket.org/company/auth-service/pull-requests/204",
                "link_label": "Open PR-204"
            },
            {
                "platform": "terminal",
                "text": "Add fallback in token_refresh.py: if refresh cookie is missing, check Authorization header for bearer token as alternative refresh path",
                "deep_link": "",
                "link_label": ""
            },
            {
                "platform": "boldesk",
                "text": "Post update on HDFC Broker Portal ticket BSD-9871 — they have been waiting 95 minutes with all iOS users locked out",
                "deep_link": "https://company.boldesk.com/tickets/9871",
                "link_label": "Open Ticket"
            },
            {
                "platform": "jira",
                "text": "Update AUTH-1092 with root cause and link PR-204 as the fix — change status to In Progress",
                "deep_link": "https://company.atlassian.net/browse/AUTH-1092",
                "link_label": "Open Jira"
            },
        ],
        "severity_score": 7
    },
}

def get_mock_synthesis(ticket_id: str) -> dict:
    return MOCK_SYNTHESIS.get(ticket_id, {
        "summary": "DevAgent collected context from 4 platforms. No critical signals detected.",
        "root_cause": "Insufficient data to determine root cause. Review GCP logs and recent PRs for the affected service.",
        "confidence": "low",
        "confidence_pct": 35,
        "already_in_motion": "No parallel investigation detected. Check Teams for any context.",
        "action_items": [
            {
                "platform": "jira",
                "text": "Read the full ticket description and comments for context before taking action",
                "deep_link": f"https://company.atlassian.net/browse/{ticket_id}",
                "link_label": "Open Jira"
            },
            {
                "platform": "gcp",
                "text": "Check GCP error logs for the affected service in the last 2 hours",
                "deep_link": "https://console.cloud.google.com/logs",
                "link_label": "View GCP Logs"
            },
            {
                "platform": "teams",
                "text": "Sync with team on #incidents channel for any context not captured in the ticket",
                "deep_link": "https://teams.microsoft.com",
                "link_label": "Open Teams"
            },
        ],
        "severity_score": 5
    })