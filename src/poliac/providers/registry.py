"""Loads provider vocabulary YAML + catalog CSVs.

The vocabulary is the closed set of values/types the model is allowed to propose for a given
attribute, and what the validator checks recommendations against. Adding a provider is
adding a YAML file plus, optionally, a catalog CSV -- no code changes here.
"""

from __future__ import annotations

import csv
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from poliac.terraform.model import Resource

AttributeType = Literal["integer", "enum", "string", "boolean"]


class AttributeSpec(BaseModel):
    type: AttributeType
    unit: str | None = None
    min: int | None = None
    catalog: str | None = None  # filename, resolved relative to the provider yaml
    key: str | None = None  # catalog column that holds the allowed value


class ResourceVocab(BaseModel):
    attributes: dict[str, AttributeSpec]


class ProviderVocab(BaseModel):
    provider: str
    resources: dict[str, ResourceVocab]
    # catalog filename -> rows (raw strings, keyed by CSV column)
    catalogs: dict[str, list[dict[str, str]]] = {}

    def allowed_catalog_values(self, catalog: str, key: str) -> set[str]:
        return {row[key] for row in self.catalogs.get(catalog, []) if key in row}


class ProviderNotFoundError(Exception):
    pass


def _providers_dir() -> Path:
    return Path(str(resources.files("poliac.providers")))


def load_provider(name: str) -> ProviderVocab:
    provider_dir = _providers_dir()
    yaml_path = provider_dir / f"{name}.yaml"
    if not yaml_path.is_file():
        raise ProviderNotFoundError(f"no provider vocabulary for {name!r} ({yaml_path})")

    raw = yaml.safe_load(yaml_path.read_text()) or {}
    resources_raw = raw.get("resources", {})
    vocab_resources = {
        resource_type: ResourceVocab.model_validate(spec)
        for resource_type, spec in resources_raw.items()
    }

    catalogs: dict[str, list[dict[str, str]]] = {}
    for resource in vocab_resources.values():
        for attr in resource.attributes.values():
            if attr.catalog and attr.catalog not in catalogs:
                catalog_path = provider_dir / attr.catalog
                with catalog_path.open(newline="") as f:
                    catalogs[attr.catalog] = list(csv.DictReader(f))

    return ProviderVocab(provider=raw.get("provider", name), resources=vocab_resources, catalogs=catalogs)


def load_providers(names: list[str]) -> dict[str, ProviderVocab]:
    return {name: load_provider(name) for name in names}


def load_providers_for_resources(resources: list[Resource]) -> tuple[dict[str, ProviderVocab], list[str]]:
    """Auto-detect which vocab packs are needed straight from the parsed Terraform -- each
    `Resource.provider` is already derived from its type prefix (`aws_instance` -> `aws`), so
    there's nothing for the user to declare. Resource types with no vocab file at all (out of
    scope for this PoC, e.g. `aws_security_group`) are not an error: their recommendations still
    get every fact-based check (V1-V5, V7, V8), just no V6 (nothing to check against), and the
    caller should surface the missing names as a non-fatal notice, not a failure.
    """
    names = sorted({r.provider for r in resources})
    vocab: dict[str, ProviderVocab] = {}
    missing: list[str] = []
    for name in names:
        try:
            vocab[name] = load_provider(name)
        except ProviderNotFoundError:
            missing.append(name)
    return vocab, missing