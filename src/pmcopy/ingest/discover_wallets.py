from __future__ import annotations

import copy
import math
import re
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from pmcopy.api.data_api import DataAPIClient
from pmcopy.api.gamma import GammaClient
from pmcopy.config import database_url, enabled_categories, project_root
from pmcopy.db import (
    CandidateWallet,
    Market,
    Token,
    Wallet,
    init_db,
    json_dumps,
    json_loads,
    promote_candidate,
    session_scope,
)
from pmcopy.logging import get_logger
from pmcopy.models.schemas import CandidateRecord, DiscoveryResult, DiscoveryWarning

LOGGER = get_logger(__name__)
WALLET_RE = re.compile(r"(?<![A-Fa-f0-9])0x[a-fA-F0-9]{40}(?![A-Fa-f0-9])")

WALLET_KEYS = (
    "wallet_address",
    "walletAddress",
    "proxyWallet",
    "proxy_wallet",
    "address",
    "userAddress",
    "user_address",
    "funder",
    "maker",
    "taker",
    "trader",
    "owner",
)
USERNAME_KEYS = ("username", "name", "displayName", "display_name", "pseudonym")
TIMESTAMP_KEYS = ("timestamp", "lastSeenAt", "last_seen_at", "lastActiveAt", "updatedAt", "createdAt", "date")


