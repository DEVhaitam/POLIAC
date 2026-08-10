"""Model output shape. What the LLM must return -- validated in `validate.py`."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Citation(BaseModel):
    row: int  # 0-based data row in the CSV
    column: str
    value: str  # must equal the cell content


class Recommendation(BaseModel):
    resource_address: str  # "libvirt_domain.vm"
    file: str
    attribute: str  # "vcpu"
    current_value: str
    recommended_value: str
    action: Literal["increase", "decrease", "change", "no_change"]
    confidence: Literal["low", "medium", "high"]
    rationale: str  # 1-4 sentences
    citations: list[Citation] = Field(min_length=1)


class AdvisorResult(BaseModel):
    recommendations: list[Recommendation]
    overall_assessment: str
