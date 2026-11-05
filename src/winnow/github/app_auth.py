"""GitHub App authentication: JWT → installation token (API.md §7).

Tokens live in memory only and expire after 1 hour; the cache refreshes at
minute 50 so a token never dies mid-run.
"""

from __future__ import annotations

import time

import httpx
import structlog

log = structlog.get_logger()

TOKEN_REFRESH_S = 50 * 60


def build_jwt(app_id: str, private_key_pem: str, now: int | None = None) -> str:
    """Short-lived JWT (10 min) for App-level calls. Pure function, testable."""
    import jwt

    moment = now if now is not None else int(time.time())
    return jwt.encode(
        {"iat": moment - 60, "exp": moment + 600, "iss": app_id},
        private_key_pem,
        algorithm="RS256",
    )


class TokenCache:
    """One installation token per installation id, refreshed at minute 50."""
    def __init__(self, app_id: str, private_key_pem: str) -> None:
        self._app_id = app_id
        self._key = private_key_pem
        self._tokens: dict[int, tuple[str, float]] = {}

    async def token(self, installation_id: int) -> str:
        cached = self._tokens.get(installation_id)
        if cached is not None and time.monotonic() - cached[1] < TOKEN_REFRESH_S:
            return cached[0]
        fresh = await self._mint(installation_id)
        self._tokens[installation_id] = (fresh, time.monotonic())
        return fresh

    async def _mint(self, installation_id: int) -> str:
        async with httpx.AsyncClient(timeout=30) as http:
            response = await http.post(
                f"https://api.github.com/app/installations/{installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {build_jwt(self._app_id, self._key)}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            response.raise_for_status()
            token = response.json()["token"]
            log.info("installation_token_minted", installation_id=installation_id)
            return token


async def get_installation_id(app_id: str, private_key_pem: str, repo: str) -> int:
    """Resolve the installation id for a repo (repo webhooks omit it).

    App webhooks carry installation.id; repository webhooks do not, so the
    runner looks it up with App credentials (verified 28 Oct, G4).
    """
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.get(
            f"https://api.github.com/repos/{repo}/installation",
            headers={
                "Authorization": f"Bearer {build_jwt(app_id, private_key_pem)}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response.raise_for_status()
        return int(response.json()["id"])
