from __future__ import annotations

from pathlib import Path


def test_no_initial_investment_roi_terms_added() -> None:
    root = Path(__file__).resolve().parents[1]
    checked_files = [
        root / "README.md",
        root / "src" / "pmcopy" / "features" / "wallet_metrics.py",
        root / "src" / "pmcopy" / "features" / "wallet_filters.py",
        root / "src" / "pmcopy" / "dashboard" / "pages" / "02_Wallet_Screener.py",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in checked_files).lower()

    forbidden = [
        "roi_on_initial_investment",
        "initial investment",
        "roi on initial investment",
        "copy-trading roi",
    ]
    assert all(term not in text for term in forbidden)
