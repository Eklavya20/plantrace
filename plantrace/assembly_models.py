"""Typed Stage 4 records, reusing candidate and measurement evidence schemas."""

from typing import Any, Literal

from pydantic import Field

from plantrace.candidate_models import Candidate, CandidateGeometry, CandidateSet, DetectionConfig
from plantrace.metrology_models import CalibrationBook, Measurement, MeasurementApproval, MeasurementSet
from plantrace.models import DocumentInfo, EvidenceModel, PageInspection


class CorrectionContext(EvidenceModel):
    config: DetectionConfig
    candidates: list[Candidate]


class ReviewWorkspace(EvidenceModel):
    schema_version: str = "0.4"
    document: DocumentInfo
    inspection: PageInspection
    candidates: CandidateSet
    calibration: CalibrationBook
    geometry_revision: int = Field(default=0, ge=0)
    corrections: list[CorrectionContext] = Field(default_factory=list)
    measurement_approvals: list[MeasurementApproval] = Field(default_factory=list)
    measurements: MeasurementSet


class TopologyIssue(EvidenceModel):
    severity: Literal["error", "warning"]
    code: str
    message: str
    candidate_ids: list[str] = Field(default_factory=list)


class TopologyRelationship(EvidenceModel):
    id: str
    kind: Literal["wall_connectivity", "room_boundary", "room_boundary_hypothesis", "room_adjacency", "opening_host"]
    from_candidate_id: str
    to_candidate_id: str
    status: Literal["supported", "inferred", "unresolved"]
    support_kind: str
    support_geometry: dict[str, Any]
    supporting_bridge_ids: list[str] = Field(default_factory=list)
    assignment_origin: Literal["geometry", "manual_supported_assignment"] = "geometry"
    assignment_id: str | None = None
    traversability: Literal["not_assessed"] = "not_assessed"


class HostAssignment(EvidenceModel):
    id: str
    opening_id: str
    wall_id: str
    opening_geometry_version: str
    wall_geometry_version: str
    actor: str
    note: str
    created_at: str


class TopologySnapshot(EvidenceModel):
    version: str
    document_id: str
    page_number: int
    tolerance: float
    effective_candidate_ids: list[str]
    superseded_candidate_ids: list[str]
    excluded_candidate_ids: list[str] = Field(default_factory=list)
    relationships: list[TopologyRelationship]
    issues: list[TopologyIssue]


class ArchitectDecision(EvidenceModel):
    id: str
    candidate_id: str
    geometry_version: str
    topology_version: str
    decision: Literal["accept_provisional", "reject", "needs_review"]
    actor: str
    note: str
    created_at: str


class ValidationReport(EvidenceModel):
    scope: Literal["candidate_repository", "committed_entities"] = "candidate_repository"
    structurally_valid: bool
    issues: list[TopologyIssue]
    candidate_errors: dict[str, list[str]]
    engineering_handoff_eligible: Literal[False] = False
    handoff_reasons: list[str] = Field(default_factory=lambda: ["Formal engineering approval/handoff is not implemented."])


class BuildingEntity(EvidenceModel):
    id: str
    candidate_id: str
    entity_type: Literal["wall", "room", "opening"]
    geometry: CandidateGeometry
    geometry_version: str
    candidate_snapshot: Candidate
    source_units: Literal["pdf_point", "pixel"]
    status: Literal["provisional", "stale"] = "provisional"
    candidate_resolution_status: Literal["provisional", "unresolved"]
    review_status: str
    flags: list[str]
    measurement: Measurement | None = None
    retired_reason: str | None = None
    engineering_approved: Literal[False] = False
    engineering_handoff_eligible: Literal[False] = False


class EntityRelationship(EvidenceModel):
    id: str
    from_entity_id: str
    to_entity_id: str
    candidate_relationship: TopologyRelationship


class BuildingModel(EvidenceModel):
    schema_version: str = "0.4"
    model_id: str
    version: int = Field(ge=1)
    scope: Literal["single_document_page"] = "single_document_page"
    document_id: str
    page_number: int
    workspace_version: str
    created_at: str
    actor: str
    note: str
    entities: list[BuildingEntity]
    retired_entities: list[BuildingEntity]
    relationships: list[EntityRelationship]
    topology: TopologySnapshot
    review_decisions: list[ArchitectDecision]
    host_assignments: list[HostAssignment]
    source_review: ReviewWorkspace
    validation: ValidationReport
    status: Literal["provisional"] = "provisional"
    engineering_handoff_eligible: Literal[False] = False
