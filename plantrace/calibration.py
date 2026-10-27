"""Manual reference association, region selection and deterministic scale diagnostics."""

from hashlib import sha256
import json
from uuid import uuid4

from shapely.geometry import LineString, box

from plantrace.candidate_models import CandidateGeometry
from plantrace.metrology_models import (
    CalibrationAssessment, CalibrationBook, CalibrationReference, PrintedScaleHypothesis,
    ReferenceConflict, ReferenceResidual, ScaleRegion,
    UNIT_TO_METRES,
    ReferenceEstablishment,
)
from plantrace.models import PageInspection, Point, Preview

MAX_REVIEW_REFERENCE_DISAGREEMENT = 0.02  # Gate policy, not engineering accuracy.


def digest(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def new_book(inspection: PageInspection) -> CalibrationBook:
    page = inspection.page
    return CalibrationBook(
        document_id=inspection.document_id, page_number=page.page_number,
        regions=[ScaleRegion(
            id=f"{inspection.document_id}:p{page.page_number}:page_scale", document_id=inspection.document_id,
            page_number=page.page_number, name="Page fallback", bounds=(0, 0, page.width, page.height),
            scope="page", source_units=page.units,
        )],
    )


def update_book(book: CalibrationBook, **changes) -> CalibrationBook:
    """Every user change advances the epoch, even if prior settings are restored."""
    return CalibrationBook.model_validate({**book.model_dump(), **changes, "revision": book.revision + 1})


def add_region(book: CalibrationBook, name: str, bounds: tuple[float, float, float, float]) -> CalibrationBook:
    page = next(r for r in book.regions if r.scope == "page")
    region = ScaleRegion(
        id=f"region:{uuid4().hex}", document_id=book.document_id, page_number=book.page_number,
        name=name.strip(), bounds=bounds, source_units=page.source_units,
    )
    if not box(*page.bounds).covers(box(*region.bounds)):
        raise ValueError("Calibration region must be inside the source page.")
    return update_book(book, regions=book.regions + [region])


def remove_region(book: CalibrationBook, region_id: str) -> CalibrationBook:
    region = next(r for r in book.regions if r.id == region_id)
    if region.scope == "page":
        raise ValueError("The page fallback region cannot be removed.")
    return update_book(book, regions=[r for r in book.regions if r.id != region_id],
                       references=[r for r in book.references if r.region_id != region_id],
                       printed_scales=[h for h in book.printed_scales if h.region_id != region_id])


def reference_line(reference: CalibrationReference) -> LineString:
    return LineString([(p.x, p.y) for p in reference.points])


def put_reference(book: CalibrationBook, reference: CalibrationReference) -> CalibrationBook:
    region = next((r for r in book.regions if r.id == reference.region_id), None)
    if region is None or reference.document_id != book.document_id or reference.page_number != book.page_number:
        raise ValueError("Reference document/page/region does not match this calibration.")
    if not box(*region.bounds).covers(reference_line(reference)):
        raise ValueError("Both reference points must lie inside the selected calibration region.")
    assigned, error = region_for_geometry(book, CandidateGeometry(type="LineString",coordinates=list(reference.points)))
    if assigned is None or assigned.id != region.id:
        raise ValueError(error or "Reference lies in a different explicit scale region; select that region.")
    others = [r for r in book.references if r.id != reference.id]
    # Replacing a reference keeps its position: the first reference is the explicit anchor.
    positions = [i for i, r in enumerate(book.references) if r.id == reference.id]
    if positions:
        others.insert(positions[0], reference)
    else:
        others.append(reference)
    return update_book(book, references=others)


def make_reference(book: CalibrationBook, region_id: str, points: tuple[Point, Point], distance: float,
                   unit: str, label: str, method: str = "manual_source_coordinates", note: str = "",
                   preview: Preview | None = None, frame_id: str | None = None,
                   establishment: ReferenceEstablishment | None = None) -> CalibrationReference:
    if preview is not None and method == "preview_click":
        a, b, c, d, e, f = preview.source_to_pixel
        pixels = tuple(Point(x=a*p.x+c*p.y+e, y=b*p.x+d*p.y+f) for p in points)
    else:
        pixels = None
    reference = CalibrationReference(
        id=f"reference:{uuid4().hex}", region_id=region_id, document_id=book.document_id,
        page_number=book.page_number, points=points, known_distance=distance, unit=unit,
        dimension_label=label, selection_method=method, note=note,
        establishment=establishment or ReferenceEstablishment(kind="manual_assumption",
            independence_group=f"drawing:{book.document_id}",evidence_record="Caller-supplied dimensions; no independent evidence record",
            established_by="Unspecified API caller",established_at="Not supplied (assumption)",unit_basis="assumed",
            unit_statement=f"Explicit caller unit {unit}; correctness is an unverified assumption"),
        preview_frame_id=frame_id if method == "preview_click" else None,
        preview_transform=preview.source_to_pixel if preview is not None and method == "preview_click" else None,
        preview_points=pixels,
    )
    put_reference(book, reference)  # Shared scope and extent validation; caller stores explicitly.
    return reference


def put_printed_scale(book: CalibrationBook, region_id: str, denominator: float | None) -> CalibrationBook:
    region = next(r for r in book.regions if r.id == region_id)
    if denominator is not None and region.source_units != "pdf_point":
        raise ValueError("Printed ratios need PDF page points; raster DPI is not established.")
    hypotheses = [h for h in book.printed_scales if h.region_id != region_id]
    if denominator is not None:
        hypotheses.append(PrintedScaleHypothesis(region_id=region_id, denominator=denominator))
    return update_book(book, printed_scales=hypotheses)


def independent(a: CalibrationReference, b: CalibrationReference) -> bool:
    """Distinct comparison spans only. This does NOT establish evidence independence."""
    if a.dimension_label.strip().casefold() == b.dimension_label.strip().casefold():
        return False
    first, second = reference_line(a), reference_line(b)
    if first.equals(second):
        return False
    # Overlapping collinear references are conservatively the same dimension evidence.
    if first.intersection(second).length > 1e-6:
        return False
    # Numerical duplicate/overlap guard for slightly shifted click coordinates.
    a0, a1 = first.coords
    b0, b1 = second.coords
    ax, ay = a1[0] - a0[0], a1[1] - a0[1]
    bx, by = b1[0] - b0[0], b1[1] - b0[1]
    parallel = abs(ax * by - ay * bx) / (first.length * second.length) < 1e-6
    if parallel and first.buffer(1e-4).intersection(second).length > 1e-4:
        return False
    return True


def independent_physical_pair(a: CalibrationReference, b: CalibrationReference) -> bool:
    records = [a.establishment,b.establishment]
    if not independent(a,b) or any(r is None or r.kind != "independently_verified_physical" or
                                  r.unit_basis != "explicit_physical_reference" for r in records):
        return False
    try:
        records=[ReferenceEstablishment.model_validate(r.model_dump()) for r in records]
    except ValueError:
        return False
    x,y=records
    # Two declarations referring to one artifact or shared survey/instrument
    # error domain are correlated, even if labels and spans differ.
    return (x.independence_group.strip().casefold() != y.independence_group.strip().casefold() and
            x.evidence_record.strip().casefold() != y.evidence_record.strip().casefold() and
            x.evidence_sha256 != y.evidence_sha256 and
            x.evidence_sha256 not in {a.document_id,b.document_id} and
            y.evidence_sha256 not in {a.document_id,b.document_id})


def assess_region(book: CalibrationBook, region: ScaleRegion) -> CalibrationAssessment:
    references = [r for r in book.references if r.region_id == region.id]
    hypothesis = next((h for h in book.printed_scales if h.region_id == region.id), None)
    version = digest({"book": book.model_dump(mode="json"), "region_id": region.id, "method": "anchor_pairwise_v2_evidence"})
    common = dict(region=region, version=version, references=references,
                  diagnostic_tolerance=book.diagnostic_tolerance, printed_hypothesis=hypothesis)
    for reference in references:
        assigned, error = region_for_geometry(book, CandidateGeometry(type="LineString",coordinates=list(reference.points)))
        if not box(*region.bounds).covers(reference_line(reference)) or assigned is None or assigned.id != region.id:
            return CalibrationAssessment(**common, status="invalid", warnings=[error or "Reference no longer belongs exclusively to this scale region."])
    if not references:
        if hypothesis is not None and region.source_units == "pdf_point":
            return CalibrationAssessment(**common, status="hypothesis_only", metres_per_source_unit=hypothesis.denominator * 0.0254 / 72,
                                         warnings=["Printed scale is an unverified reproduction-size hypothesis; not usable for approval."])
        return CalibrationAssessment(**common, status="missing", warnings=["No known-distance calibration reference."])
    scales = [r.known_distance * UNIT_TO_METRES[r.unit] / reference_line(r).length for r in references]
    scale = scales[0]
    primary = references[0]
    residuals = []
    for ref, implied in zip(references, scales):
        drawing = reference_line(ref).length
        known = ref.known_distance * UNIT_TO_METRES[ref.unit]
        error = drawing * scale - known
        relative = abs(error) / known
        residuals.append(ReferenceResidual(
            reference_id=ref.id, unit_entered=ref.unit, distance_entered=ref.known_distance,
            known_metres=known, drawing_distance=drawing, implied_metres_per_unit=implied,
            predicted_metres=drawing * scale, residual_metres=error, relative_residual=relative,
            agrees_with_primary=abs(implied-scale)/min(implied,scale) <= book.diagnostic_tolerance + 1e-12,
            independent_of_primary=ref.id != primary.id and independent(primary, ref),
            comparison_span_distinct=ref.id != primary.id and independent(primary,ref),
            evidence_independent_of_primary=ref.id != primary.id and independent_physical_pair(primary,ref),
        ))
    conflicts = []
    for i in range(len(references)):
        for j in range(i + 1, len(references)):
            difference = abs(scales[i] - scales[j]) / min(scales[i], scales[j])
            if difference > book.diagnostic_tolerance + 1e-12:
                conflicts.append(ReferenceConflict(reference_ids=(references[i].id, references[j].id), relative_scale_difference=difference))
    status = "conflicting" if conflicts else "cross_checked" if any(r.independent_of_primary for r in residuals) else "single_reference"
    warnings = []
    if conflicts:
        warnings.append("Known dimensions imply conflicting scales. Values use the first reference only as an unverified nominal anchor.")
    if status == "single_reference":
        warnings.append("No independent comparison dimension. Duplicate/overlapping references do not establish independence.")
    verified_ids=set()
    evidence_conflicts=[]
    review_tolerance=min(book.diagnostic_tolerance,MAX_REVIEW_REFERENCE_DISAGREEMENT)
    if not conflicts:
        for i,a in enumerate(references):
            for j,b in enumerate(references[i+1:],i+1):
                if independent_physical_pair(a,b):
                    difference=abs(scales[i]-scales[j])/min(scales[i],scales[j])
                    if difference>review_tolerance+1e-12:
                        evidence_conflicts.append(ReferenceConflict(reference_ids=(a.id,b.id),relative_scale_difference=difference))
                    else:
                        verified_ids.update([a.id,b.id])
    correctness="conflicting" if conflicts or evidence_conflicts else "independently_supported" if primary.id in verified_ids else "unverified"
    assumptions=[r.id for r in references if r.establishment is None or r.establishment.kind=="manual_assumption" or r.establishment.unit_basis=="assumed"]
    if correctness != "independently_supported":
        warnings.append("Reference agreement is internal consistency only. Independent scale/unit correctness is unverified; measurement approval is blocked.")
    else:
        warnings.append("Independent physical evidence is manually declared, not authenticated by PlanTrace. Shared systematic errors or false declarations may remain.")
    if assumptions:
        warnings.append("Some reference units are assumptions or have no recorded establishment evidence.")
    if evidence_conflicts:
        warnings.append("Declared independent physical references disagree beyond the measurement-review policy limit; increasing diagnostic tolerance cannot remove this block.")
    if hypothesis:
        printed_scale = hypothesis.denominator * 0.0254 / 72
        difference = abs(printed_scale - scale) / min(printed_scale, scale)
        warnings.append("Printed ratio is diagnostic only; known-distance references determine the scale.")
        if difference > book.diagnostic_tolerance:
            warnings.append("Printed-scale hypothesis disagrees with the known-distance anchor.")
    else:
        difference = None
    return CalibrationAssessment(**common, status=status, metres_per_source_unit=scale,
                                 primary_reference_id=primary.id, residuals=residuals, conflicts=conflicts,
                                 warnings=warnings, printed_relative_difference=difference,
                                 correctness_status=correctness,correctness_reference_ids=sorted(verified_ids),
                                 independent_evidence_groups=sorted({r.establishment.independence_group for r in references if r.id in verified_ids}),
                                 unit_assumption_reference_ids=assumptions,evidence_conflicts=evidence_conflicts,
                                 review_reference_tolerance=review_tolerance)


def region_for_geometry(book: CalibrationBook, geometry: CandidateGeometry) -> tuple[ScaleRegion | None, str | None]:
    shape = geometry.as_shapely()
    # Page fallback does not compete with explicit subregions. No centroid assignment.
    explicit = []
    for region in book.regions:
        if region.scope != "region":
            continue
        overlap = shape.intersection(box(*region.bounds))
        amount = overlap.area if geometry.type == "Polygon" else overlap.length
        if amount > 1e-9:
            explicit.append(region)
    if explicit:
        if len(explicit) == 1 and box(*explicit[0].bounds).covers(shape):
            return explicit[0], None
        return None, "Geometry crosses or overlaps distinct calibration regions; no single scalar applies."
    fallback = next((r for r in book.regions if r.scope == "page" and box(*r.bounds).covers(shape)), None)
    return (fallback, None) if fallback else (None, "Geometry is not fully covered by a calibration region.")


def pixel_to_source(point: Point, preview: Preview) -> Point:
    a, b, c, d, e, f = preview.source_to_pixel
    determinant = a * d - b * c
    if abs(determinant) < 1e-12:
        raise ValueError("Preview transform is not invertible.")
    if not 0 <= point.x <= preview.width or not 0 <= point.y <= preview.height:
        raise ValueError("Selected point lies outside the rendered preview.")
    x, y = point.x - e, point.y - f
    return Point(x=(d * x - c * y) / determinant, y=(-b * x + a * y) / determinant)