def apply_discovery_overrides(config: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = copy.deepcopy(config)
    if not overrides:
        return cfg

    discovery = cfg.setdefault("wallet_discovery", {})
    markets = discovery.setdefault("markets", {})
    for key in ("max_wallets_total", "include_manual_seeds", "include_leaderboards", "include_market_holders", "include_market_activity"):
        if key in overrides and overrides[key] is not None:
            discovery[key] = overrides[key]
    if "categories" in overrides and overrides["categories"]:
        discovery["categories"] = {name: name in set(overrides["categories"]) for name in discovery.get("categories", {})}
    for key in ("max_markets_per_category", "min_volume", "min_liquidity", "include_active", "include_closed"):
        if key in overrides and overrides[key] is not None:
            markets[key] = overrides[key]
    return cfg


def discover_wallets(config: dict[str, Any], overrides: dict[str, Any] | None = None) -> DiscoveryResult:
    cfg = apply_discovery_overrides(config, overrides)
    init_db(cfg)
    warnings: list[DiscoveryWarning] = []

    with session_scope(database_url(cfg)) as session:
        gamma = GammaClient(cfg, session=session)
        data_api = DataAPIClient(cfg, session=session)
        candidates: dict[str, CandidateRecord] = {}

        discovery_cfg = cfg.get("wallet_discovery", {})
        if discovery_cfg.get("include_manual_seeds", True):
            _discover_manual_seed_wallets(candidates, warnings)

        if discovery_cfg.get("include_leaderboards", True) and discovery_cfg.get("leaderboards", {}).get("enabled", True):
            _discover_leaderboard_wallets(data_api, discovery_cfg, candidates, warnings)

        markets_scanned, tokens_upserted = _discover_markets_and_market_wallets(
            session,
            gamma,
            data_api,
            cfg,
            candidates,
            warnings,
        )

        selected = sorted(candidates.values(), key=score_candidate, reverse=True)
        max_wallets = int(discovery_cfg.get("max_wallets_total", 5000))
        for record in selected[:max_wallets]:
            upsert_candidate(session, record)

        return DiscoveryResult(
            candidates_found=len(selected[:max_wallets]),
            markets_scanned=markets_scanned,
            tokens_upserted=tokens_upserted,
            warnings=warnings,
        )


def _discover_manual_seed_wallets(
    candidates: dict[str, CandidateRecord],
    warnings: list[DiscoveryWarning],
) -> None:
    path = project_root() / "config" / "wallets_seed.txt"
    if not path.exists():
        warnings.append(DiscoveryWarning(source="manual_seed", message="Seed wallet file was not found.", detail=str(path)))
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        wallet = normalize_wallet(cleaned)
        if not wallet:
            warnings.append(DiscoveryWarning(source="manual_seed", message="Ignored invalid seed wallet line.", detail=cleaned))
            continue
        record = candidates.setdefault(wallet, CandidateRecord(wallet_address=wallet))
        record.add_source("manual_seed")
        record.raw_refs.setdefault("manual_seed", []).append({"line": cleaned})


def _discover_leaderboard_wallets(
    data_api: DataAPIClient,
    discovery_cfg: dict[str, Any],
    candidates: dict[str, CandidateRecord],
    warnings: list[DiscoveryWarning],
) -> None:
    leaderboard_cfg = discovery_cfg.get("leaderboards", {})
    found = 0
    for sort in leaderboard_cfg.get("sorts", ["pnl"]):
        rows = data_api.get_leaderboard(
            sort=str(sort),
            page_size=int(leaderboard_cfg.get("page_size", 100)),
            max_pages=int(leaderboard_cfg.get("max_pages", 20)),
        )
        for row in rows:
            wallet = extract_wallet(row)
            if not wallet:
                continue
            record = candidates.setdefault(wallet, CandidateRecord(wallet_address=wallet))
            record.add_source(f"leaderboard:{sort}")
            record.username = record.username or extract_username(row)
            record.last_seen_at = max_datetime(record.last_seen_at, extract_timestamp(row))
            record.has_public_profile = record.has_public_profile or bool(record.username)
            append_raw_ref(record, f"leaderboard:{sort}", compact_ref(row))
            found += 1
    if found == 0:
        warnings.append(
            DiscoveryWarning(
                source="leaderboards",
                message="No leaderboard wallets were discovered.",
                detail="Data API leaderboard endpoints may be unavailable or have a different schema.",
            )
        )


def _discover_markets_and_market_wallets(
    session: Session,
    gamma: GammaClient,
    data_api: DataAPIClient,
    config: dict[str, Any],
    candidates: dict[str, CandidateRecord],
    warnings: list[DiscoveryWarning],
) -> tuple[int, int]:
    discovery_cfg = config.get("wallet_discovery", {})
    market_cfg = discovery_cfg.get("markets", {})
    categories = enabled_categories(config)
    if not categories:
        warnings.append(DiscoveryWarning(source="gamma", message="No categories are enabled for market discovery."))
        return 0, 0

    max_markets = int(market_cfg.get("max_markets_per_category", 300))
    page_size = min(max_markets, 500) if max_markets > 0 else 100
    max_pages = max(1, math.ceil(max_markets / page_size))
    markets_scanned = 0
    tokens_upserted = 0
    holder_found = 0
    activity_found = 0

    for category in categories:
        markets = _fetch_category_markets(gamma, category, market_cfg, max_markets, max_pages)
        if not markets:
            warnings.append(
                DiscoveryWarning(
                    source="gamma",
                    message="No markets discovered for category.",
                    detail=category,
                )
            )
            continue

        for raw_market in markets:
            market_id, token_ids, token_count = upsert_market_and_tokens(session, raw_market, category)
            if not market_id:
                warnings.append(
                    DiscoveryWarning(
                        source="gamma",
                        message="Skipped market with no usable market_id.",
                        detail=str(compact_ref(raw_market)),
                    )
                )
                continue
            markets_scanned += 1
            tokens_upserted += token_count
            volume = first_float(raw_market, ("volumeNum", "volume_num", "volume", "volumeClob"))
            high_volume = volume is not None and volume >= float(market_cfg.get("min_volume", 0) or 0)

            token_id = token_ids[0] if token_ids else None
            data_market_id = string_or_none(raw_market.get("conditionId") or raw_market.get("condition_id")) or market_id
            if discovery_cfg.get("include_market_holders", True) and discovery_cfg.get("holders", {}).get("enabled", True):
                holders = data_api.get_market_holders(
                    market_id=data_market_id,
                    token_id=token_id,
                    limit=int(discovery_cfg.get("holders", {}).get("max_holders_per_market", 200)),
                )
                holder_found += len(holders)
                for holder in holders:
                    add_candidate_from_record(
                        candidates,
                        holder,
                        source="market_holder",
                        category=category,
                        raw_ref={"market_id": market_id, "token_id": token_id, **compact_ref(holder)},
                        high_volume_market=high_volume,
                    )

            if discovery_cfg.get("include_market_activity", True) and discovery_cfg.get("activity", {}).get("enabled", True):
                activities = data_api.get_market_activity(
                    market_id=data_market_id,
                    token_id=token_id,
                    limit=int(discovery_cfg.get("activity", {}).get("max_activity_per_market", 500)),
                )
                activity_found += len(activities)
                for activity in activities:
                    add_candidate_from_record(
                        candidates,
                        activity,
                        source="market_activity",
                        category=category,
                        raw_ref={"market_id": market_id, "token_id": token_id, **compact_ref(activity)},
                        high_volume_market=high_volume,
                    )

    if discovery_cfg.get("include_market_holders", True) and holder_found == 0:
        warnings.append(
            DiscoveryWarning(
                source="holders",
                message="No holder wallets were discovered.",
                detail="Holder endpoint may be unavailable, disabled by schema change, or selected markets had no holder data.",
            )
        )
    if discovery_cfg.get("include_market_activity", True) and activity_found == 0:
        warnings.append(
            DiscoveryWarning(
                source="activity",
                message="No activity/trade wallets were discovered.",
                detail="Activity/trades endpoints may be unavailable, disabled by schema change, or selected markets had no activity data.",
            )
        )
    return markets_scanned, tokens_upserted


def _fetch_category_markets(
    gamma: GammaClient,
    category: str,
    market_cfg: dict[str, Any],
    max_markets: int,
    max_pages: int,
) -> list[dict[str, Any]]:
    markets: OrderedDict[str, dict[str, Any]] = OrderedDict()
    queries: list[tuple[bool | None, bool | None]] = []
    if market_cfg.get("include_active", True):
        queries.append((True, False))
    if market_cfg.get("include_closed", True):
        queries.append((None, True))
    if not queries:
        return []

    for active, closed in queries:
        for market in gamma.get_markets(
            category=category,
            active=active,
            closed=closed,
            min_volume=market_cfg.get("min_volume"),
            min_liquidity=market_cfg.get("min_liquidity"),
            sort_by=str(market_cfg.get("sort_by", "volume")),
            limit=max_markets,
            max_pages=max_pages,
        ):
            market_id = extract_market_id(market)
            if market_id and market_id not in markets:
                markets[market_id] = market
            if len(markets) >= max_markets:
                break
    return list(markets.values())[:max_markets]


def upsert_market_and_tokens(session: Session, raw_market: dict[str, Any], category: str) -> tuple[str | None, list[str], int]:
    market_id = extract_market_id(raw_market)
    if not market_id:
        return None, [], 0
    market = session.get(Market, market_id)
    parsed_tags = parse_jsonish(raw_market.get("tags"), default=[])
    event = extract_event(raw_market)
    if market is None:
        market = Market(market_id=market_id)
        session.add(market)
    market.condition_id = string_or_none(raw_market.get("conditionId") or raw_market.get("condition_id"))
    market.question = string_or_none(raw_market.get("question") or raw_market.get("title"))
    market.slug = string_or_none(raw_market.get("slug"))
    market.category = category
    market.tags_json = json_dumps(parsed_tags)
    market.event_id = string_or_none(event.get("id"))
    market.event_slug = string_or_none(event.get("slug"))
    market.active = bool_or_none(raw_market.get("active"))
    market.closed = bool_or_none(raw_market.get("closed"))
    market.archived = bool_or_none(raw_market.get("archived"))
    market.enable_order_book = bool_or_none(raw_market.get("enableOrderBook") or raw_market.get("enable_order_book"))
    market.start_date = string_or_none(raw_market.get("startDate") or raw_market.get("start_date"))
    market.end_date = string_or_none(raw_market.get("endDate") or raw_market.get("end_date"))
    market.resolution_status = string_or_none(raw_market.get("resolutionStatus") or raw_market.get("resolution_status"))
    market.volume = first_float(raw_market, ("volumeNum", "volume_num", "volume", "volumeClob"))
    market.liquidity = first_float(raw_market, ("liquidityNum", "liquidity_num", "liquidity"))
    market.raw_json = json_dumps(raw_market)

    token_ids: list[str] = []
    token_count = 0
    for parsed_token in parse_market_tokens(raw_market):
        token_id = parsed_token.get("token_id")
        if not token_id:
            continue
        token_ids.append(token_id)
        token = session.get(Token, token_id)
        if token is None:
            token = Token(token_id=token_id, market_id=market_id)
            session.add(token)
            token_count += 1
        token.market_id = market_id
        token.outcome_name = parsed_token.get("outcome_name")
        token.outcome_index = parsed_token.get("outcome_index")
        token.yes_no_side = parsed_token.get("yes_no_side")
        token.raw_json = json_dumps(parsed_token.get("raw", {}))
    return market_id, token_ids, token_count


def add_candidate_from_record(
    candidates: dict[str, CandidateRecord],
    row: dict[str, Any],
    *,
    source: str,
    category: str | None,
    raw_ref: dict[str, Any],
    high_volume_market: bool,
) -> None:
    wallet = extract_wallet(row)
    if not wallet:
        return
    record = candidates.setdefault(wallet, CandidateRecord(wallet_address=wallet))
    record.add_source(source)
    if category:
        record.categories.add(category)
    record.username = record.username or extract_username(row)
    record.last_seen_at = max_datetime(record.last_seen_at, extract_timestamp(row))
    record.has_public_profile = record.has_public_profile or bool(record.username)
    if high_volume_market:
        record.high_volume_market_count += 1
    append_raw_ref(record, source, raw_ref)


def upsert_candidate(session: Session, record: CandidateRecord) -> None:
    existing = session.get(CandidateWallet, record.wallet_address)
    if existing is None:
        existing = CandidateWallet(
            wallet_address=record.wallet_address,
            discovered_at=datetime.now(timezone.utc),
        )
        session.add(existing)

    existing_sources = set(json_loads(existing.sources_json, []))
    existing_categories = set(json_loads(existing.categories_json, []))
    existing_refs = json_loads(existing.raw_refs_json, {}) or {}

    merged_sources = sorted(existing_sources | record.sources)
    merged_categories = sorted(existing_categories | record.categories)
    for source, refs in record.raw_refs.items():
        existing_refs.setdefault(source, [])
        existing_refs[source].extend(refs)
        existing_refs[source] = existing_refs[source][-50:]

    existing.username = existing.username or record.username
    existing.sources_json = json_dumps(merged_sources)
    existing.source_count = len(merged_sources)
    existing.first_source = existing.first_source or record.first_source
    existing.last_seen_at = max_datetime(existing.last_seen_at, record.last_seen_at)
    existing.categories_json = json_dumps(merged_categories)
    existing.raw_refs_json = json_dumps(existing_refs)
    existing.discovery_score = score_candidate(record, merged_sources, merged_categories)


def score_candidate(
    record: CandidateRecord,
    sources_override: list[str] | None = None,
    categories_override: list[str] | None = None,
) -> float:
    sources = set(sources_override or record.sources)
    categories = set(categories_override or record.categories)
    score = 0.0
    if any(source.startswith("leaderboard") for source in sources):
        score += 3.0
    if "manual_seed" in sources:
        score += 0.5
    if len(sources) > 1:
        score += (len(sources) - 1) * 1.5
    if record.last_seen_at and record.last_seen_at >= datetime.now(timezone.utc) - timedelta(days=30):
        score += 1.0
    score += min(record.high_volume_market_count, 5) * 0.5
    if len(categories) > 1:
        score += (len(categories) - 1) * 0.75
    if record.has_public_profile:
        score += 1.0
    return round(score, 4)


def list_candidates(session: Session, limit: int = 200, promoted: bool | None = None) -> pd.DataFrame:
    stmt = select(CandidateWallet).order_by(CandidateWallet.discovery_score.desc().nullslast(), CandidateWallet.source_count.desc())
    if promoted is not None:
        stmt = stmt.where(CandidateWallet.promoted.is_(promoted))
    if limit:
        stmt = stmt.limit(limit)

    rows: list[dict[str, Any]] = []
    for candidate in session.scalars(stmt):
        rows.append(
            {
                "wallet_address": candidate.wallet_address,
                "username": candidate.username,
                "sources": ", ".join(json_loads(candidate.sources_json, [])),
                "source_count": candidate.source_count,
                "categories": ", ".join(json_loads(candidate.categories_json, [])),
                "discovery_score": candidate.discovery_score,
                "first_source": candidate.first_source,
                "last_seen_at": candidate.last_seen_at.isoformat() if candidate.last_seen_at else None,
                "promoted": candidate.promoted,
            }
        )
    return pd.DataFrame(rows)


def promote_candidates_by_address(config: dict[str, Any], wallet_addresses: list[str]) -> int:
    with session_scope(database_url(config)) as session:
        promoted = 0
        for wallet_address in wallet_addresses:
            candidate = session.get(CandidateWallet, wallet_address)
            if candidate is None:
                candidate = session.get(CandidateWallet, wallet_address.lower())
            if candidate is None:
                continue
            promote_candidate(session, candidate)
            promoted += 1
        return promoted


def list_promoted_wallets(session: Session, limit: int = 200) -> pd.DataFrame:
    stmt = select(Wallet).order_by(Wallet.last_seen_at.desc().nullslast(), Wallet.wallet_address).limit(limit)
    return pd.DataFrame(
        [
            {
                "wallet_address": wallet.wallet_address,
                "username": wallet.username,
                "source": wallet.source,
                "first_seen_at": wallet.first_seen_at.isoformat() if wallet.first_seen_at else None,
                "last_seen_at": wallet.last_seen_at.isoformat() if wallet.last_seen_at else None,
                "notes": wallet.notes,
            }
            for wallet in session.scalars(stmt)
        ]
    )


def normalize_wallet(value: Any) -> str | None:
    if value is None:
        return None
    match = WALLET_RE.search(str(value))
    return match.group(0).lower() if match else None


def extract_wallet(row: dict[str, Any]) -> str | None:
    for key in WALLET_KEYS:
        wallet = normalize_wallet(row.get(key))
        if wallet:
            return wallet
    for nested_key in ("user", "profile", "account", "trader"):
        nested = row.get(nested_key)
        if isinstance(nested, dict):
            wallet = extract_wallet(nested)
            if wallet:
                return wallet
    return None


def extract_username(row: dict[str, Any]) -> str | None:
    for key in USERNAME_KEYS:
        value = row.get(key)
        if value:
            return str(value)
    for nested_key in ("user", "profile", "account", "trader"):
        nested = row.get(nested_key)
        if isinstance(nested, dict):
            username = extract_username(nested)
            if username:
                return username
    return None


def extract_timestamp(row: dict[str, Any]) -> datetime | None:
    for key in TIMESTAMP_KEYS:
        parsed = parse_timestamp(row.get(key))
        if parsed:
            return parsed
    return None


def parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def max_datetime(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return right
    if right is None:
        return left
    if left.tzinfo is None:
        left = left.replace(tzinfo=timezone.utc)
    if right.tzinfo is None:
        right = right.replace(tzinfo=timezone.utc)
    return max(left, right)


def append_raw_ref(record: CandidateRecord, source: str, raw_ref: dict[str, Any]) -> None:
    record.raw_refs.setdefault(source, [])
    record.raw_refs[source].append(raw_ref)
    record.raw_refs[source] = record.raw_refs[source][-50:]


def compact_ref(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "wallet",
        "walletAddress",
        "proxyWallet",
        "address",
        "username",
        "name",
        "timestamp",
        "createdAt",
        "updatedAt",
        "market",
        "market_id",
        "conditionId",
        "title",
        "question",
        "volume",
        "volumeNum",
        "liquidity",
        "liquidityNum",
    )
    return {key: row.get(key) for key in keys if key in row}


def extract_market_id(raw_market: dict[str, Any]) -> str | None:
    for key in ("id", "market_id", "marketId", "conditionId", "condition_id", "slug"):
        value = raw_market.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def extract_event(raw_market: dict[str, Any]) -> dict[str, Any]:
    event = raw_market.get("event")
    if isinstance(event, dict):
        return event
    events = raw_market.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        return events[0]
    return {
        "id": raw_market.get("eventId") or raw_market.get("event_id"),
        "slug": raw_market.get("eventSlug") or raw_market.get("event_slug"),
    }


def parse_market_tokens(raw_market: dict[str, Any]) -> list[dict[str, Any]]:
    token_rows = raw_market.get("tokens")
    parsed: list[dict[str, Any]] = []
    if isinstance(token_rows, list):
        for index, token in enumerate(token_rows):
            if not isinstance(token, dict):
                continue
            token_id = string_or_none(
                token.get("token_id")
                or token.get("tokenId")
                or token.get("id")
                or token.get("asset_id")
                or token.get("assetId")
            )
            outcome_name = string_or_none(token.get("outcome") or token.get("name"))
            parsed.append(
                {
                    "token_id": token_id,
                    "outcome_name": outcome_name,
                    "outcome_index": int_or_none(token.get("outcome_index") or token.get("outcomeIndex") or index),
                    "yes_no_side": yes_no_side(outcome_name),
                    "raw": token,
                }
            )
    if parsed:
        return parsed

    clob_ids = parse_jsonish(raw_market.get("clobTokenIds") or raw_market.get("clob_token_ids"), default=[])
    outcomes = parse_jsonish(raw_market.get("outcomes"), default=[])
    if not isinstance(clob_ids, list):
        return []
    for index, token_id in enumerate(clob_ids):
        outcome_name = None
        if isinstance(outcomes, list) and index < len(outcomes):
            outcome_name = string_or_none(outcomes[index])
        parsed.append(
            {
                "token_id": string_or_none(token_id),
                "outcome_name": outcome_name,
                "outcome_index": index,
                "yes_no_side": yes_no_side(outcome_name),
                "raw": {"token_id": token_id, "outcome_name": outcome_name},
            }
        )
    return parsed


def parse_jsonish(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        parsed = json_loads(value, None)
        if parsed is not None:
            return parsed
    return default


def first_float(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "1", "yes"}
    return bool(value)


def int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def string_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def yes_no_side(outcome_name: str | None) -> str | None:
    if not outcome_name:
        return None
    normalized = outcome_name.strip().lower()
    if normalized in {"yes", "y"}:
        return "yes"
    if normalized in {"no", "n"}:
        return "no"
    return None


def seed_wallet_file_path() -> Path:
    return project_root() / "config" / "wallets_seed.txt"
