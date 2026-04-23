"""Pydantic models mirroring the entity JSON schemas.

Schemas live in ``linkedin-leads/schemas/*.schema.json``. This module is the
Python-side mirror. Enums and ``extra='forbid'`` match the schema's
``additionalProperties: false`` so drift between the two sides surfaces as
a ValidationError at load/write time rather than silently at consumer code.

Phase 4a scope: only ``Opportunity`` is modeled here. Other entities
(Lead, Conversation, InterviewLoop, PrepArtifact, Task, Signal, Application)
will be added when we need cross-linking from BetterApplyPilot (Phase 4b+)
or when linkedin-leads opts to consume them.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class OpportunityStatus(str, Enum):
    """Lifecycle status for an Opportunity.

    Higher-ranked states are 'later' in the lifecycle. ``_STATUS_RANK`` in
    BetterApplyPilot's exporter uses this ordering to prevent status
    downgrade on merge (e.g., a recruiter-sourced record already in
    ``interviewing`` isn't reverted to ``discovered`` just because BAP
    independently scrapes the same listing).
    """

    DISCOVERED = "discovered"
    CONTACTED = "contacted"
    APPLIED = "applied"
    SCREENING = "screening"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    ARCHIVED = "archived"


class OpportunitySource(str, Enum):
    """Where this Opportunity entered the system."""

    LINKEDIN = "linkedin"
    COMPANY_SITE = "company_site"
    JOB_BOARD = "job_board"
    REFERRAL = "referral"
    MANUAL = "manual"
    OTHER = "other"


class Opportunity(BaseModel):
    """A company+role the user is engaging with through either channel.

    Mirrors ``linkedin-leads/schemas/opportunity.schema.json``. Cross-link
    arrays (``lead_ids``, ``conversation_ids``, ``application_ids``,
    ``prep_artifact_ids``, ``signal_ids``) are populated by whichever system
    creates the related child record. ``extra='forbid'`` enforces the
    schema's ``additionalProperties: false`` at the Python layer.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    company: str
    role_title: str
    source: OpportunitySource
    status: OpportunityStatus

    location: Optional[str] = None
    compensation_hints: Optional[str] = None
    industry: Optional[str] = None
    fit_score: Optional[float] = Field(default=None, ge=0, le=100)
    priority_score: Optional[float] = None

    lead_ids: list[str] = Field(default_factory=list)
    conversation_ids: list[str] = Field(default_factory=list)
    application_ids: list[str] = Field(default_factory=list)
    interview_loop_id: Optional[str] = None
    prep_artifact_ids: list[str] = Field(default_factory=list)
    signal_ids: list[str] = Field(default_factory=list)

    job_url: Optional[str] = None
    description_summary: Optional[str] = None
    next_action: Optional[str] = None

    created_at: str  # ISO 8601 datetime
    updated_at: str  # ISO 8601 datetime

    def to_schema_dict(self) -> dict:
        """Return a dict suitable for writing to disk as schema-valid JSON.

        Enum members are converted to their string values; field order is
        preserved by Pydantic; no extra keys are emitted.
        """
        return self.model_dump(mode="json")
