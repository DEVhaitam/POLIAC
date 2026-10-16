"""Loads provider vocabulary YAML + catalog CSVs.

The vocabulary is the closed set of values/types the model is allowed to propose for a given
attribute, and what the validator checks recommendations against.

Two on-disk layouts are supported:
- **Split** (`<provider>/resources/*.yaml` + `<provider>/catalogs/*.csv`) -- one YAML file per
  resource type, filename == resource type, in a directory separate from the catalog CSVs those
  files reference by name. Used by `aws` (11 resources at the time of writing).
- **Single-file legacy** (`<provider>.yaml`, one file with a top-level `resources:` map) -- kept
  for providers with too few resources to bother splitting. Used by `libvirt` (one resource).

Adding an attribute to an existing resource is a one-line YAML edit; adding a new resource under
the split layout is a new `resources/<resource_type>.yaml` file plus, optionally, a new
`catalogs/<name>.csv` -- no code changes either way.
"""

from __future__ import annotations

import csv
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from poliac.terraform.model import Resource

AttributeType = Literal["integer", "enum", "string", "boolean", "number"]


class AttributeSpec(BaseModel):
    type: AttributeType
    unit: str | None = None
    min: int | float | None = None
    # `max`/`step` are "number"-only (continuous/half-step values like Aurora Serverless ACUs,
    # where `enum`+`catalog` would wrongly reject legitimate values between the catalog's rows --
    # see ROADMAP.md M6.3). `int | float` (not just `float`) so a YAML `min: 8` on an existing
    # `integer` attribute still round-trips as `8`, not `8.0`, in the rendered prompt.
    max: int | float | None = None
    step: int | float | None = None
    catalog: str | None = None  # filename, resolved relative to the provider's catalogs dir
    key: str | None = None  # catalog column that holds the allowed value


class ComboSpec(BaseModel):
    """A set of attributes that are only valid *together* -- e.g. Fargate's `cpu`/`memory`, where
    only specific pairs are legal, not any combination of individually-valid values. Not
    expressible as independent per-attribute `AttributeSpec.catalog` checks (see ROADMAP.md M6.3);
    checked by `validate.py`'s V9 as its own pass, separate from V6.
    """

    attributes: list[str]  # e.g. ["cpu", "memory"]
    catalog: str
    keys: list[str]  # catalog columns, same order/length as `attributes`


class ResourceVocab(BaseModel):
    attributes: dict[str, AttributeSpec]
    combos: list[ComboSpec] = []


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


def _load_resources_from_directory(resources_dir: Path) -> dict[str, ResourceVocab]:
    """New layout: one YAML file per resource type, filename == resource type, file content is
    the `ResourceVocab` shape directly (`attributes: {...}`) -- no `provider:`/`resources:`
    wrapper, since both are already implied by the provider directory and the filename. Lives in
    `<provider>/resources/`, a sibling of `<provider>/catalogs/` (see `load_provider`) -- resource
    definitions and the catalog data they validate against are kept in separate directories."""
    vocab_resources: dict[str, ResourceVocab] = {}
    for yaml_path in sorted(resources_dir.glob("*.yaml")):
        raw = yaml.safe_load(yaml_path.read_text()) or {}
        vocab_resources[yaml_path.stem] = ResourceVocab.model_validate(raw)
    return vocab_resources


def _load_resources_from_single_file(yaml_path: Path) -> dict[str, ResourceVocab]:
    """Legacy layout: one `<provider>.yaml` with a top-level `resources:` map. Kept for providers
    that don't need splitting (currently just `libvirt`, a single resource)."""
    raw = yaml.safe_load(yaml_path.read_text()) or {}
    resources_raw = raw.get("resources", {})
    return {
        resource_type: ResourceVocab.model_validate(spec)
        for resource_type, spec in resources_raw.items()
    }


def _load_catalogs(vocab_resources: dict[str, ResourceVocab], catalog_dir: Path) -> dict[str, list[dict[str, str]]]:
    catalogs: dict[str, list[dict[str, str]]] = {}

    def load(name: str) -> None:
        if name in catalogs:
            return
        with (catalog_dir / name).open(newline="") as f:
            catalogs[name] = list(csv.DictReader(f))

    for resource in vocab_resources.values():
        for attr in resource.attributes.values():
            if attr.catalog:
                load(attr.catalog)
        for combo in resource.combos:
            load(combo.catalog)
    return catalogs


def load_provider(name: str) -> ProviderVocab:
    providers_root = _providers_dir()
    provider_dir = providers_root / name
    resources_dir = provider_dir / "resources"
    if resources_dir.is_dir():
        # split layout: <provider>/resources/*.yaml + <provider>/catalogs/*.csv -- resource
        # definitions and the catalog data they reference live in separate directories.
        vocab_resources = _load_resources_from_directory(resources_dir)
        catalog_dir = provider_dir / "catalogs"
    else:
        yaml_path = providers_root / f"{name}.yaml"
        if not yaml_path.is_file():
            raise ProviderNotFoundError(
                f"no provider vocabulary for {name!r} (checked {resources_dir}/*.yaml and {yaml_path})"
            )
        vocab_resources = _load_resources_from_single_file(yaml_path)
        catalog_dir = providers_root

    catalogs = _load_catalogs(vocab_resources, catalog_dir)
    return ProviderVocab(provider=name, resources=vocab_resources, catalogs=catalogs)


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


def select_present_resources(
    provider_vocab: dict[str, ProviderVocab], resources: list[Resource]
) -> dict[str, ProviderVocab]:
    """Trims each loaded `ProviderVocab` down to only the resource *types* actually present in
    this run's parsed Terraform -- `load_providers_for_resources` already narrows which providers
    get loaded at all, but a loaded provider's vocab still covers everything it supports (11
    resource types for `aws`), not just what's in front of us. A `.tf` file with a single
    `aws_instance` has no reason to see ElastiCache/Redshift/OpenSearch/etc. catalogs.

    Call this once, right before assembling the LLM prompt (`llm/context.py::build_context`) --
    it's the one place that decides "which resources we both support and were actually asked
    about" for this run. A resource type present in the `.tf` but with no matching vocab entry
    (out of scope, e.g. `aws_security_group`) is simply absent from the result, same as always --
    this doesn't change what's supported, only what's shown for what's already there.
    """
    present_types = {r.type for r in resources}
    selected: dict[str, ProviderVocab] = {}
    for provider_name, vocab in provider_vocab.items():
        trimmed = {
            resource_type: resource_vocab
            for resource_type, resource_vocab in vocab.resources.items()
            if resource_type in present_types
        }
        if trimmed:
            selected[provider_name] = ProviderVocab(provider=vocab.provider, resources=trimmed, catalogs=vocab.catalogs)
    return selected