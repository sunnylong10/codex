from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import requests


@dataclass(frozen=True)
class VerificationResponse:
    status_code: int | None
    json_data: Any = None
    text: str = ""
    error_code: str = ""
    retryable: bool = False


class VerificationClient(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict | None = None,
        json: dict | None = None,
    ) -> VerificationResponse: ...


class RequestsVerificationClient:
    """Bounded HTTP client shared by ATS verification plugins."""

    def __init__(self, timeout: tuple[float, float] = (5, 10)):
        self.timeout = timeout
        self.session = requests.Session()

    def request(self, method, url, *, headers=None, params=None, json=None):
        try:
            response = self.session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json,
                timeout=self.timeout,
                allow_redirects=True,
            )
        except requests.Timeout:
            return VerificationResponse(None, error_code="timeout", retryable=True)
        except requests.RequestException:
            return VerificationResponse(None, error_code="network_error", retryable=True)
        body = None
        try:
            body = response.json()
        except ValueError:
            pass
        retryable = response.status_code == 429 or response.status_code >= 500
        error_code = ""
        if response.status_code == 429:
            error_code = "rate_limited"
        elif response.status_code >= 500:
            error_code = "server_error"
        return VerificationResponse(
            response.status_code,
            json_data=body,
            text=response.text[:10_000],
            error_code=error_code,
            retryable=retryable,
        )
