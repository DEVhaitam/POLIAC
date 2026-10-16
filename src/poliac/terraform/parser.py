"""Terraform parser. tree-sitter + tree-sitter-hcl, for byte-accurate ranges.

python-hcl2 discards positions and is deliberately not used here -- ranges are what make the
LSP diagnostics and the validator's V3 check (`current_value` matches the real source) possible.

Only top-level `resource "type" "name" { ... }` blocks are extracted. Within a resource body,
attributes are flattened recursively: an unlabeled nested block (`root_block_device { ... }`,
`cpu { ... }`) contributes its own attributes under a dotted prefix (`root_block_device.
volume_size`), matching how they're addressed in the provider vocab (`providers/*.yaml`).
*Labeled* nested blocks (`provisioner "local-exec" { ... }`) are skipped -- their labels make
them repeatable/instance-like rather than a single addressable sub-object, and nothing in scope
needs them.
"""

from __future__ import annotations

from pathlib import Path

import tree_sitter_hcl
from tree_sitter import Language, Node, Parser as TSParser

from poliac.terraform.model import Attribute, Resource, SourceRange

_LANGUAGE = Language(tree_sitter_hcl.language())


class TerraformParseError(Exception):
    pass


def _range(node: Node) -> SourceRange:
    return SourceRange(
        start_line=node.start_point[0],
        start_char=node.start_point[1],
        end_line=node.end_point[0],
        end_char=node.end_point[1],
    )


def _text(source: bytes, node: Node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _string_lit_value(source: bytes, string_lit: Node) -> str:
    parts = [_text(source, c) for c in string_lit.children if c.type == "template_literal"]
    return "".join(parts)


def _is_literal_expression(expr: Node) -> bool:
    return len(expr.children) == 1 and expr.children[0].type == "literal_value"


def _parse_attribute(source: bytes, attr_node: Node) -> tuple[str, Attribute] | None:
    name_node = next((c for c in attr_node.children if c.type == "identifier"), None)
    expr_node = next((c for c in attr_node.children if c.type == "expression"), None)
    if name_node is None or expr_node is None:
        return None
    name = _text(source, name_node)
    return name, Attribute(
        name=name,
        raw_value=_text(source, expr_node),
        is_literal=_is_literal_expression(expr_node),
        name_range=_range(name_node),
        value_range=_range(expr_node),
    )


def _parse_body(source: bytes, body_node: Node, prefix: str = "") -> dict[str, Attribute]:
    attributes: dict[str, Attribute] = {}
    for child in body_node.children:
        if child.type == "attribute":
            parsed = _parse_attribute(source, child)
            if parsed is not None:
                name, attr = parsed
                attributes[f"{prefix}{name}"] = attr
        elif child.type == "block":
            if any(c.type == "string_lit" for c in child.children):
                continue  # labeled block (provisioner "local-exec" {...}) -- see module docstring
            name_node = next((c for c in child.children if c.type == "identifier"), None)
            nested_body = next((c for c in child.children if c.type == "body"), None)
            if name_node is None or nested_body is None:
                continue
            block_name = _text(source, name_node)
            attributes.update(_parse_body(source, nested_body, prefix=f"{prefix}{block_name}."))
    return attributes


def _parse_resource_block(source: bytes, block_node: Node, file: str) -> Resource | None:
    string_lits = [c for c in block_node.children if c.type == "string_lit"]
    if len(string_lits) < 2:
        return None  # malformed `resource` block (missing type or name label)

    resource_type = _string_lit_value(source, string_lits[0])
    resource_name = _string_lit_value(source, string_lits[1])
    provider = resource_type.split("_", 1)[0]

    body_node = next((c for c in block_node.children if c.type == "body"), None)
    attributes = _parse_body(source, body_node) if body_node is not None else {}

    return Resource(
        type=resource_type,
        name=resource_name,
        address=f"{resource_type}.{resource_name}",
        provider=provider,
        attributes=attributes,
        block_range=_range(block_node),
        file=file,
    )


def parse_source(source: bytes, file: str) -> list[Resource]:
    parser = TSParser(_LANGUAGE)
    tree = parser.parse(source)
    if tree.root_node.has_error:
        raise TerraformParseError(f"{file}: syntax error while parsing Terraform")

    resources: list[Resource] = []
    top_body = next((c for c in tree.root_node.children if c.type == "body"), None)
    if top_body is None:
        return resources

    for child in top_body.children:
        if child.type != "block":
            continue
        keyword_node = next((c for c in child.children if c.type == "identifier"), None)
        if keyword_node is None or _text(source, keyword_node) != "resource":
            continue  # not a `resource` block (variable/provider/output/locals/...)
        resource = _parse_resource_block(source, child, file)
        if resource is not None:
            resources.append(resource)

    return resources


def parse_file(path: Path) -> list[Resource]:
    return parse_source(path.read_bytes(), file=str(path))


def parse_files(paths: list[Path]) -> list[Resource]:
    resources: list[Resource] = []
    for path in paths:
        resources.extend(parse_file(path))
    return resources