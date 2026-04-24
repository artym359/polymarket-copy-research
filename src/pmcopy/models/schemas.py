from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class DiscoveryWarning(BaseModel):
    source: str
    message: str
    detail: str | None = None


class CandidateRecord(BaseModel):
    wallet_address: str
    username: str | None = None
    sources: set[str] = Field(default_factory=set)
    categories: set[str] = Field(default_factory=set)
    first_source: str | None = None
    last_seen_at: datetime | None = None
    raw_refs: dict[str, list[Any]] = Field(default_factory=dict)
    high_volume_market_count: int = 0
    has_public_profile: bool = False

    def add_source(self, source: str) -> None:
        self.sources.add(source)
        if not self.first_source:
            self.first_source = source


class DiscoveryResult(BaseModel):
    candidates_found: int
    markets_scanned: int
    tokens_upserted: int
    warnings: list[DiscoveryWarning] = Field(default_factory=list)
