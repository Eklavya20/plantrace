"""Session review edits, distinct from source-derived automatic candidates."""

from hashlib import sha256

from shapely.geometry import box

from plantrace.candidate_models import Candidate, CandidateGeometry
from plantrace.models import PageInfo, Point


def correct_candidate(original: Candidate, coordinates: list, page: PageInfo) -> Candidate:
    if original.origin != "automatic" or original.candidate_type not in {"wall", "room"}:
        raise ValueError("Select an automatic wall or room candidate to correct. Opening hypotheses are not manually relabelled.")
    if page.units != "pdf_point" or any(e.source.page_number != page.page_number for e in original.evidence):
        raise ValueError("Correction page must match the candidate's PDF source page.")
    if not isinstance(coordinates, list) or not 2 <= len(coordinates) <= 1000:
        raise ValueError("Enter a JSON list of 2 to 1000 coordinate pairs.")
    if any(not isinstance(pair, (list, tuple)) or len(pair) != 2 for pair in coordinates):
        raise ValueError("Each coordinate must be an [x, y] pair.")
    geometry = CandidateGeometry(
        type=original.geometry.type, coordinates=[Point(x=x, y=y) for x, y in coordinates],
        holes=original.geometry.holes,
    )
    shape = geometry.as_shapely()
    if not box(0, 0, page.width, page.height).covers(shape):
        raise ValueError("Corrected geometry must remain within the source page extent.")
    if shape.equals(original.geometry.as_shapely()):
        raise ValueError("The geometry is unchanged. Edit coordinates before saving a correction.")
    token = sha256((original.id + geometry.model_dump_json()).encode()).hexdigest()[:16]
    return Candidate(
        id=f"{original.id}:manual:{token}", candidate_type=original.candidate_type,
        geometry=geometry, evidence=original.evidence, extraction_method="manual_coordinate_correction_v1",
        status="unresolved", origin="manual_correction", supersedes_id=original.id,
        explanation="User-entered coordinate correction. The automatic candidate and its original source evidence remain available. Automatic topology and boundary relationships have not been recomputed.",
        flags=["manual_geometry_unverified", "automatic_relationships_not_recomputed"],
        related_candidate_ids=[original.id],
    )
