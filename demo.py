"""
demo.py
────────
Run this to see DevAgent in action from the command line.
No server needed — just:  python demo.py

For competition demo, run:
  python demo.py --ticket PAY-4821 --dev "Arjun Mehta"
  python demo.py --ticket AUTH-1092 --dev "Arjun Mehta"
"""
import asyncio, argparse, sys, os
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from agents.orchestrator import run_devagent
from rich.console import Console

console = Console()

async def main(ticket_id: str, developer: str):
    briefing = await run_devagent(ticket_id, developer)
    console.print(f"\n[dim]Full briefing object available — {len(briefing.action_items)} action items generated[/]")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DevAgent CLI Demo")
    parser.add_argument("--ticket", default="PAY-4821",    help="Jira ticket ID")
    parser.add_argument("--dev",    default="Arjun Mehta", help="Developer name")
    args = parser.parse_args()
    asyncio.run(main(args.ticket, args.dev))
