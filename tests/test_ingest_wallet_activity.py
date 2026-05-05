from __future__ import annotations

from pmcopy.api.data_api import DataAPIClient
from pmcopy.db import Wallet, get_engine, session_scope
from pmcopy.ingest.ingest_wallet_activity import ingest_wallet


def test_ingest_wallet_handles_missing_endpoint_data(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "test.sqlite3"
    config = {
        "app": {"database_url": f"sqlite:///{db_path}", "raw_data_dir": str(tmp_path / "raw")},
        "api": {
            "data_base_url": "https://data-api.polymarket.com",
            "timeout_seconds": 1,
            "max_retries": 1,
            "backoff_seconds": 0,
            "min_request_interval_seconds": 0,
        },
        "wallet_ingestion": {
            "trades": {"enabled": True, "page_size": 10, "max_pages": 1},
            "activity": {"enabled": True, "page_size": 10, "max_pages": 1},
            "positions": {"enabled": True, "page_size": 10, "max_pages": 1},
            "closed_positions": {"enabled": True, "page_size": 10, "max_pages": 1},
            "value": {"enabled": True},
        },
    }

    monkeypatch.setattr(DataAPIClient, "get_wallet_trades", lambda self, wallet, page_size, max_pages: [])
    monkeypatch.setattr(DataAPIClient, "get_wallet_activity", lambda self, wallet, page_size, max_pages: [])
    monkeypatch.setattr(DataAPIClient, "get_wallet_positions", lambda self, wallet, page_size, max_pages: [])
    monkeypatch.setattr(DataAPIClient, "get_wallet_closed_positions", lambda self, wallet, page_size, max_pages: [])
    monkeypatch.setattr(DataAPIClient, "get_wallet_value", lambda self, wallet: [])

    result = ingest_wallet(config, "0x0000000000000000000000000000000000000001")

    assert result.trades == 0
    assert len(result.warnings) == 5
    with session_scope(config["app"]["database_url"]) as session:
        assert session.get(Wallet, "0x0000000000000000000000000000000000000001") is not None
