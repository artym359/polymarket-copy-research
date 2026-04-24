from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from pmcopy.api.rate_limit import PublicAPIClient
from pmcopy.config import raw_data_dir


class ClobClient(PublicAPIClient):
    def __init__(self, config: dict[str, Any], session: Session | None = None) -> None:
        super().__init__(
            config["api"]["clob_base_url"],
            "clob",
            config.get("api", {}),
            session=session,
            raw_data_dir=raw_data_dir(config),
        )

    def get_orderbook(self, token_id: str) -> dict[str, Any] | None:
        payload = self.get_json("/book", {"token_id": token_id}, endpoint="book")
        return payload if isinstance(payload, dict) else None

    def get_midpoint(self, token_id: str) -> dict[str, Any] | None:
        payload = self.get_json("/midpoint", {"token_id": token_id}, endpoint="midpoint")
        return payload if isinstance(payload, dict) else None

    def get_last_trade_price(self, token_id: str) -> dict[str, Any] | None:
        payload = self.get_json("/last-trade-price", {"token_id": token_id}, endpoint="last-trade-price")
        return payload if isinstance(payload, dict) else None

    def get_price(self, token_id: str, side: str) -> dict[str, Any] | None:
        payload = self.get_json("/price", {"token_id": token_id, "side": side.upper()}, endpoint="price")
        return payload if isinstance(payload, dict) else None

    def get_spread(self, token_id: str) -> dict[str, Any] | None:
        payload = self.get_json("/spread", {"token_id": token_id}, endpoint="spread")
        return payload if isinstance(payload, dict) else None

    def price_history_url(self, token_id: str, interval: str = "max", fidelity: int | str = 1) -> str:
        # CLOB names the asset-id filter "market" for this endpoint.
        return f"{self.base_url}/prices-history?market={token_id}&interval={interval}&fidelity={fidelity}"

    def get_price_history_payload(self, token_id: str, interval: str = "max", fidelity: int | str = 1) -> Any:
        # CLOB names the asset-id filter "market" for this endpoint.
        return self.get_json(
            "/prices-history",
            {"market": token_id, "interval": interval, "fidelity": fidelity},
            endpoint="prices-history",
        )

    def get_price_history(self, token_id: str, interval: str = "max", fidelity: int | str = 1) -> list[dict[str, Any]]:
        payload = self.get_price_history_payload(token_id, interval=interval, fidelity=fidelity)
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("history", "prices", "data", "results", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []
