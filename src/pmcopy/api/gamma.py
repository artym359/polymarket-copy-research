from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from pmcopy.api.rate_limit import PublicAPIClient
from pmcopy.config import raw_data_dir


class GammaClient(PublicAPIClient):
    def __init__(self, config: dict[str, Any], session: Session | None = None) -> None:
        super().__init__(
            config["api"]["gamma_base_url"],
            "gamma",
            config.get("api", {}),
            session=session,
            raw_data_dir=raw_data_dir(config),
        )

    def get_markets(
        self,
        *,
        category: str | None,
        active: bool | None,
        closed: bool | None,
        min_volume: float | None,
        min_liquidity: float | None,
        sort_by: str,
        limit: int,
        max_pages: int,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "active": str(active).lower() if active is not None else None,
            "closed": str(closed).lower() if closed is not None else None,
            "archived": "false",
            "order": sort_by,
            "ascending": "false",
        }
        if category:
            params["category"] = category
        if min_volume is not None:
            params["volume_num_min"] = min_volume
        if min_liquidity is not None:
            params["liquidity_num_min"] = min_liquidity
        return [
            item for item in self.iter_offset_pages(
                "/markets",
                params=params,
                endpoint="markets",
                page_size=min(limit, 500),
                max_pages=max_pages,
            )
            if isinstance(item, dict)
        ][:limit]

    def search_profiles(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        payload = self.get_json("/search", {"q": query, "limit": limit}, endpoint="search")
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            items = payload.get("profiles") or payload.get("users") or payload.get("results") or []
            return [item for item in items if isinstance(item, dict)]
        return []
