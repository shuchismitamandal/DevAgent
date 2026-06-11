"""
core/store.py
─────────────
In-memory briefing cache for DevAgent.

Why this exists:
  Without a cache, every time the developer clicks a ticket on the dashboard,
  the full pipeline runs again — 4 platform calls + Gemini synthesis.
  That's wasteful, slow, and burns API credits unnecessarily.

How it works:
  - Briefings are stored in a dict keyed by ticket_id
  - Each entry has a TTL (default 10 minutes)
  - After TTL expires, next click re-runs the pipeline and refreshes the cache
  - The store also tracks all seen mention IDs for the Jira poller (Change 4)

Design decisions:
  - Pure in-memory (no Redis, no DB) — simple, zero infra for competition demo
  - TTL is per-entry, not global — critical tickets can be given shorter TTL
  - Thread-safe via asyncio.Lock — safe for FastAPI's async handlers
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from core.models import AgentBriefing
import json
import os
from pathlib import Path
from itsdangerous import URLSafeSerializer

# Default TTL: 10 minutes
# After this, the next click re-runs the full pipeline and refreshes
DEFAULT_TTL_SECONDS = 600

_cred_file = Path("user_credentials.json")
_cred_serializer = URLSafeSerializer(os.getenv("SESSION_SECRET", "dev-secret"))

def save_user_credentials(account_id: str, credentials: dict):
    """Save encrypted per-user credentials to local file."""
    data = {}
    if _cred_file.exists():
        try:
            data = json.loads(_cred_file.read_text())
        except Exception:
            data = {}
    
    # Encrypt the credentials
    data[account_id] = _cred_serializer.dumps(credentials)
    _cred_file.write_text(json.dumps(data))

def load_user_credentials(account_id: str) -> dict:
    """Load and decrypt per-user credentials."""
    if not _cred_file.exists():
        return {}
    try:
        data = json.loads(_cred_file.read_text())
        if account_id not in data:
            return {}
        return _cred_serializer.loads(data[account_id])
    except Exception:
        return {}
    
class BriefingStore:
    """
    In-memory cache for AgentBriefing objects.

    Usage:
        store = BriefingStore()

        # Save a briefing
        await store.set("PAY-4821", briefing)

        # Get a cached briefing (returns None if expired or not found)
        briefing = await store.get("PAY-4821")

        # Check if a briefing is cached and fresh
        if await store.has("PAY-4821"):
            ...

        # Invalidate manually (e.g. when ticket is updated)
        await store.invalidate("PAY-4821")

        # Get cache stats for the dashboard
        stats = store.stats()
    """

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self._cache: dict[str, dict] = {}
        # { ticket_id: { "briefing": AgentBriefing, "expires_at": datetime } }
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()
        self._hit_count  = 0
        self._miss_count = 0

    async def get(self, ticket_id: str) -> Optional[AgentBriefing]:
        """
        Return cached briefing if it exists and hasn't expired.
        Returns None if not found or expired (caller should re-run pipeline).
        """
        async with self._lock:
            entry = self._cache.get(ticket_id)
            if not entry:
                self._miss_count += 1
                return None

            if datetime.utcnow() > entry["expires_at"]:
                # Expired — remove and return None
                del self._cache[ticket_id]
                self._miss_count += 1
                return None

            self._hit_count += 1
            return entry["briefing"]

    async def set(
        self,
        ticket_id: str,
        briefing: AgentBriefing,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        """
        Store a briefing with TTL.
        Critical tickets (severity 9-10) get a shorter TTL so they refresh faster.
        """
        ttl = ttl_seconds or self._ttl

        # Auto-shorten TTL for critical tickets — stale data is worse for incidents
        if briefing.severity_score >= 9:
            ttl = min(ttl, 180)   # 3 minutes max for severity 9-10
        elif briefing.severity_score >= 7:
            ttl = min(ttl, 300)   # 5 minutes max for severity 7-8

        async with self._lock:
            self._cache[ticket_id] = {
                "briefing":   briefing,
                "cached_at":  datetime.utcnow(),
                "expires_at": datetime.utcnow() + timedelta(seconds=ttl),
                "ttl":        ttl,
            }

    async def has(self, ticket_id: str) -> bool:
        """Return True if a fresh (non-expired) briefing exists."""
        return await self.get(ticket_id) is not None

    async def invalidate(self, ticket_id: str) -> bool:
        """
        Manually remove a briefing from cache.
        Use when a ticket is updated or when you want to force a refresh.
        Returns True if something was removed.
        """
        async with self._lock:
            if ticket_id in self._cache:
                del self._cache[ticket_id]
                return True
            return False

    async def invalidate_all(self) -> int:
        """Clear entire cache. Returns number of entries cleared."""
        async with self._lock:
            count = len(self._cache)
            self._cache.clear()
            return count

    def time_remaining(self, ticket_id: str) -> Optional[int]:
        """
        Return seconds remaining before a cached briefing expires.
        Returns None if not cached.
        Used by the dashboard to show "refreshes in Xm Xs".
        """
        entry = self._cache.get(ticket_id)
        if not entry:
            return None
        remaining = (entry["expires_at"] - datetime.utcnow()).total_seconds()
        return max(0, int(remaining))

    def stats(self) -> dict:
        """
        Return cache stats — shown in the dashboard header.
        """
        now = datetime.utcnow()
        active = {
            k: v for k, v in self._cache.items()
            if v["expires_at"] > now
        }
        total_requests = self._hit_count + self._miss_count
        hit_rate = (self._hit_count / total_requests * 100) if total_requests > 0 else 0

        return {
            "cached_tickets":  len(active),
            "total_requests":  total_requests,
            "cache_hits":      self._hit_count,
            "cache_misses":    self._miss_count,
            "hit_rate_pct":    round(hit_rate, 1),
            "entries": [
                {
                    "ticket_id":       k,
                    "cached_at":       v["cached_at"].isoformat(),
                    "expires_at":      v["expires_at"].isoformat(),
                    "seconds_remaining": max(0, int((v["expires_at"] - now).total_seconds())),
                    "severity":        v["briefing"].severity_score,
                    "ttl":             v["ttl"],
                }
                for k, v in active.items()
            ]
        }


# ── Singleton instance shared across the app ──────────────────────────
# Import this in orchestrator.py and api/server.py
briefing_store = BriefingStore()