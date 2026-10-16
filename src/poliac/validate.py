"""Validator. The anti-hallucination layer: checks the model's output against the
parsed Terraform and the metrics CSV. Does not decide anything -- only verifies that what the
model said is consistent with what it was given.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from poliac.llm.schema import Recommendation
from poliac.metrics.table import MetricsTable
from poliac.providers.registry import AttributeSpec, ProviderVocab, ResourceVocab
from poliac.terraform.model import Attribute, Resource


@dataclass
class ValidatedRecommendation:
    recommendation: Recommendation
    applicable: bool  # False when V8 fires: target attribute is not a literal


@dataclass
class Rejection:
    recommendation: Recommendation
    check: str  # "V1".."V7", or "V9" (cross-recommendation combo check, see _check_combos)
    reason: str


@dataclass
class ValidationOutcome:
    valid: list[ValidatedRecommendation]
    rejected: list[Rejection]


def _normalize_value(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1]
    return s


def _normalize_cell(s: str) -> str:
    s = s.strip()
    if s.endswith("%"):
        s = s[:-1].strip()
    return s


def _find_resource_vocab(resource: Resource, provider_vocab: dict[str, ProviderVocab]) -> ResourceVocab | None:
    for vocab in provider_vocab.values():
        resource_vocab = vocab.resources.get(resource.type)
        if resource_vocab is not None:
            return resource_vocab
    return None


def _find_attribute_spec(
    resource: Resource, attribute: str, provider_vocab: dict[str, ProviderVocab]
) -> AttributeSpec | None:
    resource_vocab = _find_resource_vocab(resource, provider_vocab)
    if resource_vocab and attribute in resource_vocab.attributes:
        return resource_vocab.attributes[attribute]
    return None


def _check_v6(
    rec: Recommendation, spec: AttributeSpec, provider_vocab: dict[str, ProviderVocab]
) -> str | None:
    value = rec.recommended_value
    if spec.type == "integer":
        try:
            n = int(_normalize_value(value))
        except ValueError:
            return f"recommended_value {value!r} is not an integer"
        if spec.min is not None and n < spec.min:
            return f"recommended_value {n} is below the minimum ({spec.min})"
    elif spec.type == "enum":
        if not spec.catalog or not spec.key:
            return None  # misconfigured vocab, not the model's fault -- nothing to check
        allowed = set()
        for vocab in provider_vocab.values():
            allowed |= vocab.allowed_catalog_values(spec.catalog, spec.key)
        if _normalize_value(value) not in allowed:
            return f"recommended_value {value!r} is not in the {spec.catalog} catalog"
    elif spec.type == "boolean":
        if _normalize_value(value).lower() not in {"true", "false"}:
            return f"recommended_value {value!r} is not a boolean"
    elif spec.type == "number":
        try:
            n = float(_normalize_value(value))
        except ValueError:
            return f"recommended_value {value!r} is not a number"
        if spec.min is not None and n < spec.min:
            return f"recommended_value {n} is below the minimum ({spec.min})"
        if spec.max is not None and n > spec.max:
            return f"recommended_value {n} is above the maximum ({spec.max})"
        if spec.step is not None:
            base = spec.min if spec.min is not None else 0
            remainder = (n - base) % spec.step
            if remainder > 1e-9 and abs(remainder - spec.step) > 1e-9:
                return f"recommended_value {n} is not a multiple of step {spec.step} (from {base})"
    return None


def _combo_allowed_rows(
    provider_vocab: dict[str, ProviderVocab], catalog: str, keys: list[str]
) -> set[tuple[str, ...]]:
    rows: set[tuple[str, ...]] = set()
    for vocab in provider_vocab.values():
        for row in vocab.catalogs.get(catalog, []):
            if all(key in row for key in keys):
                rows.add(tuple(row[key] for key in keys))
    return rows


def _effective_value(
    attribute: str, resource: Resource, recs_for_resource: list["ValidatedRecommendation"]
) -> str | None:
    """The value `attribute` would have *after* applying this batch of (already individually
    valid) recommendations -- the recommended_value if one of them targets it, else the
    resource's real current value from the parsed Terraform. `None` if neither exists (the
    attribute isn't set in the .tf and nothing recommends it -- nothing to check)."""
    for vr in recs_for_resource:
        if vr.recommendation.attribute == attribute:
            return _normalize_value(vr.recommendation.recommended_value)
    attr = resource.attributes.get(attribute)
    return _normalize_value(attr.raw_value) if attr is not None else None


