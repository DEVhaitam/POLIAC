"""Loads `.poliac.yaml` (SPEC.md §8). Missing file -> defaults, not an error."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

CONFIG_FILENAME = ".poliac.yaml"


class MetricsConfig(BaseModel):
    path: str = "./analysis.csv"
    max_rows: int = 300
    max_bytes: int = 100_000


class ContractConfig(BaseModel):
    # No default path: - "there is nothing sensible to default it to."
    path: str | None = None


class LlmConfig(BaseModel):
    # gemini / gemini-3.6-flash are the working defaults for this PoC's day-to-day use;
    # still fully overridable via .poliac.yaml or --llm-provider/--llm-model.
    provider: str = "gemini"  # "gemini" | "anthropic" | "openai" | "deepseek" | ...
    model: str = "gemini-3.6-flash"
    temperature: float = 0
    max_retries: int = 1
    timeout_s: int = 60


class Config(BaseModel):
    metrics: MetricsConfig = MetricsConfig()
    contract: ContractConfig = ContractConfig()
    llm: LlmConfig = LlmConfig()
    # No `iac_providers` field: which vocab packs (providers/*.yaml) apply is auto-detected
    # from the parsed Terraform itself (`Resource.provider`, derived from the type prefix) --
    # see `providers.registry.load_providers_for_resources`. Nothing for the user to declare.
    analyse_on_save: bool = False


class ConfigLoadResult(BaseModel):
    config: Config
    # Human-readable notes on what was assumed because it wasn't specified.
    # Surfaced via `window/showMessage` in the LSP server.
    assumptions: list[str] = []


def load_config(workspace_root: Path) -> ConfigLoadResult:
    config_path = workspace_root / CONFIG_FILENAME
    if not config_path.is_file():
        return ConfigLoadResult(
            config=Config(),
            assumptions=[f"no {CONFIG_FILENAME} in {workspace_root}; using defaults"],
        )

    raw = yaml.safe_load(config_path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{config_path}: expected a YAML mapping at the top level")
    return ConfigLoadResult(config=Config.model_validate(raw))
