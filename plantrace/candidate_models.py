"""Stage 2 review records; none of these types represents a verified entity."""

from typing import Literal

from pydantic import Field, model_validator
from shapely import __version__ as shapely_version, geos_version_string
from shapely.geometry import LineString, Polygon
from shapely.validation import explain_validity

from plantrace.models import EvidenceModel, Point, SourceReference


class DetectionConfig(EvidenceModel):
    # All thresholds are drawing units (PDF points), never building dimensions.
    min_stroke_width: float = Field(default=2.0, ge=0, le=100)
    min_segment_length: float = Field(default=12.0, gt=0, le=1000)
    endpoint_gap_tolerance: float = Field(default=2.0, ge=0, le=20)
    orientation_tolerance_degrees: float = Field(default=3.0, gt=0, le=15)
    min_room_area: float = Field(default=5000.0, gt=0)
    min_room_span: float = Field(default=40.0, gt=0)
    min_opening_gap: float = Field(default=12.0, gt=0)
    max_opening_gap: float = Field(default=120.0, gt=0, le=500)
    use_opening_hypotheses: bool = True
    max_segments: int = Field(default=2000, ge=1, le=5000)
    max_noded_segments: int = Field(default=10000, ge=1, le=20000)

    @model_validator(mode="after")
    def ordered_gaps(self) -> "DetectionConfig":
        if self.min_opening_gap > self.max_opening_gap:
            raise ValueError("Minimum opening gap exceeds maximum opening gap.")
        return self


class CandidateGeometry(EvidenceModel):
    type: Literal["LineString", "Polygon"]
    coordinates: list[Point]
    holes: list[list[Point]] = Field(default_factory=list)

    def as_shapely(self) -> LineString | Polygon:
        points = [(p.x, p.y) for p in self.coordinates]
        if self.type == "LineString":
            return LineString(points)
        return Polygon(points, [[(p.x, p.y) for p in ring] for ring in self.holes])

    @model_validator(mode="after")
    def valid_geometry(self) -> "CandidateGeometry":
        if self.type == "LineString":
            if len(self.coordinates) != 2 or self.holes:
                raise ValueError("A wall/opening segment requires exactly two points and no holes.")
        elif len(self.coordinates) < 4 or self.coordinates[0] != self.coordinates[-1]:
            raise ValueError("A polygon needs at least three vertices and an explicit closing point.")
        if any(len(ring) < 4 or ring[0] != ring[-1] for ring in self.holes):
            raise ValueError("Polygon holes must be explicitly closed rings.")
        shape = self.as_shapely()
        if shape.is_empty or not shape.is_valid or (self.type == "LineString" and shape.length == 0):
            raise ValueError(f"Invalid candidate geometry: {explain_validity(shape)}")
        return self


class CommandEvidence(EvidenceModel):
    path_id: str
    source: SourceReference
    item_index: int
    edge_index: int | None = None
    operation: str
    # Original command points, including Bezier controls; never adjusted for topology.
    source_coordinates: list[Point]


class RoomReconstructionBasis(EvidenceModel):
    method: Literal["current_wall_faces_v1"] = "current_wall_faces_v1"
    original_room_id: str
    original_geometry_version: str
    wall_geometry_versions: dict[str, str]
    bridge_geometry_versions: dict[str, str]
    network_version: str
    reviewer: str = Field(min_length=1)
    note: str = Field(min_length=1)


class Candidate(EvidenceModel):
    id: str
    candidate_type: Literal["wall", "room", "opening"]
    geometry: CandidateGeometry
    evidence: list[CommandEvidence]
    extraction_method: str
    status: Literal["provisional", "unresolved"] = "provisional"
    origin: Literal["automatic", "manual_correction"] = "automatic"
    explanation: str
    flags: list[str] = Field(default_factory=list)
    related_candidate_ids: list[str] = Field(default_factory=list)
    boundary_bridge_ids: list[str] = Field(default_factory=list)
    supersedes_id: str | None = None
    reconstruction_basis: RoomReconstructionBasis | None = None

    @model_validator(mode="after")
    def geometry_matches_type(self) -> "Candidate":
        expected = "Polygon" if self.candidate_type == "room" else "LineString"
        if self.geometry.type != expected:
            raise ValueError(f"{self.candidate_type} requires {expected} geometry.")
        if not self.evidence:
            raise ValueError("Candidates must retain source evidence.")
        return self


class BoundaryBridge(EvidenceModel):
    id: str
    geometry: CandidateGeometry
    reason: Literal["endpoint_gap_tolerance", "opening_hypothesis"]
    evidence: list[CommandEvidence]
    supporting_candidate_ids: list[str]
    status: Literal["hypothesis"] = "hypothesis"


class Diagnostic(EvidenceModel):
    code: str
    message: str
    path_ids: list[str] = Field(default_factory=list)
    candidate_ids: list[str] = Field(default_factory=list)


class SourceLineReview(EvidenceModel):
    evidence: CommandEvidence
    geometry: CandidateGeometry
    stroke_width: float | None
    dashed: bool
    covered_length: float
    unexplained_length: float
    interpretation: Literal["represented", "small_or_closed_graphic_ambiguous", "possible_missed_wall", "unexplained_linework"]
    affected_room_ids: list[str] = Field(default_factory=list)
    explanation: str


class SourceLineAudit(EvidenceModel):
    version: str = "source_line_coverage_v1"
    document_id: str
    page_number: int
    wall_geometry_version: str
    significant_length_threshold: float
    numeric_tolerance: float = 1e-6
    status: Literal["assessed_supported_straight_strokes", "review_limit_reached", "source_geometry_unavailable"]
    lines: list[SourceLineReview]
    unassessed_curve_commands: int = 0
    unassessed_invalid_paths: int = 0
    completeness: Literal["not_established"] = "not_established"


class CandidateSet(EvidenceModel):
    document_id: str
    page_number: int
    config: DetectionConfig
    extraction_version: str = "geometry-baseline-0.2"
    schema_version: str = "0.2"
    geometry_engine: str = f"Shapely {shapely_version}; GEOS {geos_version_string}"
    candidates: list[Candidate] = Field(default_factory=list)
    bridges: list[BoundaryBridge] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    source_line_audit: SourceLineAudit | None = None

    def list_candidates(self, candidate_type: str | None = None) -> list[Candidate]:
        return [c for c in self.candidates if candidate_type is None or c.candidate_type == candidate_type]

    def select_candidate(self, candidate_id: str) -> Candidate:
        for candidate in self.candidates:
            if candidate.id == candidate_id:
                return candidate
        raise KeyError(f"Candidate {candidate_id!r} is not present on this page.")
