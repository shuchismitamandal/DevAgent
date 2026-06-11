"""
core/models.py
──────────────
Shared data models for DevAgent. Every agent speaks these types.

Changes from v1:
  - ActionItem replaces plain strings in action_items
  - AgentBriefing gains confidence + confidence_pct on root cause
  - AgentBriefing gains signal_summary for dashboard stat chips
  - Platform enum added so platform tags are never free-text
"""
from __future__ import annotations
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field
from datetime import datetime


# ── Enums ─────────────────────────────────────────────────────────────

class TicketPriority(str, Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    MEDIUM   = "medium"
    LOW      = "low"

class TicketType(str, Enum):
    BUG      = "bug"
    INCIDENT = "incident"
    REVIEW   = "review"
    TASK     = "task"

class ConfidenceLevel(str, Enum):
    HIGH   = "high"
    MEDIUM = "medium"
    LOW    = "low"

class Platform(str, Enum):
    """Fixed set of platforms DevAgent knows about.
    Gemini must use only these values when tagging action items.
    Prevents free-text platform names that break the UI.
    """
    JIRA      = "jira"
    BITBUCKET = "bitbucket"
    GCP       = "gcp"
    OUTLOOK   = "outlook"
    TEAMS     = "teams"
    BOLDESK   = "boldesk"
    TERMINAL  = "terminal"   # for local code/config changes with no external URL


# ── Raw data collected by each sub-agent ──────────────────────────────

class JiraTicket(BaseModel):
    id:           str
    title:        str
    description:  str
    priority:     TicketPriority
    type:         TicketType
    reporter:     str
    assignee:     Optional[str] = None
    service:      str
    created_at:   str
    comments:     list[str] = Field(default_factory=list)
    jira_url:     str = ""                   # deep link to this ticket

class PullRequest(BaseModel):
    id:            str
    title:         str
    author:        str
    status:        str                       # open / merged / declined
    branch:        str
    url:           str                       # deep link to this PR
    changed_files: list[str] = Field(default_factory=list)
    last_updated:  str

class GCPLogEntry(BaseModel):
    timestamp:  str
    severity:   str                          # ERROR / WARNING / INFO
    service:    str
    message:    str
    trace_id:   Optional[str] = None
    log_url:    str = ""                     # deep link to GCP log explorer

class EmailThread(BaseModel):
    subject:     str
    from_email:  str
    snippet:     str
    received_at: str
    thread_id:   str
    needs_reply: bool = False
    thread_url:  str = ""                    # deep link to Outlook thread

class TeamsMessage(BaseModel):
    sender:      str
    message:     str
    channel:     str
    sent_at:     str
    is_mention:  bool = False
    channel_url: str = ""                    # deep link to Teams channel

class SupportComplaint(BaseModel):
    ticket_id:   str
    customer:    str
    subject:     str
    description: str
    severity:    str
    created_at:  str
    ticket_url:  str = ""                    # deep link to Boldesk ticket


# ── NEW: Structured action item ────────────────────────────────────────

class ActionItem(BaseModel):
    """
    A single step in the developer's action plan.
    Replaces plain strings — now carries platform tag + deep link.

    platform:   one of Platform enum values — drives badge colour in UI
    text:       what to do, written specifically (never "investigate the issue")
    deep_link:  exact URL — specific PR, log filter, email thread, ticket
    link_label: button label shown in UI e.g. "Open PR-381"

    If no external URL exists (local code change),
    set platform = "terminal" and deep_link = "".
    """
    platform:   Platform
    text:       str
    deep_link:  str = ""
    link_label: str = ""


# ── NEW: Signal summary for dashboard stat chips ───────────────────────

class SignalSummary(BaseModel):
    """
    Count of items found per platform.
    Auto-computed by orchestrator after fan-out — NOT from Gemini.
    Shown as stat chips in the dashboard.
    """
    bitbucket_prs:      int = 0
    gcp_errors:         int = 0
    gcp_warnings:       int = 0
    email_threads:      int = 0
    teams_messages:     int = 0

    def to_chips(self) -> list[dict]:
        """Returns chip list for the dashboard renderer."""
        chips = []
        if self.bitbucket_prs > 0:
            chips.append({"platform": "Bitbucket", "count": self.bitbucket_prs,      "label": "PRs found",     "color": "purple"})
        if self.gcp_errors > 0:
            chips.append({"platform": "GCP",       "count": self.gcp_errors,         "label": "Errors",        "color": "red"})
        if self.gcp_warnings > 0:
            chips.append({"platform": "GCP",       "count": self.gcp_warnings,       "label": "Warnings",      "color": "amber"})
        if self.email_threads > 0:
            chips.append({"platform": "Outlook",   "count": self.email_threads,      "label": "Email threads", "color": "amber"})
        if self.teams_messages > 0:
            chips.append({"platform": "Teams",     "count": self.teams_messages,     "label": "Messages",      "color": "blue"})
       


# ── Updated AgentBriefing ─────────────────────────────────────────────

class AgentBriefing(BaseModel):
    """Final structured output delivered to the developer."""

    # Trigger context
    trigger_ticket: JiraTicket
    triggered_for:  str
    generated_at:   str = Field(default_factory=lambda: datetime.utcnow().isoformat())

    # Raw signals per sub-agent
    pull_requests:      list[PullRequest]      = Field(default_factory=list)
    gcp_logs:           list[GCPLogEntry]      = Field(default_factory=list)
    email_threads:      list[EmailThread]      = Field(default_factory=list)
    teams_messages:     list[TeamsMessage]     = Field(default_factory=list)
    # support_complaints: list[SupportComplaint] = Field(default_factory=list)

    # Signal summary (auto-computed, not LLM)
    signal_summary:    SignalSummary = Field(default_factory=SignalSummary)

    # LLM-synthesized intelligence
    summary:           str = ""
    root_cause:        str = ""

    # NEW: confidence on root cause hypothesis
    confidence:        ConfidenceLevel = ConfidenceLevel.MEDIUM
    confidence_pct:    int = 50          # 0-100

    already_in_motion: str = ""

    # NEW: structured action items (was list[str])
    action_items:      list[ActionItem] = Field(default_factory=list)

    severity_score:    int = 5           # 1-10

    # Delivery status
    posted_to_jira: bool = False
    sent_via_teams: bool = False
    drafted_email:  bool = False