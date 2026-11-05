"""GitHub REST client over httpx (API.md §7). All calls carry an installation
token; 5xx and secondary rate limits retry with backoff, honouring Retry-After.
Only plain code calls this module (deterministic publisher, ADR-010).
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

API_ROOT = "https://api.github.com"
APIVERSION_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


class GitHubClient:
    def __init__(self, token: str) -> None:
        self._token = token
        self._http: httpx.AsyncClient | None = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=API_ROOT,
                headers={**APIVERSION_HEADERS, "Authorization": f"Bearer {self._token}"},
                timeout=60.0,
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        http = await self._client()
        last: Exception | None = None
        for attempt in range(4):
            try:
                response = await http.request(method, path, **kwargs)
            except httpx.TimeoutException as exc:
                last = exc
                await asyncio.sleep(2**attempt + random.uniform(0, 0.5))  # noqa: S311
                continue
            if response.status_code in (500, 502, 503, 504):
                last = RuntimeError(f"GitHub {response.status_code} on {method} {path}")
                await asyncio.sleep(2**attempt + random.uniform(0, 0.5))  # noqa: S311
                continue
            if response.status_code == 403 and "rate limit" in response.text.lower():
                wait = _retry_after(response)
                log.warning("github_rate_limit", wait_s=wait, path=path)
                await asyncio.sleep(wait)
                last = RuntimeError(f"GitHub rate limit on {method} {path}")
                continue
            if response.status_code >= 400:
                raise RuntimeError(
                    f"GitHub {response.status_code} on {method} {path}: {response.text[:300]}"
                )
            if response.status_code == 204 or not response.content:
                return None
            content_type = response.headers.get("content-type", "")
            return response.json() if "json" in content_type else response.text
        raise RuntimeError(f"GitHub request failed after retries: {last}")

    async def get_pr(self, repo: str, number: int) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{repo}/pulls/{number}")

    async def get_pr_diff(self, repo: str, number: int) -> str:
        http = await self._client()
        response = await http.get(
            f"/repos/{repo}/pulls/{number}",
            headers={"Accept": "application/vnd.github.diff"},
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"GitHub {response.status_code} fetching diff: {response.text[:200]}"
            )
        return response.text

    async def get_pr_files(self, repo: str, number: int) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = await self._request(
                "GET",
                f"/repos/{repo}/pulls/{number}/files",
                params={"per_page": 100, "page": page},
            )
            files.extend(batch)
            if len(batch) < 100:
                return files
            page += 1

    async def get_tarball(self, repo: str, ref: str) -> bytes:
        http = await self._client()
        response = await http.get(f"/repos/{repo}/tarball/{ref}", follow_redirects=True)
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub {response.status_code} fetching tarball")
        return response.content

    async def get_contents(self, repo: str, path: str, ref: str) -> str | None:
        """Decoded file content, or None when absent (config is optional)."""
        try:
            data = await self._request("GET", f"/repos/{repo}/contents/{path}", params={"ref": ref})
        except RuntimeError as exc:
            if "404" in str(exc):
                return None
            raise
        import base64

        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")

    async def post_review(
        self, repo: str, number: int, commit_id: str, comments: list[dict[str, Any]], body: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/repos/{repo}/pulls/{number}/reviews",
            json={"commit_id": commit_id, "event": "COMMENT", "body": body, "comments": comments},
        )

    async def post_issue_comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body}
        )

    async def patch_issue_comment(self, repo: str, comment_id: int, body: str) -> None:
        await self._request(
            "PATCH", f"/repos/{repo}/issues/comments/{comment_id}", json={"body": body}
        )

    async def create_check_run(self, repo: str, head_sha: str) -> int:
        data = await self._request(
            "POST",
            f"/repos/{repo}/check-runs",
            json={
                "name": "Winnow review",
                "head_sha": head_sha,
                "status": "in_progress",
                "output": {"title": "Winnow review", "summary": "Review running…"},
            },
        )
        return int(data["id"])

    async def complete_check_run(
        self, repo: str, check_run_id: int, summary: str, details_url: str | None = None
    ) -> None:
        payload: dict[str, Any] = {
            "status": "completed",
            "conclusion": "neutral",
            "output": {"title": "Winnow review", "summary": summary[:60000]},
        }
        if details_url:
            payload["details_url"] = details_url
        await self._request("PATCH", f"/repos/{repo}/check-runs/{check_run_id}", json=payload)

    async def add_reaction(self, repo: str, comment_id: int, emoji: str) -> None:
        await self._request(
            "POST",
            f"/repos/{repo}/issues/comments/{comment_id}/reactions",
            json={"content": emoji},
        )

    async def get_review_comments(
        self, repo: str, pr_number: int, review_id: int
    ) -> list[dict[str, Any]]:
        return await self._request(
            "GET", f"/repos/{repo}/pulls/{pr_number}/reviews/{review_id}/comments"
        )

    async def get_review_comment_reactions(
        self, repo: str, comment_id: int
    ) -> list[dict[str, Any]]:
        return await self._request("GET", f"/repos/{repo}/pulls/comments/{comment_id}/reactions")

    # --- Git Data API: atomic multi-file commit for /winnow docs apply ---

    async def get_ref(self, repo: str, branch: str) -> str:
        data = await self._request("GET", f"/repos/{repo}/git/ref/heads/{branch}")
        return data["object"]["sha"]

    async def create_blob(self, repo: str, content: str) -> str:
        data = await self._request(
            "POST", f"/repos/{repo}/git/blobs", json={"content": content, "encoding": "utf-8"}
        )
        return data["sha"]

    async def create_tree(self, repo: str, base_tree: str, entries: list[dict[str, str]]) -> str:
        data = await self._request(
            "POST", f"/repos/{repo}/git/trees", json={"base_tree": base_tree, "tree": entries}
        )
        return data["sha"]

    async def get_commit_tree(self, repo: str, commit_sha: str) -> str:
        data = await self._request("GET", f"/repos/{repo}/git/commits/{commit_sha}")
        return data["tree"]["sha"]

    async def create_commit(
        self, repo: str, message: str, tree_sha: str, parents: list[str]
    ) -> str:
        data = await self._request(
            "POST",
            f"/repos/{repo}/git/commits",
            json={"message": message, "tree": tree_sha, "parents": parents},
        )
        return data["sha"]

    async def update_ref(self, repo: str, branch: str, sha: str) -> None:
        await self._request("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", json={"sha": sha})


def _retry_after(response: httpx.Response) -> float:
    try:
        return max(1.0, float(response.headers.get("Retry-After", "30")))
    except (TypeError, ValueError):
        return 30.0
