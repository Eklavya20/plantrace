"""Calibration and measurement evidence, separate from building-entity approval."""

from typing import Literal
import math
from datetime import datetime

from pydantic import Field, model_validator
from shapely.geometry import box

from plantrace.candidate_models import CandidateGeometry, CommandEvidence
from plantrace.models import EvidenceModel, Point

DistanceUnit = Literal["m", "cm", "mm", "ft", "in"]
VerificationState = Literal["calculated", "cross_checked", "needs_review", "manually_approved"]
UNIT_TO_METRES = {"m": 1.0, "cm": 0.01, "mm": 0.001, "ft": 0.3048, "in": 0.0254}


class ScaleRegion(EvidenceModel):
    id: str
    document_id: str
    page_number: int = Field(ge=1)
    name: str = Field(min_length=1)
    bounds: tuple[float, float, float, float]
    scope: Literal["page", "region"] = "region"
    source_units: Literal["pdf_point", "pixel"]

    @model_validator(mode="after")
    def ordered_bounds(self) -> "ScaleRegion":
        x0, y0, x1, y1 = self.bounds
        if x1 <= x0 or y1 <= y0:
            raise ValueError("A calibration region needs positive width and height.")
        return self


class CalibrationReference(EvidenceModel):
    id: str
    region_id: str
    document_id: str
    page_number: int = Field(ge=1)
    points: tuple[Point, Point]
    known_distance: float = Field(gt=0)
    unit: DistanceUnit
    dimension_label: str = Field(min_length=1)
    selection_method: Literal["preview_click", "manual_source_coordinates"]
    association_method: Literal["manual_dimension_association"] = "manual_dimension_association"
    preview_frame_id: str | None = None
    preview_transform: tuple[float, float, float, float, float, float] | None = None
    preview_points: tuple[Point, Point] | None = None
    note: str = ""
    establishment: "ReferenceEstablishment | None" = None

    @model_validator(mode="after")
    def distinct_points(self) -> "CalibrationReference":
        if self.points[0] == self.points[1]:
            raise ValueError("Reference endpoints must be different.")
        if not self.dimension_label.strip():
            raise ValueError("Enter a label identifying the independent known dimension.")
        drawing = math.hypot(self.points[1].x - self.points[0].x, self.points[1].y - self.points[0].y)
        known = self.known_distance * UNIT_TO_METRES[self.unit]
        if drawing < 1e-9 or known <= 0 or not math.isfinite(known / drawing) or known / drawing <= 0:
            raise ValueError("Reference distance is too small or outside the supported numeric range.")
        if self.selection_method == "preview_click" and (not self.preview_frame_id or self.preview_transform is None or self.preview_points is None):
            raise ValueError("Preview selections must retain their frame, affine transform and pixel endpoints.")
        return self


class ReferenceEstablishment(EvidenceModel):
    """Manually recorded provenance, never authenticated physical truth.

    A group identifies a shared error source (survey/instrument/session), not
    an arbitrary dimension name. Reusing a source record cannot create proof.
    """
    kind: Literal["manual_assumption", "source_annotation", "independently_verified_physical"]
    independence_group: str = Field(min_length=1)
    evidence_record: str = Field(min_length=1)
    evidence_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    established_by: str = Field(min_length=1)
    established_at: str = Field(min_length=1)
    unit_basis: Literal["assumed", "explicit_source_annotation", "explicit_physical_reference"]
    unit_statement: str = Field(min_length=1)

    @model_validator(mode="after")
    def required_evidence(self):
        if any(not str(value).strip() for value in (self.independence_group,self.evidence_record,self.established_by,self.established_at,self.unit_statement)):
            raise ValueError("Evidence identity, group, recorder, date and unit statement cannot be blank.")
        if self.kind == "independently_verified_physical" and (self.evidence_sha256 is None or self.unit_basis != "explicit_physical_reference"):
            raise ValueError("Physical verification requires a recorded evidence SHA-256 and explicit physical units.")
        if self.kind == "independently_verified_physical":
            try:
                datetime.fromisoformat(self.established_at)
            except ValueError as exc:
                raise ValueError("Physical evidence needs an ISO date or timestamp.") from exc
        if self.kind == "source_annotation" and self.unit_basis == "explicit_physical_reference":
            raise ValueError("An annotation alone is not a physical unit verification.")
        return self


