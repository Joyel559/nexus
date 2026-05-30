"""Internal OAuth runtime for Google and GitHub ecosystem onboarding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from auth.credential_store import AuthRepository
from auth.models import AuthBackendType
from config.settings import Settings


class OAuthRuntimeError(RuntimeError):
    """Raised when OAuth flow bootstrap or token exchange fails."""

    def __init__(self, *, status_code: int | None, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True, slots=True)
class OAuthStartResult:
    redirect_url: str
    state: str
    csrf_token: str


class OAuthRuntime:
    """Gateway-owned OAuth session, redirect, and token exchange runtime."""

    def __init__(self, *, settings: Settings, repo: AuthRepository):
        self._settings = settings
        self._repo = repo

    def google_configured(self) -> bool:
        return bool(
            (self._settings.google_oauth_client_id or "").strip()
            and (self._settings.google_oauth_client_secret or "").strip()
        )

    def github_configured(self) -> bool:
        return bool(
            (self._settings.github_oauth_client_id or "").strip()
            and (self._settings.github_oauth_client_secret or "").strip()
        )

    def start_google(
        self,
        *,
        account_key: str,
        label: str,
        backend_key: str,
        redirect_uri: str,
    ) -> OAuthStartResult:
        if not self.google_configured():
            raise OAuthRuntimeError(
                status_code=400,
                message=(
                    "Google OAuth not configured. Set GOOGLE_OAUTH_CLIENT_ID and "
                    "GOOGLE_OAUTH_CLIENT_SECRET."
                ),
            )
        self._repo.upsert_auth_backend(
            provider_id="antigravity",
            backend_type=AuthBackendType.OAUTH,
            backend_key=backend_key,
            label=backend_key,
            metadata={"provider": "google"},
            enabled=True,
        )
        session = self._repo.create_oauth_session(
            provider_id="antigravity",
            backend_key=backend_key,
            redirect_uri=redirect_uri,
            metadata={"account_key": account_key, "label": label},
            ttl_seconds=900,
        )
        scope = (self._settings.google_oauth_scopes or "").strip()
        query = urlencode(
            {
                "client_id": self._settings.google_oauth_client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": scope,
                "state": session["state"],
                "access_type": "offline",
                "include_granted_scopes": "true",
                "prompt": "consent",
            }
        )
        return OAuthStartResult(
            redirect_url=f"https://accounts.google.com/o/oauth2/v2/auth?{query}",
            state=session["state"],
            csrf_token=session["csrf_token"],
        )

    def start_github(
        self,
        *,
        account_key: str,
        label: str,
        backend_key: str,
        redirect_uri: str,
    ) -> OAuthStartResult:
        if not self.github_configured():
            raise OAuthRuntimeError(
                status_code=400,
                message=(
                    "GitHub OAuth not configured. Set GITHUB_OAUTH_CLIENT_ID and "
                    "GITHUB_OAUTH_CLIENT_SECRET."
                ),
            )
        self._repo.upsert_auth_backend(
            provider_id="github_models",
            backend_type=AuthBackendType.OAUTH,
            backend_key=backend_key,
            label=backend_key,
            metadata={"provider": "github"},
            enabled=True,
        )
        session = self._repo.create_oauth_session(
            provider_id="github_models",
            backend_key=backend_key,
            redirect_uri=redirect_uri,
            metadata={"account_key": account_key, "label": label},
            ttl_seconds=900,
        )
        query = urlencode(
            {
                "client_id": self._settings.github_oauth_client_id,
                "redirect_uri": redirect_uri,
                "scope": "read:user user:email",
                "state": session["state"],
                "allow_signup": "true",
            }
        )
        return OAuthStartResult(
            redirect_url=f"https://github.com/login/oauth/authorize?{query}",
            state=session["state"],
            csrf_token=session["csrf_token"],
        )

    async def exchange_google_code(
        self,
        *,
        code: str,
        state: str,
        csrf_token: str,
    ) -> dict[str, Any]:
        session = self._repo.consume_oauth_session(state=state, csrf_token=csrf_token)
        if session is None:
            raise OAuthRuntimeError(
                status_code=400,
                message="OAuth session invalid or expired",
            )
        redirect_uri = str(session.get("redirect_uri") or "")
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": self._settings.google_oauth_client_id,
                    "client_secret": self._settings.google_oauth_client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
                headers={"Accept": "application/json"},
            )
            if token_resp.status_code >= 400:
                raise OAuthRuntimeError(
                    status_code=502,
                    message=_http_error_detail(token_resp, "Google token exchange failed"),
                )
            token_payload = token_resp.json()
            access_token = token_payload.get("access_token")
            if not isinstance(access_token, str) or not access_token.strip():
                raise OAuthRuntimeError(
                    status_code=502,
                    message="Google token exchange missing access_token",
                )
            profile_resp = await client.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if profile_resp.status_code >= 400:
                raise OAuthRuntimeError(
                    status_code=502,
                    message=_http_error_detail(profile_resp, "Google profile fetch failed"),
                )
            profile = profile_resp.json()
        metadata = session.get("metadata", {})
        scopes = _split_scopes(token_payload.get("scope"))
        return {
            "provider_id": "antigravity",
            "backend_key": str(session.get("backend_key") or "google_oauth"),
            "account_key": str(
                metadata.get("account_key")
                or profile.get("email")
                or profile.get("sub")
                or "google-account"
            ),
            "label": str(
                metadata.get("label")
                or profile.get("email")
                or profile.get("name")
                or profile.get("sub")
                or "google-account"
            ),
            "external_account_id": str(profile.get("sub") or profile.get("email") or ""),
            "access_token": str(access_token),
            "refresh_token": (
                str(token_payload.get("refresh_token"))
                if isinstance(token_payload.get("refresh_token"), str)
                else None
            ),
            "expires_in": (
                float(token_payload.get("expires_in"))
                if isinstance(token_payload.get("expires_in"), (int, float))
                else None
            ),
            "scopes": scopes,
            "metadata": {
                "source": "google_oauth",
                "google_email": profile.get("email"),
                "google_name": profile.get("name"),
                "google_picture": profile.get("picture"),
            },
        }

    async def exchange_github_code(
        self,
        *,
        code: str,
        state: str,
        csrf_token: str,
    ) -> dict[str, Any]:
        session = self._repo.consume_oauth_session(state=state, csrf_token=csrf_token)
        if session is None:
            raise OAuthRuntimeError(
                status_code=400,
                message="OAuth session invalid or expired",
            )
        redirect_uri = str(session.get("redirect_uri") or "")
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_resp = await client.post(
                "https://github.com/login/oauth/access_token",
                headers={"Accept": "application/json"},
                data={
                    "client_id": self._settings.github_oauth_client_id,
                    "client_secret": self._settings.github_oauth_client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "state": state,
                },
            )
            if token_resp.status_code >= 400:
                raise OAuthRuntimeError(
                    status_code=502,
                    message=_http_error_detail(token_resp, "GitHub token exchange failed"),
                )
            token_payload = token_resp.json()
            access_token = token_payload.get("access_token")
            if not isinstance(access_token, str) or not access_token.strip():
                raise OAuthRuntimeError(
                    status_code=502,
                    message=_http_error_detail(token_resp, "GitHub token exchange missing access_token"),
                )

            base_headers = {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {access_token}",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            user_resp = await client.get("https://api.github.com/user", headers=base_headers)
            if user_resp.status_code >= 400:
                raise OAuthRuntimeError(
                    status_code=502,
                    message=_http_error_detail(user_resp, "GitHub profile fetch failed"),
                )
            user_payload = user_resp.json()
            email = None
            email_resp = await client.get("https://api.github.com/user/emails", headers=base_headers)
            if email_resp.status_code < 400:
                email = _primary_email_from_payload(email_resp.json())

        metadata = session.get("metadata", {})
        login = str(user_payload.get("login") or "")
        external_id = str(user_payload.get("id") or login)
        account_key = str(metadata.get("account_key") or login or external_id or "github-account")
        label = str(metadata.get("label") or email or login or account_key)
        scopes = _split_scopes(token_payload.get("scope"), delimiter=",")
        return {
            "provider_id": "github_models",
            "backend_key": str(session.get("backend_key") or "github_oauth"),
            "account_key": account_key,
            "label": label,
            "external_account_id": external_id or account_key,
            "access_token": str(access_token),
            "refresh_token": (
                str(token_payload.get("refresh_token"))
                if isinstance(token_payload.get("refresh_token"), str)
                else None
            ),
            "expires_in": (
                float(token_payload.get("expires_in"))
                if isinstance(token_payload.get("expires_in"), (int, float))
                else None
            ),
            "scopes": scopes,
            "metadata": {
                "source": "github_oauth",
                "github_login": login,
                "github_email": email,
                "github_avatar_url": user_payload.get("avatar_url"),
                "github_profile_url": user_payload.get("html_url"),
                "github_student_eligible": _infer_student_eligibility(email=email),
                "github_student_reason": _student_reason(email=email),
            },
        }

    async def refresh_google_token(self, *, refresh_token: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": self._settings.google_oauth_client_id,
                    "client_secret": self._settings.google_oauth_client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                headers={"Accept": "application/json"},
            )
            if response.status_code >= 400:
                raise OAuthRuntimeError(
                    status_code=502,
                    message=_http_error_detail(response, "Google token refresh failed"),
                )
            payload = response.json()
        if not isinstance(payload, dict):
            raise OAuthRuntimeError(
                status_code=502,
                message="Google token refresh returned invalid payload",
            )
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise OAuthRuntimeError(
                status_code=502,
                message="Google token refresh missing access_token",
            )
        return payload

    async def google_token_info(self, *, access_token: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://www.googleapis.com/oauth2/v3/tokeninfo",
                params={"access_token": access_token},
            )
            if response.status_code >= 400:
                raise OAuthRuntimeError(
                    status_code=502,
                    message=_http_error_detail(response, "Google token info failed"),
                )
            payload = response.json()
        if not isinstance(payload, dict):
            raise OAuthRuntimeError(
                status_code=502,
                message="Google token info returned invalid payload",
            )
        return payload


def _split_scopes(value: Any, *, delimiter: str = " ") -> list[str]:
    if not isinstance(value, str):
        return []
    return [part.strip() for part in value.split(delimiter) if part.strip()]


def _primary_email_from_payload(payload: Any) -> str | None:
    if not isinstance(payload, list):
        return None
    primary = next(
        (row for row in payload if isinstance(row, dict) and row.get("primary") is True),
        None,
    )
    candidate = primary or next(
        (row for row in payload if isinstance(row, dict) and isinstance(row.get("email"), str)),
        None,
    )
    if not isinstance(candidate, dict):
        return None
    value = candidate.get("email")
    return value if isinstance(value, str) and value.strip() else None


def _infer_student_eligibility(*, email: str | None) -> bool:
    if not isinstance(email, str):
        return False
    return email.strip().lower().endswith(".edu")


def _student_reason(*, email: str | None) -> str:
    if _infer_student_eligibility(email=email):
        return "email_domain_edu"
    return "unknown"


def _http_error_detail(response: httpx.Response, fallback: str) -> str:
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
        if isinstance(error, str) and error.strip():
            desc = payload.get("error_description")
            if isinstance(desc, str) and desc.strip():
                return f"{error}: {desc}"
            return error
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message
    text = response.text.strip()
    if text:
        return text[:500]
    return f"{fallback} (HTTP {response.status_code})"
