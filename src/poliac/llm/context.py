"""Context builder. Deterministic and unit-testable: this is the part that decides
what the model can and cannot see. Nothing here injects domain knowledge -- it only formats
facts we already have (parsed Terraform, the metrics CSV, the contract, the provider vocabulary)
so the model can cite them precisely.
"""

from __future__ import annotations

from poliac.metrics.table import MetricsTable
from poliac.providers.registry import ProviderVocab
from poliac.terraform.model import Resource

TASK_INSTRUCTIONS = """\
You are advising on changes to the given Terraform, grounded in the given production metrics \
and the user's stated contract.

- The metrics are observed fact. The contract is the user's stated intent, and the user stating \
it can be mistaken about what the system can actually sustain. Do not silently favour the \
contract over the data: if satisfying the contract's intent would go against what the metrics \
show, do not comply quietly -- say so explicitly in `overall_assessment` (and in the relevant \
recommendation's `rationale`) so the conflict is visible, rather than picking a side yourself.
- Only recommend changes to attributes that appear in the provided resource inventory.
- Only propose `instance_type` values that appear in the provided catalog. Never invent an \
instance type.
- Every recommendation must cite at least one metrics cell by (row, column), and the cited \
`value` must be copied verbatim from that cell. The contract is not cited this way -- it is \
intent, not a data source.
- Do not invent metrics, resources, or attribute names that were not given to you.
- If the metrics do not support a change, or the contract doesn't call for one, return an empty \
`recommendations` list and explain why in `overall_assessment`. An empty result is a correct \
answer, not a failure.
- Output must match the provided JSON schema exactly. No prose outside it.
"""


def _render_terraform_sources(sources: dict[str, str]) -> str:
    parts = ["## Terraform source\n"]
    for path, text in sources.items():
        parts.append(f"### file: {path}\n```")
        lines = text.splitlines()
        width = len(str(len(lines)))
        for i, line in enumerate(lines, start=1):
            parts.append(f"{i:>{width}}| {line}")
        parts.append("```\n")
    return "\n".join(parts)


def _render_resource_inventory(resources: list[Resource]) -> str:
    inventory = [r.model_dump() for r in resources]
    import json

    return "## Structured resource inventory\n\n```json\n" + json.dumps(inventory, indent=2) + "\n```\n"


def _render_metrics(metrics: MetricsTable) -> str:
    parts = [
        "## Metrics CSV\n",
        f"Source: {metrics.path}",
        "Row 0 is the first data row (not the header). Cite cells as (row, column).\n",
        "```",
        "header: " + ",".join(metrics.headers),
    ]
    for i, row in enumerate(metrics.rows):
        values = ",".join(row.get(h, "") for h in metrics.headers)
        parts.append(f"row {i}: {values}")
    parts.append("```\n")
    return "\n".join(parts)


def _render_contract(contract_text: str) -> str:
    return (
        "## Contract (user's stated intent -- not a data source, not citable)\n\n"
        + contract_text.strip()
        + "\n"
    )


def _render_provider_vocabulary(provider_vocab: dict[str, ProviderVocab]) -> str:
    parts = ["## Provider vocabulary (the closed set of values you may propose)\n"]
    for vocab in provider_vocab.values():
        for resource_type, resource_vocab in vocab.resources.items():
            parts.append(f"### {resource_type} ({vocab.provider})\n")
            for attr_name, spec in resource_vocab.attributes.items():
                if spec.type == "enum" and spec.catalog and spec.key:
                    allowed = sorted(vocab.allowed_catalog_values(spec.catalog, spec.key))
                    parts.append(f"- `{attr_name}`: enum, allowed values: {allowed}")
                else:
                    constraints = []
                    if spec.unit:
                        constraints.append(f"unit={spec.unit}")
                    if spec.min is not None:
                        constraints.append(f"min={spec.min}")
                    suffix = f" ({', '.join(constraints)})" if constraints else ""
                    parts.append(f"- `{attr_name}`: {spec.type}{suffix}")
            parts.append("")
    return "\n".join(parts)


def build_context(
    sources: dict[str, str],
    resources: list[Resource],
    metrics: MetricsTable,
    contract_text: str,
    provider_vocab: dict[str, ProviderVocab],
) -> str:
    return "\n".join(
        [
            _render_terraform_sources(sources),
            _render_resource_inventory(resources),
            _render_metrics(metrics),
            _render_contract(contract_text),
            _render_provider_vocabulary(provider_vocab),
        ]
    )