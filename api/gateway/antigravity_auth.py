"""Thin integration client for opencode-antigravity-auth OAuth orchestration."""

from __future__ import annotations

from typing import Any

import httpx


class AntigravityAuthError(RuntimeError):
    """Raised when the upstream auth backend request fails."""

    def __init__(self, *, status_code: int | None, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class AntigravityAuthClient:
    """Delegates OAuth and token exchange to the external antigravity auth backend."""

    def __init__(self, base_url: str, *, timeout_s: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    async def health(self) -> dict[str, Any]:
        return await self._request_json("GET", "/health")

    async def start_google_oauth(self, *, account_key: str) -> dict[str, Any]:
        return await self._request_json(
            "GET",
            "/oauth/google/start",
            params={"account_key": account_key},
        )

    async def exchange_google_code(
        self,
        *,
        account_key: str,
        code: str,
        state: str | None,
        redirect_uri: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "account_key": account_key,
            "code": code,
            "state": state,
        }
        if redirect_uri:
            payload["redirect_uri"] = redirect_uri
        return await self._request_json(
            "POST",
            "/oauth/google/exchange",
            json=payload,
        )

    async def refresh_google_token(
        self,
        *,
        account_key: str,
        refresh_token: str,
    ) -> dict[str, Any]:
        payload = await self._request_json(
            "POST",
            "/oauth/google/refresh",
            json={
                "account_key": account_key,
                "refresh_token": refresh_token,
            },
        )
        return payload

    async def google_quota(
        self,
        *,
        account_key: str,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"account_key": account_key}
        if access_token:
            params["access_token"] = access_token
        return await self._request_json(
            "GET",
            "/oauth/google/quota",
            params=params,
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            try:
                response = await client.request(
                    method=method,
                    url=f"{self._base_url}{path}",
                    params=params,
                    json=json,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = _http_error_detail(exc.response)
                raise AntigravityAuthError(
                    status_code=exc.response.status_code,
                    message=detail,
                ) from exc
            except httpx.HTTPError as exc:
                raise AntigravityAuthError(
                    status_code=None,
                    message=f"Network error contacting antigravity auth backend: {type(exc).__name__}",
                ) from exc

        payload = response.json()
        if not isinstance(payload, dict):
            raise AntigravityAuthError(
                status_code=response.status_code,
                message="Antigravity auth backend returned a non-object payload",
            )
        return payload


def _http_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message
    text = response.text.strip()
    if text:
        return text[:500]
    return f"Antigravity auth backend returned HTTP {response.status_code}"
