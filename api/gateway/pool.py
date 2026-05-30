"""Provider/account pool management with quota and cooldown handling."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass
from threading import RLock
from typing import Any

from api.gateway.crypto import CredentialCipher
from api.gateway.db import GatewayDatabase
from api.gateway.models import AccountType, ProviderAccount, ProviderState

_DAY_FMT = "%Y-%m-%d"


@dataclass(frozen=True, slots=True)
class AccountSelectionHints:
    sticky_key: str | None = None
    prefer_low_latency: bool = False
    prefer_high_quota: bool = False


@dataclass(frozen=True, slots=True)
class ProviderRuntimeScore:
    health_score: float
    latency_score: float
    quota_headroom: float


class ProviderPoolManager:
    """Manages provider/account records and runtime selection views."""

    def __init__(self, db: GatewayDatabase, *, cipher: CredentialCipher):
        self._db = db
        self._cipher = cipher
        self._lock = RLock()
        self._rr_index_by_provider: dict[str, int] = defaultdict(int)

    @property
    def db(self) -> GatewayDatabase:
        return self._db

    def list_providers(self) -> tuple[ProviderState, ...]:
        rows = self._db.fetchall(
            "SELECT provider_id, enabled, priority FROM providers ORDER BY priority ASC, provider_id ASC"
        )
        return tuple(
            ProviderState(
                provider_id=str(row["provider_id"]),
                enabled=bool(row["enabled"]),
                priority=int(row["priority"]),
            )
            for row in rows
        )

    def set_provider_enabled(self, provider_id: str, enabled: bool) -> None:
        now = time.time()
        self._db.execute(
            "UPDATE providers SET enabled = ?, updated_at = ? WHERE provider_id = ?",
            (1 if enabled else 0, now, provider_id),
        )

    def is_provider_enabled(self, provider_id: str) -> bool:
        row = self._db.fetchone(
            "SELECT enabled FROM providers WHERE provider_id = ?",
            (provider_id,),
        )
        if row is None:
            return True
        return bool(row["enabled"])

    def upsert_provider_priority(self, provider_id: str, priority: int) -> None:
        now = time.time()
        self._db.execute(
            "UPDATE providers SET priority = ?, updated_at = ? WHERE provider_id = ?",
            (priority, now, provider_id),
        )

    def add_or_update_account(
        self,
        *,
        provider_id: str,
        account_key: str,
        label: str,
        account_type: AccountType,
        credential: str,
        metadata: dict[str, Any] | None,
        max_requests_per_day: int | None,
        max_tokens_per_day: int | None,
        enabled: bool,
    ) -> int:
        now = time.time()
        existing = self._db.fetchone(
            """
            SELECT account_id, credential, credential_version
            FROM provider_accounts
            WHERE provider_id = ? AND account_key = ?
            """,
            (provider_id, account_key),
        )
        current_version = 1
        if existing is not None:
            current_version = int(existing["credential_version"]) + 1
        encrypted = self._cipher.encrypt(credential)
        metadata_json = json.dumps(
            metadata or {}, separators=(",", ":"), sort_keys=True
        )
        self._db.execute(
            """
            INSERT INTO provider_accounts(
                provider_id, account_key, label, account_type, credential, metadata_json,
                enabled, max_requests_per_day, max_tokens_per_day, credential_version,
                created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider_id, account_key) DO UPDATE SET
                label = excluded.label,
                account_type = excluded.account_type,
                credential = excluded.credential,
                metadata_json = excluded.metadata_json,
                enabled = excluded.enabled,
                max_requests_per_day = excluded.max_requests_per_day,
                max_tokens_per_day = excluded.max_tokens_per_day,
                credential_version = excluded.credential_version,
                updated_at = excluded.updated_at
            """,
            (
                provider_id,
                account_key,
                label,
                account_type.value,
                encrypted,
                metadata_json,
                1 if enabled else 0,
                max_requests_per_day,
                max_tokens_per_day,
                current_version,
                now,
                now,
            ),
        )
        row = self._db.fetchone(
            "SELECT account_id FROM provider_accounts WHERE provider_id = ? AND account_key = ?",
            (provider_id, account_key),
        )
        assert row is not None
        account_id = int(row["account_id"])
        self._db.execute(
            """
            INSERT INTO provider_account_credential_versions(
                account_id, version, credential, source, created_at
            ) VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(account_id, version) DO NOTHING
            """,
            (account_id, current_version, encrypted, "upsert", now),
        )
        return account_id

    def delete_account(self, account_id: int) -> None:
        self._db.execute(
            "DELETE FROM provider_accounts WHERE account_id = ?", (account_id,)
        )

    def credential_for_account(self, account_id: int) -> str | None:
        row = self._db.fetchone(
            "SELECT credential FROM provider_accounts WHERE account_id = ?",
            (account_id,),
        )
        if row is None:
            return None
        return self._cipher.decrypt(str(row["credential"]))

    def set_account_enabled(self, account_id: int, enabled: bool) -> None:
        self._db.execute(
            "UPDATE provider_accounts SET enabled = ?, updated_at = ? WHERE account_id = ?",
            (1 if enabled else 0, time.time(), account_id),
        )

    def list_accounts(
        self, provider_id: str | None = None
    ) -> tuple[ProviderAccount, ...]:
        sql = "SELECT * FROM provider_accounts ORDER BY provider_id ASC, enabled DESC, account_id ASC"
        params: tuple[Any, ...] = ()
        if provider_id is not None:
            sql = (
                "SELECT * FROM provider_accounts WHERE provider_id = ? "
                "ORDER BY enabled DESC, account_id ASC"
            )
            params = (provider_id,)
        rows = self._db.fetchall(sql, params)
        today = time.strftime(_DAY_FMT, time.gmtime())
        accounts: list[ProviderAccount] = []
        for row in rows:
            used_requests = int(row["used_requests_today"])
            used_tokens = int(row["used_tokens_today"])
            if str(row["last_reset_day"] or "") != today:
                used_requests = 0
                used_tokens = 0
            accounts.append(
                ProviderAccount(
                    account_id=int(row["account_id"]),
                    provider_id=str(row["provider_id"]),
                    label=str(row["label"]),
                    account_type=AccountType(str(row["account_type"])),
                    credential=self._cipher.decrypt(str(row["credential"])),
                    credential_version=int(row["credential_version"]),
                    metadata=GatewayDatabase.row_json(row, "metadata_json"),
                    enabled=bool(row["enabled"]),
                    max_requests_per_day=(
                        int(row["max_requests_per_day"])
                        if row["max_requests_per_day"] is not None
                        else None
                    ),
                    max_tokens_per_day=(
                        int(row["max_tokens_per_day"])
                        if row["max_tokens_per_day"] is not None
                        else None
                    ),
                    used_requests_today=used_requests,
                    used_tokens_today=used_tokens,
                    cooldown_until=(
                        float(row["cooldown_until"])
                        if row["cooldown_until"] is not None
                        else None
                    ),
                    backoff_level=int(row["backoff_level"]),
                    health_score=float(row["health_score"]),
                    last_latency_ms=(
                        float(row["last_latency_ms"])
                        if row["last_latency_ms"] is not None
                        else None
                    ),
                )
            )
        return tuple(accounts)

    def next_account_round_robin(
        self,
        provider_id: str,
        *,
        now_ts: float | None = None,
    ) -> ProviderAccount | None:
        now_ts = time.time() if now_ts is None else now_ts
        eligible = [
            account
            for account in self.list_accounts(provider_id)
            if self._is_account_eligible(account, now_ts)
        ]
        if not eligible:
            return None
        with self._lock:
            index = self._rr_index_by_provider[provider_id] % len(eligible)
            self._rr_index_by_provider[provider_id] += 1
        return eligible[index]

    def account_by_sticky_hash(
        self, provider_id: str, sticky_key: str
    ) -> ProviderAccount | None:
        eligible = [
            account
            for account in self.list_accounts(provider_id)
            if self._is_account_eligible(account, time.time())
        ]
        if not eligible:
            return None
        index = abs(hash(sticky_key)) % len(eligible)
        return eligible[index]

    def best_latency_account(self, provider_id: str) -> ProviderAccount | None:
        eligible = [
            account
            for account in self.list_accounts(provider_id)
            if self._is_account_eligible(account, time.time())
        ]
        if not eligible:
            return None
        return min(
            eligible,
            key=lambda account: (
                account.last_latency_ms
                if account.last_latency_ms is not None
                else 1e12,
                -account.health_score,
                account.account_id,
            ),
        )

    def best_quota_account(self, provider_id: str) -> ProviderAccount | None:
        eligible = [
            account
            for account in self.list_accounts(provider_id)
            if self._is_account_eligible(account, time.time())
        ]
        if not eligible:
            return None

        def quota_score(account: ProviderAccount) -> tuple[float, float]:
            req_left = 1.0
            if account.max_requests_per_day and account.max_requests_per_day > 0:
                req_left = max(
                    0.0,
                    (account.max_requests_per_day - account.used_requests_today)
                    / account.max_requests_per_day,
                )
            tok_left = 1.0
            if account.max_tokens_per_day and account.max_tokens_per_day > 0:
                tok_left = max(
                    0.0,
                    (account.max_tokens_per_day - account.used_tokens_today)
                    / account.max_tokens_per_day,
                )
            return req_left, tok_left

        return max(
            eligible, key=lambda account: (quota_score(account), account.health_score)
        )

    def mark_success(
        self, account_id: int, *, latency_ms: float, output_tokens: int = 0
    ) -> None:
        now = time.time()
        today = time.strftime(_DAY_FMT, time.gmtime(now))
        row = self._db.fetchone(
            "SELECT used_requests_today, used_tokens_today, last_reset_day, health_score "
            "FROM provider_accounts WHERE account_id = ?",
            (account_id,),
        )
        if row is None:
            return
        used_requests = int(row["used_requests_today"])
        used_tokens = int(row["used_tokens_today"])
        if str(row["last_reset_day"] or "") != today:
            used_requests = 0
            used_tokens = 0
        health = min(1.0, float(row["health_score"]) + 0.03)
        self._db.execute(
            """
            UPDATE provider_accounts
            SET used_requests_today = ?,
                used_tokens_today = ?,
                last_reset_day = ?,
                health_score = ?,
                last_error = NULL,
                last_latency_ms = ?,
                last_success_at = ?,
                updated_at = ?
            WHERE account_id = ?
            """,
            (
                used_requests + 1,
                used_tokens + max(0, output_tokens),
                today,
                health,
                latency_ms,
                now,
                now,
                account_id,
            ),
        )

    def mark_failure(
        self,
        account_id: int,
        *,
        error_type: str,
        is_rate_limit: bool,
    ) -> None:
        now = time.time()
        row = self._db.fetchone(
            "SELECT backoff_level, health_score, provider_id FROM provider_accounts WHERE account_id = ?",
            (account_id,),
        )
        if row is None:
            return
        level = int(row["backoff_level"])
        health = max(0.0, float(row["health_score"]) - 0.15)
        cooldown_until: float | None = None
        if is_rate_limit:
            level += 1
            cooldown_until = now + min(1800.0, 15.0 * (2 ** (level - 1)))
            self._db.execute(
                """
                INSERT INTO cooldowns(provider_id, account_id, reason, started_at, until_ts, backoff_level, active)
                VALUES(?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    str(row["provider_id"]),
                    account_id,
                    error_type,
                    now,
                    cooldown_until,
                    level,
                ),
            )

        self._db.execute(
            """
            UPDATE provider_accounts
            SET backoff_level = ?,
                cooldown_until = ?,
                health_score = ?,
                last_error = ?,
                last_failure_at = ?,
                updated_at = ?
            WHERE account_id = ?
            """,
            (level, cooldown_until, health, error_type, now, now, account_id),
        )

    def _is_account_eligible(self, account: ProviderAccount, now_ts: float) -> bool:
        if not account.enabled:
            return False
        if account.health_score <= 0.05:
            return False
        if account.cooldown_until is not None and account.cooldown_until > now_ts:
            return False
        if (
            account.max_requests_per_day is not None
            and account.used_requests_today >= account.max_requests_per_day
        ):
            return False
        return not (
            account.max_tokens_per_day is not None
            and account.used_tokens_today >= account.max_tokens_per_day
        )

    def provider_runtime_score(self, provider_id: str) -> ProviderRuntimeScore:
        accounts = self.list_accounts(provider_id)
        if not accounts:
            return ProviderRuntimeScore(
                health_score=0.5, latency_score=0.5, quota_headroom=0.5
            )
        eligible = [account for account in accounts if self._is_account_eligible(account, time.time())]
        cohort = eligible if eligible else list(accounts)
        health = sum(account.health_score for account in cohort) / max(1, len(cohort))
        latencies = [
            account.last_latency_ms
            for account in cohort
            if account.last_latency_ms is not None and account.last_latency_ms >= 0.0
        ]
        avg_latency = sum(latencies) / len(latencies) if latencies else 1500.0
        # Normalize latency to ~0..1 where lower is better.
        latency_score = min(1.0, max(0.0, avg_latency / 4000.0))

        quota_parts: list[float] = []
        for account in cohort:
            request_ratio = 1.0
            if account.max_requests_per_day and account.max_requests_per_day > 0:
                request_ratio = max(
                    0.0,
                    (account.max_requests_per_day - account.used_requests_today)
                    / account.max_requests_per_day,
                )
            token_ratio = 1.0
            if account.max_tokens_per_day and account.max_tokens_per_day > 0:
                token_ratio = max(
                    0.0,
                    (account.max_tokens_per_day - account.used_tokens_today)
                    / account.max_tokens_per_day,
                )
            quota_parts.append((request_ratio + token_ratio) / 2.0)
        quota_headroom = (
            sum(quota_parts) / len(quota_parts) if quota_parts else 0.5
        )
        return ProviderRuntimeScore(
            health_score=min(1.0, max(0.0, health)),
            latency_score=latency_score,
            quota_headroom=min(1.0, max(0.0, quota_headroom)),
        )
