from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session

from pmcopy.db import RawResponse, json_dumps
from pmcopy.logging import get_logger

LOGGER = get_logger(__name__)


class RateLimiter:
    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval_seconds = min_interval_seconds
        self._last_request_at = 0.0

    def wait(self) -> None:
        if self.min_interval_seconds <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.min_interval_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.monotonic()


def extract_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "results", "items", "markets", "trades", "holders", "users", "leaderboard"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


class PublicAPIClient:
    def __init__(
        self,
        base_url: str,
        source: str,
        api_config: dict[str, Any],
        session: Session | None = None,
        raw_data_dir: Path | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.source = source
        self.session = session
        self.raw_data_dir = raw_data_dir
        self.timeout_seconds = float(api_config.get("timeout_seconds", 20))
        self.max_retries = int(api_config.get("max_retries", 3))
        self.backoff_seconds = float(api_config.get("backoff_seconds", 1.0))
        self.rate_limiter = RateLimiter(float(api_config.get("min_request_interval_seconds", 0.2)))
        self.client = httpx.Client(
            timeout=self.timeout_seconds,
            headers={"User-Agent": str(api_config.get("user_agent", "pmcopy-research public-readonly"))},
            follow_redirects=True,
        )

    def get_json(self, path: str, params: dict[str, Any] | None = None, endpoint: str | None = None) -> Any | None:
        url = self._url(path)
        endpoint_name = endpoint or path.strip("/") or "root"
        params = {key: value for key, value in (params or {}).items() if value is not None}
        last_error: str | None = None

        for attempt in range(1, self.max_retries + 1):
            self.rate_limiter.wait()
            try:
                response = self.client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
                self._persist_response(endpoint_name, url, params, response.status_code, True, None, payload, response.text)
                return payload
            except Exception as exc:  # noqa: BLE001 - API callers need graceful degradation.
                status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                response_text = exc.response.text if isinstance(exc, httpx.HTTPStatusError) else None
                last_error = f"{type(exc).__name__}: {exc}"
                self._persist_response(endpoint_name, url, params, status_code, False, last_error, None, response_text)
                if status_code and 400 <= status_code < 500 and status_code not in {408, 429}:
                    break
                if attempt < self.max_retries:
                    time.sleep(self.backoff_seconds * (2 ** (attempt - 1)))

        LOGGER.warning("%s endpoint failed: %s params=%s error=%s", self.source, endpoint_name, params, last_error)
        return None

    def iter_offset_pages(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        endpoint: str | None = None,
        limit_param: str = "limit",
        offset_param: str = "offset",
        page_size: int = 100,
        max_pages: int = 1,
    ) -> list[Any]:
        collected: list[Any] = []
        for page in range(max_pages):
            page_params = dict(params or {})
            page_params[limit_param] = page_size
            page_params[offset_param] = page * page_size
            payload = self.get_json(path, page_params, endpoint=endpoint)
            items = extract_items(payload)
            if not items:
                break
            collected.extend(items)
            if len(items) < page_size:
                break
        return collected

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def _persist_response(
        self,
        endpoint: str,
        url: str,
        params: dict[str, Any],
        status_code: int | None,
        success: bool,
        error: str | None,
        payload: Any | None,
        response_text: str | None,
    ) -> None:
        if self.session is None:
            return
        raw = RawResponse(
            source=self.source,
            endpoint=endpoint,
            method="GET",
            url=url,
            params_json=json_dumps(params),
            status_code=status_code,
            success=success,
            error=error,
            response_json=json_dumps(payload) if payload is not None else None,
            response_text=response_text[:10000] if response_text else None,
        )
        self.session.add(raw)
        self.session.flush()
