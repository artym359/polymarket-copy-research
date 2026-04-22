from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


def project_root() -> Path:
    source_path = Path(__file__).resolve()
    for parent in source_path.parents:
        if (parent / "pyproject.toml").exists() and (parent / "config" / "default.yaml").exists():
            return parent
    cwd = Path.cwd().resolve()
    if (cwd / "pyproject.toml").exists() and (cwd / "config" / "default.yaml").exists():
        return cwd
    return source_path.parents[2]


def resolve_project_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return project_root() / candidate


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(dict(merged[key]), value)
        else:
            merged[key] = value
    return merged


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    load_dotenv(project_root() / ".env")
    path = Path(
        config_path
        or os.getenv("PMCOPY_CONFIG")
        or project_root() / "config" / "default.yaml"
    )
    if not path.is_absolute():
        path = project_root() / path

    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    env_override: dict[str, Any] = {"app": {}, "api": {}}
    if os.getenv("PMCOPY_DATABASE_URL"):
        env_override["app"]["database_url"] = os.environ["PMCOPY_DATABASE_URL"]
    if os.getenv("PMCOPY_RAW_DATA_DIR"):
        env_override["app"]["raw_data_dir"] = os.environ["PMCOPY_RAW_DATA_DIR"]
    if os.getenv("PMCOPY_LOG_LEVEL"):
        env_override["app"]["log_level"] = os.environ["PMCOPY_LOG_LEVEL"]

    return _deep_merge(config, env_override)


def enabled_categories(config: Mapping[str, Any]) -> list[str]:
    categories = (
        config.get("wallet_discovery", {})
        .get("categories", {})
    )
    return [category for category, enabled in categories.items() if enabled]


def database_url(config: Mapping[str, Any]) -> str:
    return str(config.get("app", {}).get("database_url", "sqlite:///data/processed/pmcopy.sqlite3"))


def raw_data_dir(config: Mapping[str, Any]) -> Path:
    return resolve_project_path(config.get("app", {}).get("raw_data_dir", "data/raw"))
