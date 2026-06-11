from google.adk.agents import Agent, ParallelAgent
from google.adk.agents.llm_agent import LlmAgent
import asyncio, os

# ── Tool functions (wrap your existing logic) ──

def fetch_jira_ticket(ticket_id: str) -> dict:
    """Fetch a real Jira ticket by ID."""
    # Import your existing real fetch function
    import asyncio
    from agents.orchestrator import _fetch_real_jira_ticket
    # Return ticket data as dict for ADK
    return {"ticket_id": ticket_id, "status": "fetched"}

def fetch_bitbucket_prs(service: str) -> dict:
    """Fetch Bitbucket PRs for a service."""
    return {"prs": [], "service": service}

def fetch_gcp_logs(service: str) -> dict:
    """Fetch GCP logs for a service."""
    return {"logs": [], "service": service}

def fetch_outlook_emails(developer: str) -> dict:
    """Fetch Outlook emails mentioning developer."""
    return {"emails": [], "developer": developer}

def fetch_teams_messages(developer: str) -> dict:
    """Fetch Teams messages mentioning developer."""
    return {"messages": [], "developer": developer}

# ── Individual platform agents ──

jira_agent = LlmAgent(
    name="JiraAgent",
    model="gemini-2.5-flash",
    description="Fetches and analyses Jira ticket details",
    instruction="Fetch the Jira ticket and extract key information: title, priority, description, comments.",
    tools=[fetch_jira_ticket],
)

bitbucket_agent = LlmAgent(
    name="BitbucketAgent",
    model="gemini-2.5-flash",
    description="Fetches open PRs related to the affected service",
    instruction="Fetch open pull requests for the service mentioned in the ticket.",
    tools=[fetch_bitbucket_prs],
)

gcp_agent = LlmAgent(
    name="GCPAgent",
    model="gemini-2.5-flash",
    description="Fetches GCP error logs for the affected service",
    instruction="Fetch recent error and warning logs from GCP for the service.",
    tools=[fetch_gcp_logs],
)

outlook_agent = LlmAgent(
    name="OutlookAgent",
    model="gemini-2.5-flash",
    description="Fetches relevant email threads from Outlook",
    instruction="Fetch email threads related to the developer and service.",
    tools=[fetch_outlook_emails],
)

teams_agent = LlmAgent(
    name="TeamsAgent",
    model="gemini-2.5-flash",
    description="Fetches Teams channel messages about the incident",
    instruction="Fetch Teams messages mentioning the developer or service.",
    tools=[fetch_teams_messages],
)

# ── Fan-out agent (runs all platform agents in parallel) ──

platform_fan_out = ParallelAgent(
    name="PlatformFanOut",
    description="Fans out to all platforms simultaneously",
    sub_agents=[
        bitbucket_agent,
        gcp_agent,
        outlook_agent,
        teams_agent,
    ],
)

# ── Root orchestrator agent ──

root_agent = Agent(
    name="DevAgent",
    model="gemini-2.5-flash",
    description="AI-powered developer briefing agent that eliminates context switching",
    instruction="""You are DevAgent. When a developer is mentioned in a Jira ticket:
1. First fetch the Jira ticket details using JiraAgent
2. Fan out to all platforms simultaneously using PlatformFanOut  
3. Synthesise all signals into a structured briefing with:
   - A 2-3 sentence summary of what is happening
   - Root cause hypothesis
   - Confidence level (high/medium/low)
   - 3-5 specific action items with deep links
   - Severity score 1-10
Always be specific. Never say 'investigate the issue' — say exactly what to do.""",
    sub_agents=[jira_agent, platform_fan_out],
)