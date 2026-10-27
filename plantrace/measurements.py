"""SI calculations from actual candidate geometry, with revision-bound review records."""

from datetime import datetime, timezone
import math
from shapely.geometry import LineString
from shapely.ops import unary_union
from shapely import __version__ as shapely_version, geos_version_string

from plantrace.calibration import assess_region, digest, region_for_geometry, independent_physical_pair, reference_line, MAX_REVIEW_REFERENCE_DISAGREEMENT
from plantrace.metrology_models import UNIT_TO_METRES
from plantrace.candidate_models import Candidate, CandidateSet
from plantrace.topology import build_topology
from plantrace.metrology_models import (
    ApprovalEvaluation, CalibrationBook, Measurement, MeasurementApproval,
    MeasurementSet, VerificationCheck,
)

AREA_CONVENTION = (
    "2D candidate polygon exterior area minus explicit holes, using extracted stroke-centerline boundaries. "
    "No gross, net, usable, legal or architectural area convention is asserted."
)
LENGTH_CONVENTION = "Length along the candidate's straight extracted stroke centerline; not a wall-face or material-thickness measurement."
CALCULATION_ENGINE = f"Shapely {shapely_version}; GEOS {geos_version_string}"
REVIEW_POLICY_VERSION = "stage6_evidence_v1"


def _source_covers_geometry(candidate: Candidate, additional_lines=()) -> bool:
    """Verify geometric support from original straight commands, not only flags."""
    strokes=[]
    for evidence in candidate.evidence:
        points=[(p.x,p.y) for p in evidence.source_coordinates]
        if evidence.operation in {"line","close_path"} and len(points)==2:
            strokes.append(LineString(points))
        elif evidence.operation in {"rectangle","quad"} and len(points)==4:
            if evidence.edge_index is not None and 0<=evidence.edge_index<4:
                index=evidence.edge_index
                strokes.append(LineString([points[index],points[(index+1)%4]]))
            else:
                strokes.append(LineString(points+[points[0]]))
    strokes.extend(additional_lines)
    if not strokes:
        return False
    shape=candidate.geometry.as_shapely()
    boundary=shape.boundary if candidate.candidate_type=="room" else shape
    return boundary.difference(unary_union(strokes).buffer(1e-6)).length<=1e-6


def _inferred(candidate: Candidate, by_id: dict[str, Candidate]) -> tuple[bool, list[str]]:
    ids = set(candidate.boundary_bridge_ids)
    seen = set()
    current = candidate
    while current.supersedes_id and current.supersedes_id in by_id and current.supersedes_id not in seen:
        seen.add(current.supersedes_id)
        current = by_id[current.supersedes_id]
        ids.update(current.boundary_bridge_ids)
    inferred = bool(ids) or "hypothetical_boundary_closure" in candidate.flags
    if candidate.candidate_type == "room" and candidate.origin == "manual_correction":
        inferred = True  # Entered polygon boundaries have not been matched back to observed strokes.
    return inferred, sorted(ids)


