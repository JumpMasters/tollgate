"""TollgateClient: the SDK's HTTP transport to the control plane's command routes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import httpx

from tollgate.adapters.integrations.sdk.config import SdkConfig
from tollgate.adapters.integrations.sdk.errors import EnforcementUnavailable, error_for


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Provider-reported token counts for a commit — never caller-asserted."""

    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ReserveResult:
    reservation_id: str
    estimated_micro: int
    price_book_version: str
    ttl_deadline: datetime


@dataclass(frozen=True, slots=True)
class CommitResult:
    reservation_id: str
    committed_micro: int
    overage_micro: int


@dataclass(frozen=True, slots=True)
class CancelResult:
    reservation_id: str
    released_micro: int


@dataclass(frozen=True, slots=True)
class ExtendResult:
    reservation_id: str
    ttl_deadline: datetime


@dataclass(frozen=True, slots=True)
class MeterResult:
    actual_micro: int
    price_book_version: str


def _usage_body(usage: ProviderUsage) -> dict[str, int]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
    }


class TollgateClient:
    """Async transport for reserve/commit/cancel/extend, with fail-closed error mapping."""

    def __init__(self, config: SdkConfig, *, http: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            base_url=config.base_url,
            timeout=httpx.Timeout(
                connect=config.connect_timeout_seconds,
                read=config.read_timeout_seconds,
                write=config.read_timeout_seconds,
                pool=config.connect_timeout_seconds,
            ),
            headers={"Authorization": f"Bearer {config.token}"},
        )

    async def __aenter__(self) -> TollgateClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _post(
        self, path: str, body: dict[str, Any], *, idempotency_key: str | None
    ) -> dict[str, Any]:
        # Set explicitly per-request (not just relied on via the owned client's default
        # headers) so an injected `http` — which may not carry it — still authenticates.
        headers = {"Authorization": f"Bearer {self._config.token}"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = await self._http.post(path, json=body, headers=headers)
        except httpx.HTTPError as exc:  # connect/read/pool timeout, connection reset, DNS, ...
            raise EnforcementUnavailable(
                "control plane unreachable", status=503, code="enforcement_unavailable"
            ) from exc
        if response.status_code // 100 != 2:
            envelope = _error_envelope(response)
            raise error_for(response.status_code, envelope[0], envelope[1])
        return cast(dict[str, Any], response.json())

    async def reserve(
        self,
        *,
        provider: str,
        model: str,
        input_bound_tokens: int,
        max_output_tokens: int,
        idempotency_key: str,
        cache_creation_bound_tokens: int = 0,
        project: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> ReserveResult:
        body: dict[str, Any] = {
            "provider": provider,
            "model": model,
            "input_bound_tokens": input_bound_tokens,
            "max_output_tokens": max_output_tokens,
            "cache_creation_bound_tokens": cache_creation_bound_tokens,
            "labels": labels or {},
        }
        if project is not None:
            body["project_id"] = project
        data = await self._post("/v1/reserve", body, idempotency_key=idempotency_key)
        return ReserveResult(
            reservation_id=str(data["reservation_id"]),
            estimated_micro=int(data["estimated_micro"]),
            price_book_version=str(data["price_book_version"]),
            ttl_deadline=datetime.fromisoformat(str(data["ttl_deadline"])),
        )

    async def commit(
        self, *, reservation_id: str, usage: ProviderUsage, idempotency_key: str
    ) -> CommitResult:
        body = {
            "reservation_id": reservation_id,
            "usage": _usage_body(usage),
        }
        data = await self._post("/v1/commit", body, idempotency_key=idempotency_key)
        return CommitResult(
            reservation_id=str(data["reservation_id"]),
            committed_micro=int(data["committed_micro"]),
            overage_micro=int(data["overage_micro"]),
        )

    async def cancel(self, *, reservation_id: str, idempotency_key: str) -> CancelResult:
        data = await self._post(
            "/v1/cancel", {"reservation_id": reservation_id}, idempotency_key=idempotency_key
        )
        return CancelResult(
            reservation_id=str(data["reservation_id"]), released_micro=int(data["released_micro"])
        )

    async def extend(self, *, reservation_id: str) -> ExtendResult:
        data = await self._post(
            "/v1/extend", {"reservation_id": reservation_id}, idempotency_key=None
        )
        return ExtendResult(
            reservation_id=str(data["reservation_id"]),
            ttl_deadline=datetime.fromisoformat(str(data["ttl_deadline"])),
        )

    async def meter(
        self,
        *,
        provider: str,
        model: str,
        usage: ProviderUsage,
        idempotency_key: str,
        labels: dict[str, str] | None = None,
        project: str | None = None,
        truncated: bool = False,
    ) -> MeterResult:
        body: dict[str, Any] = {
            "provider": provider,
            "model": model,
            "usage": _usage_body(usage),
            "labels": labels or {},
            "truncated": truncated,
        }
        if project is not None:
            body["project_id"] = project
        data = await self._post("/v1/meter", body, idempotency_key=idempotency_key)
        return MeterResult(
            actual_micro=int(data["actual_micro"]),
            price_book_version=str(data["price_book_version"]),
        )


def _error_envelope(response: httpx.Response) -> tuple[str | None, str]:
    """Best-effort (code, message) from an ADR-0031 error body; robust to a non-JSON body."""
    try:
        payload = response.json()
        error = payload["error"]
        return (error.get("code"), error.get("message") or response.reason_phrase)
    except (ValueError, KeyError, TypeError, AttributeError):
        return (None, response.reason_phrase or f"HTTP {response.status_code}")
