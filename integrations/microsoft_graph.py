from core.models import EmailThread, TeamsMessage


class MicrosoftGraphClient:

    async def fetch_emails(self, keyword: str) -> list[EmailThread]:
        return []

    async def fetch_teams_messages(
        self,
        developer: str,
        service: str
    ) -> list[TeamsMessage]:
        return []

    async def post_teams_briefing(
        self,
        channel_id: str,
        team_id: str,
        message: str
    ) -> bool:
        return False

    async def send_email(
        self,
        to: str,
        subject: str,
        body: str
    ) -> bool:
        return False