class PrintedScaleHypothesis(EvidenceModel):
    region_id: str
    denominator: float = Field(gt=0)
    note: str = "Manually entered printed ratio; original reproduction size is unverified."
    status: Literal["unverified_hypothesis"] = "unverified_hypothesis"


class CalibrationBook(EvidenceModel):
    document_id: str
    page_number: int
    regions: list[ScaleRegion] = Field(min_length=1)
    references: list[CalibrationReference] = Field(default_factory=list)
    printed_scales: list[PrintedScaleHypothesis] = Field(default_factory=list)
    diagnostic_tolerance: float = Field(default=0.02, ge=0, le=0.5)
    revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def consistent_scope(self) -> "CalibrationBook":
        region_ids = [r.id for r in self.regions]
        if len(region_ids) != len(set(region_ids)) or len([r for r in self.regions if r.scope == "page"]) != 1:
            raise ValueError("Calibration region IDs must be unique; only one page fallback is allowed.")
        if any(r.document_id != self.document_id or r.page_number != self.page_number for r in self.regions):
            raise ValueError("Every region must belong to this document/page.")
        page=next(r for r in self.regions if r.scope=="page")
        if any(r.source_units!=page.source_units or not box(*page.bounds).covers(box(*r.bounds)) for r in self.regions):
            raise ValueError("Regions must share the page's coordinate units and lie inside its extent.")
        if len({r.id for r in self.references}) != len(self.references):
            raise ValueError("Calibration reference IDs must be unique.")
        if any(r.region_id not in region_ids or r.document_id != self.document_id or r.page_number != self.page_number for r in self.references):
            raise ValueError("References must belong to a region on this document/page.")
        if len({h.region_id for h in self.printed_scales}) != len(self.printed_scales) or any(h.region_id not in region_ids for h in self.printed_scales):
            raise ValueError("Only one printed hypothesis per existing region is allowed.")
        if self.printed_scales and page.source_units!="pdf_point":
            raise ValueError("Printed ratios cannot calibrate raster pixels without established paper units.")
        return self


class ReferenceResidual(EvidenceModel):
    reference_id: str
    unit_entered: DistanceUnit
    distance_entered: float
    known_metres: float
    drawing_distance: float
    implied_metres_per_unit: float
    predicted_metres: float
    residual_metres: float
    relative_residual: float
    agrees_with_primary: bool
    independent_of_primary: bool
    comparison_span_distinct: bool = False
    evidence_independent_of_primary: bool = False


class ReferenceConflict(EvidenceModel):
    reference_ids: tuple[str, str]
    relative_scale_difference: float


class CalibrationAssessment(EvidenceModel):
    region: ScaleRegion
    version: str
    status: Literal["missing", "single_reference", "cross_checked", "conflicting", "hypothesis_only", "invalid"]
    metres_per_source_unit: float | None = None
    primary_reference_id: str | None = None
    references: list[CalibrationReference] = Field(default_factory=list)
    residuals: list[ReferenceResidual] = Field(default_factory=list)
    conflicts: list[ReferenceConflict] = Field(default_factory=list)
    diagnostic_tolerance: float
    warnings: list[str] = Field(default_factory=list)
    printed_hypothesis: PrintedScaleHypothesis | None = None
    printed_relative_difference: float | None = None
    correctness_status: Literal["unverified", "independently_supported", "conflicting"] = "unverified"
    correctness_reference_ids: list[str] = Field(default_factory=list)
    independent_evidence_groups: list[str] = Field(default_factory=list)
    unit_assumption_reference_ids: list[str] = Field(default_factory=list)
    evidence_conflicts: list[ReferenceConflict] = Field(default_factory=list)
    review_reference_tolerance: float = 0.02


