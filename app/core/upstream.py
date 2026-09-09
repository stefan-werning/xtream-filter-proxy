from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("proxy.upstream")


class UpstreamError(Exception):
    pass


class UpstreamClient:
    def __init__(self, config: dict):
        u = config["upstream"]
        self.base_url = u["base_url"].rstrip("/")
        self.username = u["username"]
        self.password = u["password"]
        self.timeout = u.get("timeout_seconds", 20)
        self.user_agent = u.get("user_agent", "IPTVSmartersPro")

    def _headers(self) -> dict:
        return {"User-Agent": self.user_agent}

    async def player_api(self, params: dict) -> dict | list | None:
        query = {"username": self.username, "password": self.password, **params}
        url = f"{self.base_url}/player_api.php"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, params=query, headers=self._headers())
                resp.raise_for_status()
                return resp.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("upstream player_api failed: %s", e)
            raise UpstreamError(str(e)) from e

    def build_redirect_url(self, kind_path: str, rest: str) -> str:
        return f"{self.base_url}/{kind_path}/{self.username}/{self.password}/{rest}"

    def build_url(self, path: str, params: dict) -> str:
        query = {"username": self.username, "password": self.password, **params}
        req = httpx.Request("GET", f"{self.base_url}/{path}", params=query)
        return str(req.url)
