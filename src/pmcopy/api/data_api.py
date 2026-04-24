from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from pmcopy.api.rate_limit import PublicAPIClient, extract_items
from pmcopy.config import raw_data_dir


class DataAPIClient(PublicAPIClient):
    def __init__(self, config: dict[str, Any], session: Session | None = None) -> None:
        super().__init__(
            config["api"]["data_base_url"],
            "data_api",
            config.get("api", {}),
            session=session,
            raw_data_dir=raw_data_dir(config),
        )
        self._leaderboard_paths_checked = False
        self._working_leaderboard_path: str | None = None
        self._holders_checked = False
        self._working_holder_variant: tuple[str, str] | None = None
        self._activity_checked = False
        self._working_activity_variant: tuple[str, str] | None = None

    def get_leaderboard(self, sort: str, page_size: int, max_pages: int) -> list[dict[str, Any]]:
        if self._leaderboard_paths_checked and self._working_leaderboard_path is None:
            return []
        candidates: list[dict[str, Any]] = []
        paths = [self._working_leaderboard_path] if self._working_leaderboard_path else [
            "/leaderboard",
            "/leaderboards",
            "/rankings",
        ]
        endpoint_variants = [(path, {"sort": sort}) for path in paths if path]
        for path, params in endpoint_variants:
            items = self.iter_offset_pages(
                path,
                params=params,
                endpoint=f"leaderboard:{sort}:{path}",
                page_size=page_size,
                max_pages=max_pages,
            )
            typed_items = [item for item in items if isinstance(item, dict)]
            if typed_items:
                self._working_leaderboard_path = path
                candidates.extend(typed_items)
                break
        self._leaderboard_paths_checked = True
        return candidates

    def get_market_holders(self, market_id: str | None, token_id: str | None, limit: int) -> list[dict[str, Any]]:
        if self._holders_checked and self._working_holder_variant is None:
            return []

        endpoint_variants: list[tuple[str, dict[str, Any]]] = []
        if self._working_holder_variant:
            path, key = self._working_holder_variant
            value = token_id if "token" in key else market_id
            if value:
                endpoint_variants.append((path, {key: value}))
        else:
            if market_id:
                endpoint_variants.append(("/holders", {"market": market_id}))
                endpoint_variants.append(("/holders", {"market_id": market_id}))
            if token_id:
                endpoint_variants.append(("/holders", {"token": token_id}))
                endpoint_variants.append(("/holders", {"token_id": token_id}))

        for path, params in endpoint_variants:
            payload = self.get_json(path, {**params, "limit": limit}, endpoint=f"holders:{path}")
            items = [item for item in extract_items(payload) if isinstance(item, dict)]
            if items:
                key = next(iter(params.keys()))
                self._working_holder_variant = (path, key)
                self._holders_checked = True
                return items[:limit]
        self._holders_checked = True
        return []

    def get_market_activity(self, market_id: str | None, token_id: str | None, limit: int) -> list[dict[str, Any]]:
        if self._activity_checked and self._working_activity_variant is None:
            return []

        endpoint_variants: list[tuple[str, dict[str, Any]]] = []
        if self._working_activity_variant:
            path, key = self._working_activity_variant
            value = token_id if "token" in key else market_id
            if value:
                endpoint_variants.append((path, {key: value}))
        else:
            if market_id:
                endpoint_variants.append(("/activity", {"market": market_id}))
                endpoint_variants.append(("/activity", {"market_id": market_id}))
                endpoint_variants.append(("/trades", {"market": market_id}))
                endpoint_variants.append(("/trades", {"market_id": market_id}))
            if token_id:
                endpoint_variants.append(("/activity", {"token": token_id}))
                endpoint_variants.append(("/activity", {"token_id": token_id}))
                endpoint_variants.append(("/trades", {"token": token_id}))
                endpoint_variants.append(("/trades", {"token_id": token_id}))

        for path, params in endpoint_variants:
            payload = self.get_json(path, {**params, "limit": limit}, endpoint=f"market_activity:{path}")
            items = [item for item in extract_items(payload) if isinstance(item, dict)]
            if items:
                key = next(iter(params.keys()))
                self._working_activity_variant = (path, key)
                self._activity_checked = True
                return items[:limit]
        self._activity_checked = True
        return []

    def get_wallet_trades(self, wallet_address: str, page_size: int, max_pages: int) -> list[dict[str, Any]]:
        return [
            item for item in self.iter_offset_pages(
                "/trades",
                params={"user": wallet_address},
                endpoint="wallet_trades:/trades",
                page_size=page_size,
                max_pages=max_pages,
            )
            if isinstance(item, dict)
        ]

    def get_wallet_activity(self, wallet_address: str, page_size: int, max_pages: int) -> list[dict[str, Any]]:
        return [
            item for item in self.iter_offset_pages(
                "/activity",
                params={"user": wallet_address},
                endpoint="wallet_activity:/activity",
                page_size=page_size,
                max_pages=max_pages,
            )
            if isinstance(item, dict)
        ]

    def get_wallet_positions(self, wallet_address: str, page_size: int, max_pages: int) -> list[dict[str, Any]]:
        return [
            item for item in self.iter_offset_pages(
                "/positions",
                params={"user": wallet_address},
                endpoint="wallet_positions:/positions",
                page_size=page_size,
                max_pages=max_pages,
            )
            if isinstance(item, dict)
        ]

    def get_wallet_closed_positions(self, wallet_address: str, page_size: int, max_pages: int) -> list[dict[str, Any]]:
        return [
            item for item in self.iter_offset_pages(
                "/closed-positions",
                params={"user": wallet_address},
                endpoint="wallet_closed_positions:/closed-positions",
                page_size=page_size,
                max_pages=max_pages,
            )
            if isinstance(item, dict)
        ]

    def get_wallet_value(self, wallet_address: str) -> list[dict[str, Any]]:
        payload = self.get_json("/value", {"user": wallet_address}, endpoint="wallet_value:/value")
        return [item for item in extract_items(payload) if isinstance(item, dict)]