def _check_combos(
    valid: list[ValidatedRecommendation],
    resources_by_address: dict[str, Resource],
    provider_vocab: dict[str, ProviderVocab],
) -> tuple[list[ValidatedRecommendation], list[Rejection]]:
    """V9: attributes declared together as a `ResourceVocab.combos` group (e.g. Fargate's
    `cpu`/`memory` -- only specific pairs are valid, not any combination of individually-valid
    values) can't be checked per-recommendation the way V6 checks a single attribute against a
    single catalog column. This runs once per resource address that has at least one
    already-valid recommendation, computes the *effective* value of every combo attribute
    (`_effective_value`), and rejects every recommendation touching that combo if the resulting
    tuple isn't a real row in the catalog -- even though each one already passed V1-V6 alone.
    """
    by_address: dict[str, list[ValidatedRecommendation]] = {}
    for vr in valid:
        by_address.setdefault(vr.recommendation.resource_address, []).append(vr)

    rejected: list[Rejection] = []
    rejected_ids: set[int] = set()

    for address, recs in by_address.items():
        resource = resources_by_address[address]
        resource_vocab = _find_resource_vocab(resource, provider_vocab)
        for combo in resource_vocab.combos if resource_vocab else []:
            effective = [_effective_value(attr, resource, recs) for attr in combo.attributes]
            if any(v is None for v in effective):
                continue  # a combo attribute isn't set in the .tf and nothing recommends it
            allowed = _combo_allowed_rows(provider_vocab, combo.catalog, combo.keys)
            if tuple(effective) in allowed:
                continue
            for vr in recs:
                if vr.recommendation.attribute in combo.attributes and id(vr) not in rejected_ids:
                    rejected_ids.add(id(vr))
                    rejected.append(
                        Rejection(
                            vr.recommendation,
                            "V9",
                            f"{combo.attributes} = {tuple(effective)} is not a valid combination "
                            f"in the {combo.catalog} catalog",
                        )
                    )

    still_valid = [vr for vr in valid if id(vr) not in rejected_ids]
    return still_valid, rejected


def _validate_one(
    rec: Recommendation,
    resources_by_address: dict[str, Resource],
    metrics: MetricsTable,
    provider_vocab: dict[str, ProviderVocab],
) -> ValidatedRecommendation | Rejection:
    resource = resources_by_address.get(rec.resource_address)
    if resource is None:
        return Rejection(rec, "V1", f"resource_address {rec.resource_address!r} not found")

    attr: Attribute | None = resource.attributes.get(rec.attribute)
    if attr is None:
        return Rejection(
            rec, "V2", f"attribute {rec.attribute!r} not found on {rec.resource_address}"
        )

    if _normalize_value(rec.current_value) != _normalize_value(attr.raw_value):
        return Rejection(
            rec,
            "V3",
            f"current_value {rec.current_value!r} does not match the actual value "
            f"{attr.raw_value!r}",
        )

    if not rec.citations:
        return Rejection(rec, "V4", "citations list is empty")
    for c in rec.citations:
        if metrics.cell(c.row, c.column) is None:
            return Rejection(rec, "V4", f"citation (row={c.row}, column={c.column!r}) not in the CSV")

    for c in rec.citations:
        actual = metrics.cell(c.row, c.column)
        if actual is not None and _normalize_cell(c.value) != _normalize_cell(actual):
            return Rejection(
                rec,
                "V5",
                f"citation (row={c.row}, column={c.column!r}) value {c.value!r} does not match "
                f"the actual cell {actual!r}",
            )

    spec = _find_attribute_spec(resource, rec.attribute, provider_vocab)
    if spec is not None:
        err = _check_v6(rec, spec, provider_vocab)
        if err:
            return Rejection(rec, "V6", err)

    if rec.action != "no_change" and _normalize_value(rec.recommended_value) == _normalize_value(
        rec.current_value
    ):
        return Rejection(
            rec, "V7", "recommended_value equals current_value but action is not 'no_change'"
        )

    return ValidatedRecommendation(recommendation=rec, applicable=attr.is_literal)  # V8


def validate(
    recommendations: list[Recommendation],
    resources: list[Resource],
    metrics: MetricsTable,
    provider_vocab: dict[str, ProviderVocab],
) -> ValidationOutcome:
    resources_by_address = {r.address: r for r in resources}
    valid: list[ValidatedRecommendation] = []
    rejected: list[Rejection] = []

    for rec in recommendations:
        outcome = _validate_one(rec, resources_by_address, metrics, provider_vocab)
        if isinstance(outcome, Rejection):
            rejected.append(outcome)
        else:
            valid.append(outcome)

    valid, combo_rejected = _check_combos(valid, resources_by_address, provider_vocab)
    rejected.extend(combo_rejected)

    return ValidationOutcome(valid=valid, rejected=rejected)


def log_rejections(workspace_root: Path, rejected: list[Rejection]) -> None:
    """Always log every dropped recommendation with its failure reason (SPEC §6.2) --
    shared by the CLI and the LSP server so both paths produce the same audit trail.
    """
    if not rejected:
        return
    poliac_dir = workspace_root / ".poliac"
    poliac_dir.mkdir(exist_ok=True)
    log_path = poliac_dir / "rejected.jsonl"
    with log_path.open("a") as f:
        for r in rejected:
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "check": r.check,
                "reason": r.reason,
                "recommendation": r.recommendation.model_dump(),
            }
            f.write(json.dumps(entry) + "\n")


def format_errors_for_retry(rejected: list[Rejection]) -> str:
    lines = [
        "The following recommendations failed validation. Correct them (or drop them) and "
        "return a full, corrected result matching the schema:",
        "",
    ]
    for r in rejected:
        rec = r.recommendation
        lines.append(
            f"- [{r.check}] {rec.resource_address}.{rec.attribute}: {r.reason}"
        )
    return "\n".join(lines)
