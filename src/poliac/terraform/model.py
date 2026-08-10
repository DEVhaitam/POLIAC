"""Parsed Terraform shapes"""

from __future__ import annotations

from pydantic import BaseModel


class SourceRange(BaseModel):
    # 0-based, LSP convention.
    start_line: int
    start_char: int
    end_line: int
    end_char: int


class Attribute(BaseModel):
    name: str
    raw_value: str  # exactly as written: "2", "\"t3.large\"", "var.cpu"
    is_literal: bool
    name_range: SourceRange
    value_range: SourceRange


class Resource(BaseModel):
    type: str  # "libvirt_domain" | "aws_instance"
    name: str  # "vm"
    address: str  # "libvirt_domain.vm"
    provider: str  # "libvirt" | "aws"
    attributes: dict[str, Attribute]
    block_range: SourceRange
    file: str
