from __future__ import annotations

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from pmcopy.db import (
    Activity,
    Base,
    CandidateWallet,
    Market,
    RawResponse,
    Token,
    Trade,
    Wallet,
    WalletClassification,
    WalletMetrics,
    cleanup_database,
)


def count(session: Session, model) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_cleanup_promoted_keeps_candidates_and_resets_flags() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(CandidateWallet(wallet_address="0xabc", promoted=True))
        session.add(Wallet(wallet_address="0xabc"))
        session.add(Trade(id="t1", wallet_address="0xabc"))
        session.add(Activity(id="a1", wallet_address="0xabc"))
        session.add(WalletMetrics(wallet_address="0xabc"))
        session.add(WalletClassification(wallet_address="0xabc", class_label="unknown"))
        session.commit()

        counts = cleanup_database(session, "promoted")
        session.commit()

        candidate = session.get(CandidateWallet, "0xabc")
        assert candidate is not None
        assert candidate.promoted is False
        assert count(session, Wallet) == 0
        assert count(session, Trade) == 0
        assert count(session, Activity) == 0
        assert count(session, WalletMetrics) == 0
        assert count(session, WalletClassification) == 0
        assert counts["candidate_wallets_promoted_reset"] == 1


def test_cleanup_all_clears_discovery_and_raw_rows() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(CandidateWallet(wallet_address="0xabc", promoted=True))
        session.add(Market(market_id="m1"))
        session.add(Token(token_id="tok1", market_id="m1"))
        session.add(RawResponse(source="data_api", endpoint="test", url="https://example.test"))
        session.commit()

        cleanup_database(session, "all")
        session.commit()

        assert count(session, CandidateWallet) == 0
        assert count(session, Market) == 0
        assert count(session, Token) == 0
        assert count(session, RawResponse) == 0