class VerificationCheck(EvidenceModel):
    code: str
    passed: bool
    explanation: str


class Measurement(EvidenceModel):
    id: str
    candidate_id: str
    candidate_type: Literal["wall", "room"]
    candidate_origin: str
    candidate_status: str
    geometry_version: str
    dependency_version: str
    source_document_id: str
    page_number: int
    source_units: Literal["pdf_point", "pixel"]
    source_geometry: CandidateGeometry
    source_evidence: list[CommandEvidence]
    region_id: str | None
    calibration_region: ScaleRegion | None
    calibration_version: str
    calibration_revision: int
    calibration_diagnostic_tolerance: float
    calibration_status: str
    calibration_reference_ids: list[str]
    calibration_references: list[CalibrationReference]
    calibration_primary_reference_id: str | None
    calibration_residuals: list[ReferenceResidual]
    printed_hypothesis: PrintedScaleHypothesis | None = None
    scale_metres_per_source_unit: float | None
    raw_drawing_value: float | None
    value_si: float | None
    si_unit: Literal["m", "m2"]
    calculation_method: str
    geometric_convention: str
    has_inferred_boundaries: bool
    boundary_bridge_ids: list[str]
    boundary_support: Literal["complete_source_supported", "inferred", "manual_unverified", "stale"]
    verification_state: VerificationState
    review_status: Literal["not_reviewed", "needs_review", "manually_approved"]
    checks: list[VerificationCheck]
    warnings: list[str]
    approval_id: str | None = None
    engineering_approved: Literal[False] = False
    calibration_correctness: Literal["unverified", "independently_supported", "conflicting"] = "unverified"
    structure_completeness: Literal["not_established", "not_applicable"] = "not_established"
    review_policy_version: str = "legacy_internal_agreement_only"


class MeasurementApproval(EvidenceModel):
    id: str
    measurement_id: str
    candidate_id: str
    dependency_version: str
    measurement_snapshot: Measurement
    reviewer: str = Field(min_length=1)
    note: str = Field(min_length=1)
    approved_at: str
    scope: Literal["manual_measurement_review_only"] = "manual_measurement_review_only"
    engineering_approval: Literal[False] = False

    @model_validator(mode="after")
    def snapshot_matches_review(self) -> "MeasurementApproval":
        snapshot=self.measurement_snapshot
        if (snapshot.id,snapshot.candidate_id,snapshot.dependency_version)!=(self.measurement_id,self.candidate_id,self.dependency_version):
            raise ValueError("Approval must retain the exact measurement revision it reviews.")
        if snapshot.value_si is None or snapshot.has_inferred_boundaries or snapshot.boundary_support!="complete_source_supported" or snapshot.calibration_status!="cross_checked" or snapshot.verification_state not in {"cross_checked","manually_approved"}:
            raise ValueError("Approval snapshot must be a current, source-supported cross-checked measurement.")
        if snapshot.review_policy_version != "legacy_internal_agreement_only":
            from plantrace.measurements import approval_blockers
            if approval_blockers(snapshot):
                raise ValueError("Approval snapshot does not satisfy current independent-evidence policy.")
        return self


class ApprovalEvaluation(EvidenceModel):
    approval: MeasurementApproval
    status: Literal["current", "stale"]
    reason: str


class MeasurementSet(EvidenceModel):
    schema_version: str = "0.3"
    document_id: str
    page_number: int
    calibration: CalibrationBook
    assessments: list[CalibrationAssessment]
    geometry_revision: int
    measurements: list[Measurement]
    approvals: list[ApprovalEvaluation] = Field(default_factory=list)

    def select_measurement(self, measurement_id: str) -> Measurement:
        for measurement in self.measurements:
            if measurement.id == measurement_id:
                return measurement
        raise KeyError(f"Measurement {measurement_id!r} is not current on this page.")