def approval_blockers(measurement: Measurement) -> list[str]:
    reasons = []
    if measurement.value_si is None:
        reasons.append("No current usable SI value.")
    if measurement.calibration_status != "cross_checked":
        reasons.append("Two independent, agreeing known dimensions are required for manual measurement approval.")
    refs=measurement.calibration_references
    evidence_pair=any(measurement.calibration_primary_reference_id in {a.id,b.id} and independent_physical_pair(a,b) for i,a in enumerate(refs) for b in refs[i+1:])
    if measurement.calibration_correctness != "independently_supported" or not evidence_pair:
        reasons.append("Independent scale and unit evidence is missing or conflicting; internal reference agreement is not proof of correctness.")
    if measurement.candidate_type == "room":
        reasons.append("Room structure completeness is not established. A closed polygon cannot prove that no partition is missing; area approval remains blocked.")
    if measurement.review_policy_version != REVIEW_POLICY_VERSION:
        reasons.append("Historical measurement-review policy requires recalculation under the current evidence gate.")
    scales=[r.known_distance*UNIT_TO_METRES[r.unit]/reference_line(r).length for r in refs]
    if not scales or measurement.scale_metres_per_source_unit is None or not math.isfinite(measurement.scale_metres_per_source_unit) or measurement.scale_metres_per_source_unit<=0 or any(
        abs(s-measurement.scale_metres_per_source_unit)/min(s,measurement.scale_metres_per_source_unit) > min(measurement.calibration_diagnostic_tolerance,MAX_REVIEW_REFERENCE_DISAGREEMENT)+1e-12 for s in scales):
        reasons.append("Reference snapshot does not agree with the recorded measurement scale.")
    if measurement.boundary_support != "complete_source_supported" or measurement.has_inferred_boundaries:
        reasons.append("Inferred, manual or stale boundaries cannot be approved as source-supported measurements.")
    if measurement.verification_state not in {"cross_checked", "manually_approved"}:
        reasons.append("Outstanding geometry/calibration review checks must be resolved.")
    return reasons


def measure_candidates(candidates: CandidateSet, book: CalibrationBook, geometry_revision: int = 0,
                       approvals: list[MeasurementApproval] | None = None) -> MeasurementSet:
    if (candidates.document_id, candidates.page_number) != (book.document_id, book.page_number):
        raise ValueError("Calibration belongs to a different document/page.")
    assessments = [assess_region(book, region) for region in book.regions]
    by_region = {assessment.region.id: assessment for assessment in assessments}
    by_id = {candidate.id: candidate for candidate in candidates.candidates}
    superseded = {c.supersedes_id for c in candidates.candidates if c.supersedes_id}
    edited_walls = [c for c in candidates.candidates if c.origin == "manual_correction" and c.candidate_type == "wall"]
    # Original command evidence is historical. A room also needs its current
    # effective wall/bridge dependencies, even when a caller deletes a wall
    # without producing a manual-correction object or incrementing an epoch.
    unsupported_rooms = {identity for issue in build_topology(candidates).issues
                         if issue.severity == "error" and issue.code == "unsupported_room_boundary"
                         for identity in issue.candidate_ids}
    scene_version = digest({"candidates": candidates.model_dump(mode="json"), "geometry_revision": geometry_revision})
    book_version = digest(book.model_dump(mode="json"))
    measurements = []
    for candidate in candidates.candidates:
        if candidate.candidate_type not in {"wall", "room"}:
            continue
        shape = candidate.geometry.as_shapely()
        inferred, bridge_ids = _inferred(candidate, by_id)
        source_supported=_source_covers_geometry(candidate)
        inferred = inferred or not source_supported
        historical_support = source_supported or candidate.id in unsupported_rooms and _source_covers_geometry(
            candidate, [b.geometry.as_shapely() for b in candidates.bridges if b.id in candidate.boundary_bridge_ids])
        stale = candidate.id in superseded or (candidate.id in unsupported_rooms and
                    candidate.origin == "automatic" and historical_support)
        rebuilt_current=False
        if candidate.reconstruction_basis is not None:
            from plantrace.room_rebuild import reconstruction_is_current
            rebuilt_current=reconstruction_is_current(candidate,candidates) and candidate.id not in unsupported_rooms
            stale=stale or not rebuilt_current
        if candidate.candidate_type == "room":
            for wall in edited_walls:
                original = by_id.get(wall.supersedes_id)
                if not rebuilt_current and original and (original.id in candidate.related_candidate_ids or
                                 shape.intersection(original.geometry.as_shapely()).length > 1e-6 or
                                 shape.intersection(wall.geometry.as_shapely()).length > 1e-6):
                    stale = True
        manual = candidate.origin == "manual_correction"
        support = "stale" if stale else "manual_unverified" if manual else "inferred" if inferred else "complete_source_supported"
        region, assignment_error = region_for_geometry(book, candidate.geometry)
        assessment = by_region.get(region.id) if region else None
        status = assessment.status if assessment else "ambiguous_region"
        scale = assessment.metres_per_source_unit if assessment else None
        power = 2 if candidate.candidate_type == "room" else 1
        raw = shape.area if power == 2 else shape.length
        source_matches = all(e.source.document_id == book.document_id and e.source.page_number == book.page_number for e in candidate.evidence)
        warnings = list(assessment.warnings) if assessment else [assignment_error or "No calibration region."]
        if candidate.candidate_type=="room":
            warnings.append("Room structure completeness is not established. This is polygon area only; an entirely absent partition is not detectable from source geometry.")
        if inferred:
            warnings.append("Geometry includes inferred or manually supplied edges; this measurement cannot be automatically cross-checked or approved.")
        if stale:
            warnings.append("Candidate is superseded or its current wall dependencies are missing/edited. Its geometry/topology must be rebuilt before an SI value is used.")
        if manual:
            warnings.append("Manual coordinate geometry has not been re-associated with observed boundaries.")
        if candidate.status == "unresolved":
            warnings.append("Candidate geometry/classification is unresolved.")
        if "pdf_visibility_unresolved" in candidate.flags:
            warnings.append("PDF clipping/visibility and architectural classification remain unverified.")
        value = None
        if scale is not None and status != "invalid" and not stale and source_matches:
            try:
                value = raw * scale ** power
            except OverflowError:
                value = None
                warnings.append("SI calculation is outside the supported numeric range.")
            if value is not None and (not math.isfinite(value) or value <= 0):
                value = None
                warnings.append("SI calculation is outside the supported numeric range.")
        raw = raw if math.isfinite(raw) else None
        checks = [
            VerificationCheck(code="known_distance_calibration", passed=status in {"single_reference", "cross_checked"}, explanation="Scale must come from consistent known-distance references, not a printed hypothesis."),
            VerificationCheck(code="independent_scale_comparison", passed=status == "cross_checked", explanation="Independent manually associated dimensions agree within the configured diagnostic tolerance."),
            VerificationCheck(code="independent_scale_unit_evidence", passed=assessment is not None and assessment.correctness_status=="independently_supported", explanation="The primary span needs a distinct physical evidence record/error-source comparison with explicit units. Review disagreement is capped at 2% or the smaller diagnostic tolerance; this is gate policy, not engineering accuracy. Declarations are not authenticated."),
            VerificationCheck(code="structure_completeness", passed=candidate.candidate_type!="room", explanation="Room geometry alone cannot establish the absence of missing structure; no independent completeness validation is implemented."),
            VerificationCheck(code="complete_source_boundary", passed=support == "complete_source_supported", explanation="No inferred/manual boundary closure is used."),
            VerificationCheck(code="original_command_coverage", passed=source_supported, explanation="Source straight commands cover the measured line or all polygon rings within 0.000001 drawing units."),
            VerificationCheck(code="current_geometry_dependencies", passed=not stale, explanation="Superseded candidates and regions affected by wall edits require reconstruction."),
            VerificationCheck(code="resolved_candidate", passed=candidate.status == "provisional", explanation="Unresolved candidate flags prevent measurement cross-check status."),
            VerificationCheck(code="source_scope", passed=source_matches, explanation="All source evidence belongs to the calibrated document/page."),
        ]
        geometry_ok = support == "complete_source_supported" and candidate.status == "provisional" and source_matches
        review_ok=geometry_ok and candidate.candidate_type=="wall" and assessment is not None and assessment.correctness_status=="independently_supported"
        state = "cross_checked" if value is not None and review_ok and status == "cross_checked" else "calculated" if value is not None and geometry_ok and status == "single_reference" else "needs_review"
        geometry_version = digest(candidate.geometry.model_dump(mode="json"))
        dependency = digest({"scene": scene_version, "book": book_version, "geometry": geometry_version,
                             "candidate": candidate.id, "region": region.id if region else None,
                             "method": "shapely_si_v1", "engine": CALCULATION_ENGINE,"review_policy":REVIEW_POLICY_VERSION})
        measurements.append(Measurement(
            id=f"measurement:{dependency}", candidate_id=candidate.id, candidate_type=candidate.candidate_type,
            candidate_origin=candidate.origin, candidate_status=candidate.status, geometry_version=geometry_version,
            dependency_version=dependency, source_document_id=book.document_id, page_number=book.page_number,
            source_units=book.regions[0].source_units, source_geometry=candidate.geometry,
            source_evidence=candidate.evidence, region_id=region.id if region else None,
            calibration_region=region,
            calibration_version=assessment.version if assessment else book_version, calibration_status=status,
            calibration_revision=book.revision, calibration_diagnostic_tolerance=book.diagnostic_tolerance,
            calibration_reference_ids=[r.id for r in assessment.references] if assessment else [],
            calibration_references=assessment.references if assessment else [],
            calibration_primary_reference_id=assessment.primary_reference_id if assessment else None,
            calibration_residuals=assessment.residuals if assessment else [],
            printed_hypothesis=assessment.printed_hypothesis if assessment else None,
            scale_metres_per_source_unit=scale, raw_drawing_value=raw, value_si=value,
            si_unit="m2" if power == 2 else "m",
            calculation_method=f"{CALCULATION_ENGINE}; {'Polygon.area * scale**2' if power == 2 else 'LineString.length * scale'}; shapely_si_v1",
            geometric_convention=AREA_CONVENTION if power == 2 else LENGTH_CONVENTION,
            has_inferred_boundaries=inferred, boundary_bridge_ids=bridge_ids, boundary_support=support,
            verification_state=state, review_status="needs_review" if state == "needs_review" else "not_reviewed",
            checks=checks, warnings=warnings,
            calibration_correctness=assessment.correctness_status if assessment else "unverified",
            structure_completeness="not_established" if candidate.candidate_type=="room" else "not_applicable",
            review_policy_version=REVIEW_POLICY_VERSION,
        ))
    evaluations = []
    current = {m.id: index for index, m in enumerate(measurements)}
    for approval in approvals or []:
        index = current.get(approval.measurement_id)
        valid = index is not None and measurements[index].dependency_version == approval.dependency_version and measurements[index].candidate_id == approval.candidate_id and not approval_blockers(measurements[index])
        evaluations.append(ApprovalEvaluation(approval=approval, status="current" if valid else "stale",
                                              reason="Current manual measurement review; not engineering approval." if valid else "Measurement dependency, calibration or geometry changed; prior approval is invalid."))
        if valid:
            measurements[index] = measurements[index].model_copy(update={"verification_state": "manually_approved", "review_status": "manually_approved", "approval_id": approval.id})
    return MeasurementSet(document_id=book.document_id, page_number=book.page_number, calibration=book,
                          assessments=assessments, geometry_revision=geometry_revision,
                          measurements=measurements, approvals=evaluations)


def approve_measurement(current: MeasurementSet, measurement_id: str, reviewer: str, note: str) -> MeasurementApproval:
    measurement = current.select_measurement(measurement_id)
    blockers = approval_blockers(measurement)
    if blockers:
        raise ValueError(" ".join(blockers))
    if not reviewer.strip() or not note.strip():
        raise ValueError("Reviewer name and review note are required. This records measurement review only.")
    timestamp = datetime.now(timezone.utc).isoformat()
    token = digest({"measurement": measurement.id, "reviewer": reviewer.strip(), "note": note.strip(), "time": timestamp})
    return MeasurementApproval(id=f"approval:{token}", measurement_id=measurement.id, candidate_id=measurement.candidate_id,
                               dependency_version=measurement.dependency_version, measurement_snapshot=measurement, reviewer=reviewer.strip(),
                               note=note.strip(), approved_at=timestamp)
