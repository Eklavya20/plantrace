from io import BytesIO

import pymupdf
import pytest
from pydantic import ValidationError

from plantrace.calibration import (
    add_region, assess_region, digest, make_reference, new_book, pixel_to_source,
    put_printed_scale, put_reference, region_for_geometry, remove_region, update_book,
)
from plantrace.candidates import extract_candidates
from plantrace.corrections import correct_candidate
from plantrace.documents import inspect_page, render_page
from plantrace.measurements import approve_measurement, measure_candidates
from plantrace.metrology_models import Measurement, MeasurementSet
from plantrace.candidate_models import CandidateGeometry
from plantrace.models import Point
from plantrace.overlays import source_to_pixel
from plantrace.point_picker import decode_selection, picker_frame
from support_provenance import with_physical_declarations


@pytest.fixture
def controlled_pdf():
    # Source-authored fixture: 800 x 600 rectangle, independent stroked boundaries.
    with pymupdf.open() as doc:
        page = doc.new_page(width=1200, height=1000)
        for a, b in [((100, 100), (900, 100)), ((900, 100), (900, 700)),
                     ((900, 700), (100, 700)), ((100, 700), (100, 100))]:
            page.draw_line(a, b, width=4)
        return doc.tobytes()


def reference(book, points, distance, label, unit="m", region_id=None):
    return make_reference(book, region_id or book.regions[0].id,
                          tuple(Point(x=x, y=y) for x, y in points), distance, unit, label)


def calibrated(inspection):
    book = new_book(inspection)
    first = reference(book, [(100, 100), (900, 100)], 4, "Horizontal dimension")
    book = put_reference(book, first)
    second = reference(book, [(100, 100), (100, 700)], 3, "Vertical independent dimension")
    return with_physical_declarations(put_reference(book, second))


def setup(controlled_pdf):
    inspection = inspect_page(controlled_pdf, "controlled.pdf")
    return inspection, extract_candidates(inspection), calibrated(inspection)


def test_exact_scale_length_area_from_real_candidates(controlled_pdf):
    inspection, candidates, book = setup(controlled_pdf)
    measured = measure_candidates(candidates, book)
    assert measured.assessments[0].metres_per_source_unit == pytest.approx(0.005, abs=1e-15)
    assert measured.assessments[0].status == "cross_checked"
    walls = [m for m in measured.measurements if m.candidate_type == "wall"]
    assert len(walls) == 4
    assert sorted(m.value_si for m in walls) == pytest.approx([3, 3, 4, 4], abs=1e-12)
    rooms = [m for m in measured.measurements if m.candidate_type == "room"]
    assert len(rooms) == 1
    assert rooms[0].value_si == pytest.approx(12, abs=1e-12)
    assert rooms[0].source_geometry.as_shapely().area == 800 * 600
    assert rooms[0].verification_state == "needs_review"  # Scale agreement cannot establish room completeness.
    assert not rooms[0].engineering_approved
    assert "No gross, net, usable" in rooms[0].geometric_convention
    assert not any(m.review_status == "manually_approved" for m in measured.measurements)


def test_scale_plus_ten_percent_gives_area_plus_twenty_one_percent(controlled_pdf):
    _, candidates, book = setup(controlled_pdf)
    original = measure_candidates(candidates, book)
    for ref in list(book.references):
        book = put_reference(book, type(ref).model_validate({**ref.model_dump(), "known_distance": ref.known_distance * 1.1}))
    changed = measure_candidates(candidates, book)
    first = next(m for m in original.measurements if m.candidate_type == "room")
    second = next(m for m in changed.measurements if m.candidate_type == "room")
    assert second.value_si == pytest.approx(14.52, abs=1e-12)
    assert second.value_si / first.value_si == pytest.approx(1.21, abs=1e-12)
    assert first.id != second.id
    assert first.geometry_version == second.geometry_version
    assert first.calibration_version != second.calibration_version


def test_conflicting_reference_dimensions_are_exposed_and_block_approval(controlled_pdf):
    _, candidates, book = setup(controlled_pdf)
    second = book.references[1].model_copy(update={"known_distance": 6.0})
    book = put_reference(book, second)
    result = measure_candidates(candidates, book)
    assessment = result.assessments[0]
    assert assessment.status == "conflicting"
    assert len(assessment.conflicts) == 1
    assert assessment.conflicts[0].relative_scale_difference == pytest.approx(1)
    assert assessment.residuals[1].residual_metres == pytest.approx(-3)
    assert assessment.residuals[1].relative_residual == pytest.approx(0.5)
    assert assessment.warnings
    assert all(m.verification_state == "needs_review" for m in result.measurements)
    # Nominal values remain explicitly unverified and anchored to the first reference.
    assert next(m.value_si for m in result.measurements if m.raw_drawing_value == 600) == pytest.approx(3)
    with pytest.raises(ValueError):
        approve_measurement(result, result.measurements[0].id, "Reviewer", "Checked")


def test_pairwise_conflict_cannot_hide_behind_primary_tolerance(controlled_pdf):
    inspection, _, _ = setup(controlled_pdf)
    book = new_book(inspection)
    for points, distance, label in [([(100,100),(900,100)],4,"anchor"),
                                     ([(100,100),(100,700)],2.97,"lower"),
                                     ([(100,800),(500,800)],2.02,"upper")]:
        book = put_reference(book, reference(book, points, distance, label))
    book = update_book(book, diagnostic_tolerance=0.015)
    assessment = assess_region(book, book.regions[0])
    assert all(residual.agrees_with_primary for residual in assessment.residuals)
    assert assessment.status == "conflicting"
    assert assessment.conflicts[0].reference_ids == (book.references[1].id, book.references[2].id)


def test_tolerance_boundary_is_diagnostic_and_configurable(controlled_pdf):
    _, _, book = setup(controlled_pdf)
    book = put_reference(book, book.references[1].model_copy(update={"known_distance": 3.06}))
    assert assess_region(book, book.regions[0]).status == "cross_checked"
    strict = update_book(book, diagnostic_tolerance=0.01)
    assert assess_region(strict, strict.regions[0]).status == "conflicting"


def test_missing_calibration_withholds_si_and_blocks_approval(controlled_pdf):
    inspection = inspect_page(controlled_pdf, "controlled.pdf")
    result = measure_candidates(extract_candidates(inspection), new_book(inspection))
    assert all(m.value_si is None and m.verification_state == "needs_review" for m in result.measurements)
    with pytest.raises(ValueError, match="SI value"):
        approve_measurement(result, result.measurements[0].id, "Reviewer", "No reference")


def test_single_reference_is_calculated_not_cross_checked(controlled_pdf):
    _, candidates, book = setup(controlled_pdf)
    book = update_book(book, references=book.references[:1])
    result = measure_candidates(candidates, book)
    assert result.assessments[0].status == "single_reference"
    assert all(m.verification_state == "calculated" for m in result.measurements)
    with pytest.raises(ValueError, match="independent"):
        approve_measurement(result, result.measurements[0].id, "Reviewer", "One reference")


def test_reversed_duplicate_and_reused_dimension_labels_do_not_cross_check(controlled_pdf):
    _, _, book = setup(controlled_pdf)
    original = book.references[0]
    duplicate = reference(book, [(900,100),(100,100)], 4, "Different label")
    duplicate_book = update_book(book, references=[original, duplicate])
    assert assess_region(duplicate_book, duplicate_book.regions[0]).status == "single_reference"
    same_label = book.references[1].model_copy(update={"dimension_label": original.dimension_label})
    labelled_book = update_book(book, references=[original, same_label])
    assert assess_region(labelled_book, labelled_book.regions[0]).status == "single_reference"


@pytest.mark.parametrize("unit,distance", [("m",4), ("cm",400), ("mm",4000), ("ft",4/0.3048), ("in",4/0.0254)])
def test_reference_unit_conversion_is_preserved_in_provenance(controlled_pdf, unit, distance):
    inspection = inspect_page(controlled_pdf, "controlled.pdf")
    book = new_book(inspection)
    ref = reference(book, [(100,100),(900,100)], distance, "known", unit)
    book = put_reference(book, ref)
    assessment = assess_region(book, book.regions[0])
    assert assessment.metres_per_source_unit == pytest.approx(0.005, abs=1e-15)
    assert assessment.residuals[0].known_metres == pytest.approx(4, abs=1e-12)
    assert assessment.residuals[0].unit_entered == unit


def test_printed_ratio_is_only_a_hypothesis(controlled_pdf):
    inspection, candidates, book = setup(controlled_pdf)
    book = put_printed_scale(book, book.regions[0].id, 100)
    verified = measure_candidates(candidates, book)
    assert verified.assessments[0].status == "cross_checked"
    assert verified.assessments[0].metres_per_source_unit == pytest.approx(0.005)
    assert any("disagrees" in warning for warning in verified.assessments[0].warnings)
    hypothesis = update_book(book, references=[])
    unverified = measure_candidates(candidates, hypothesis)
    assert unverified.assessments[0].status == "hypothesis_only"
    assert unverified.assessments[0].metres_per_source_unit == pytest.approx(100 * 0.0254 / 72)
    assert all(m.verification_state == "needs_review" for m in unverified.measurements)
    with pytest.raises(ValueError):
        approve_measurement(unverified, unverified.measurements[0].id, "Reviewer", "Printed only")


def test_existing_synthetic_rooms_measure_six_m2_but_inference_prevents_verification(pdf_bytes):
    inspection = inspect_page(pdf_bytes, "synthetic.pdf")
    book = new_book(inspection)
    book = put_reference(book, reference(book, [(100,100),(500,100)], 4, "Outer horizontal"))
    book = put_reference(book, reference(book, [(100,100),(100,400)], 3, "Outer vertical"))
    result = measure_candidates(extract_candidates(inspection), book)
    assert result.assessments[0].status == "cross_checked"
    rooms = [m for m in result.measurements if m.candidate_type == "room"]
    assert len(rooms) == 2
    assert all(m.value_si == pytest.approx(6) and m.has_inferred_boundaries for m in rooms)
    assert all(m.verification_state == "needs_review" and m.boundary_bridge_ids for m in rooms)
    for room in rooms:
        with pytest.raises(ValueError, match="Inferred"):
            approve_measurement(result, room.id, "Reviewer", "Cannot approve an inferred area")


def test_approval_is_manual_and_scale_changes_invalidate_it_even_if_restored(controlled_pdf):
    _, candidates, book = setup(controlled_pdf)
    first = measure_candidates(candidates, book)
    measurement = first.measurements[0]
    approval = approve_measurement(first, measurement.id, "Alice", "Compared both known dimensions and source stroke.")
    reviewed = measure_candidates(candidates, book, approvals=[approval])
    assert reviewed.measurements[0].verification_state == "manually_approved"
    assert MeasurementSet.model_validate_json(reviewed.model_dump_json())==reviewed
    assert not reviewed.measurements[0].engineering_approved
    assert not approval.engineering_approval
    changed = put_reference(book, book.references[0].model_copy(update={"known_distance": 4.4}))
    stale = measure_candidates(candidates, changed, approvals=[approval])
    assert stale.approvals[0].status == "stale"
    assert stale.approvals[0].approval.measurement_snapshot==measurement
    assert stale.approvals[0].approval.measurement_snapshot.calibration_references==book.references
    assert stale.approvals[0].approval.measurement_snapshot.calibration_diagnostic_tolerance==book.diagnostic_tolerance
    restored = put_reference(changed, book.references[0])
    restored_result = measure_candidates(candidates, restored, approvals=[approval])
    assert restored_result.approvals[0].status == "stale"
    with pytest.raises(KeyError):
        approve_measurement(stale, measurement.id, "Alice", "Old ID")


def test_wall_edit_invalidates_neighbor_room_without_rebuilding_it(controlled_pdf):
    inspection, candidates, book = setup(controlled_pdf)
    before = measure_candidates(candidates, book)
    original_room = next(m for m in before.measurements if m.candidate_type == "room")
    wall = next(c for c in candidates.list_candidates("wall") if c.geometry.as_shapely().bounds == (100,100,100,700))
    with pytest.raises(ValueError,match="completeness"):
        approve_measurement(before,original_room.id,"Alice","A closed face is insufficient")
    approval = approve_measurement(before,next(m.id for m in before.measurements if m.candidate_id==wall.id), "Alice", "Checked wall measurement")
    correction = correct_candidate(wall, [[110,100],[110,700]], inspection.page)
    edited = candidates.model_copy(update={"candidates": candidates.candidates + [correction]})
    after = measure_candidates(edited, book, geometry_revision=1, approvals=[approval])
    room = next(m for m in after.measurements if m.candidate_type == "room")
    assert room.source_geometry == original_room.source_geometry  # Stage 2 did not rebuild it.
    assert room.boundary_support == "stale" and room.value_si is None
    assert room.verification_state == "needs_review"
    assert room.id != original_room.id
    assert after.approvals[0].status == "stale"
    superseded = next(m for m in after.measurements if m.candidate_id == wall.id)
    assert superseded.value_si is None
    manual = next(m for m in after.measurements if m.candidate_id == correction.id)
    assert manual.value_si == pytest.approx(3)
    assert manual.verification_state == "needs_review"


def test_manual_room_edit_recalculates_but_never_inherits_approval(controlled_pdf):
    inspection, candidates, book = setup(controlled_pdf)
    original = candidates.list_candidates("room")[0]
    corrected = correct_candidate(original, [[110,100],[900,100],[900,700],[110,700],[110,100]], inspection.page)
    edited = candidates.model_copy(update={"candidates": candidates.candidates + [corrected]})
    measured = measure_candidates(edited, book)
    manual = next(m for m in measured.measurements if m.candidate_id == corrected.id)
    assert manual.value_si == pytest.approx(11.85)
    assert manual.has_inferred_boundaries and manual.verification_state == "needs_review"
    assert next(m for m in measured.measurements if m.candidate_id == original.id).value_si is None


def test_geometry_epoch_invalidates_review_for_unchanged_candidate_coordinates(controlled_pdf):
    _, candidates, book = setup(controlled_pdf)
    before = measure_candidates(candidates, book, geometry_revision=0)
    approval = approve_measurement(before, before.measurements[0].id, "Alice", "Checked")
    after = measure_candidates(candidates, book, geometry_revision=1, approvals=[approval])
    assert after.measurements[0].geometry_version == before.measurements[0].geometry_version
    assert after.approvals[0].status == "stale"


def test_distinct_regions_have_distinct_scales_and_cross_region_geometry_is_blocked(controlled_pdf):
    inspection, candidates, _ = setup(controlled_pdf)
    book = new_book(inspection)
    book = add_region(book, "Left detail", (0,0,600,1000))
    left = book.regions[-1]
    book = add_region(book, "Right detail", (600,0,1200,1000))
    right = book.regions[-1]
    for region, distance in [(left,4),(right,8)]:
        x0 = region.bounds[0] + 100
        book = put_reference(book, reference(book, [(x0,100),(x0,500)], distance, region.name, region_id=region.id))
    result = measure_candidates(candidates, book)
    vertical = {m.source_geometry.as_shapely().bounds[0]:m for m in result.measurements if m.candidate_type == "wall" and m.raw_drawing_value == 600}
    assert vertical[100].value_si == pytest.approx(6)
    assert vertical[900].value_si == pytest.approx(12)
    room = next(m for m in result.measurements if m.candidate_type == "room")
    assert room.value_si is None and room.calibration_status == "ambiguous_region"
    assert any("crosses" in warning for warning in room.warnings)
    assert remove_region(book, left.id).revision > book.revision


def test_overlapping_regions_are_ambiguous_even_with_page_fallback(controlled_pdf):
    inspection, candidates, book = setup(controlled_pdf)
    book = add_region(book, "Detail A", (0,0,1200,1000))
    book = add_region(book, "Detail B", (0,0,1200,1000))
    region, error = region_for_geometry(book, candidates.candidates[0].geometry)
    assert region is None and error


@pytest.mark.parametrize("rotation", [0,90,180,270])
def test_cropped_rotated_pdf_pixel_selection_gives_same_scale_and_area(rotation):
    with pymupdf.open() as doc:
        page = doc.new_page(width=1400,height=1200)
        for a,b in [((150,150),(950,150)),((950,150),(950,750)),((950,750),(150,750)),((150,750),(150,150))]:
            page.draw_line(a,b,width=4)
        page.set_cropbox(pymupdf.Rect(50,50,1250,1050))
        page.set_rotation(rotation)
        data=doc.tobytes()
    inspection = inspect_page(data,"crop.pdf")
    preview = render_page(data,"crop.pdf",max_edge=700)
    frame = picker_frame(inspection,preview)
    book = new_book(inspection)
    for points,distance,label in [([(100,100),(900,100)],4,"horizontal"), ([(100,100),(100,700)],3,"vertical")]:
        pixels = [Point(x=x,y=y) for x,y in [source_to_pixel(Point(x=x,y=y),preview) for x,y in points]]
        decoded = decode_selection({"frame":frame,"points":[p.model_dump() for p in pixels]},frame,preview)
        assert [value for p in decoded for value in (p.x,p.y)] == pytest.approx([value for point in points for value in point], abs=1e-10)
        ref = make_reference(book,book.regions[0].id,decoded,distance,"m",label,"preview_click",preview=preview,frame_id=frame)
        book=put_reference(book,ref)
    result=measure_candidates(extract_candidates(inspection),book)
    room=next(m for m in result.measurements if m.candidate_type=="room")
    assert room.value_si == pytest.approx(12,abs=1e-10)
    assert all(r.selection_method=="preview_click" for r in room.calibration_references)


def test_json_measurement_provenance_matches_current_geometry_and_references(controlled_pdf):
    _, candidates, book = setup(controlled_pdf)
    result=measure_candidates(candidates,book)
    assert MeasurementSet.model_validate_json(result.model_dump_json())==result
    for measurement in result.measurements:
        candidate=candidates.select_candidate(measurement.candidate_id)
        assert measurement.geometry_version==digest(candidate.geometry.model_dump(mode="json"))
        assert measurement.source_geometry==candidate.geometry
        assert measurement.source_evidence==candidate.evidence
        assert measurement.calibration_reference_ids==[r.id for r in book.references]
        assert measurement.calibration_references==book.references
        assert measurement.source_document_id==book.document_id
        assert measurement.page_number==book.page_number
        assert "Shapely" in measurement.calculation_method
    with pytest.raises(ValidationError):
        Measurement.model_validate({**result.measurements[0].model_dump(),"engineering_approved":True})


def test_invalid_reference_inputs_and_scope_are_rejected(controlled_pdf):
    inspection,_,book=setup(controlled_pdf)
    for points,distance in [([(1,1),(1,1)],1), ([(1,1),(2,2)],0), ([(1,1),(2,2)],float("nan")), ([(-1,0),(2,2)],1)]:
        with pytest.raises(ValueError):
            reference(book,points,distance,"bad")
    with pytest.raises(ValueError):
        add_region(book,"outside",(-1,0,1200,1000))
    with pytest.raises(ValueError):
        remove_region(book,book.regions[0].id)
    preview=render_page(controlled_pdf,"plan.pdf")
    with pytest.raises(ValueError,match="different preview"):
        decode_selection({"frame":"stale","points":[]},"current",preview)
    with pytest.raises(ValueError,match="outside"):
        pixel_to_source(Point(x=-1,y=1),preview)


def test_adding_explicit_region_invalidates_page_references_in_that_detail(controlled_pdf):
    _,candidates,book=setup(controlled_pdf)
    changed=add_region(book,"Different-scale detail",(0,0,1000,800))
    assessment=assess_region(changed,changed.regions[0])
    assert assessment.status=="invalid"
    assert assessment.metres_per_source_unit is None
    # The candidate belongs to the explicit region, which has no references yet.
    result=measure_candidates(candidates,changed)
    assert all(m.value_si is None for m in result.measurements)
    with pytest.raises(ValueError,match="different explicit"):
        put_reference(changed,book.references[0])


def test_preview_pixel_provenance_roundtrip_and_invalid_payloads(controlled_pdf):
    inspection=inspect_page(controlled_pdf,"controlled.pdf")
    preview=render_page(controlled_pdf,"controlled.pdf",max_edge=700)
    frame=picker_frame(inspection,preview)
    assert decode_selection({"frame":frame,"points":[]},frame,preview) is None
    assert decode_selection({"frame":frame,"points":[{"x":20,"y":30}]},frame,preview) is None
    for payload in [{"frame":frame,"points":"bad"},{"frame":frame,"points":[{},{}]}, {"frame":frame,"points":[{"x":float("nan"),"y":2},{"x":3,"y":4}]}]:
        with pytest.raises(ValueError):
            decode_selection(payload,frame,preview)
    altered=preview.model_copy(update={"source_to_pixel":(2,1,0.5,3,-7,11)})
    pixel=Point(x=23,y=81)
    source=pixel_to_source(pixel,altered)
    assert (source.x,source.y)==pytest.approx((10,20),abs=1e-12)


def test_no_fake_engineering_approval_and_wrong_document_are_rejected(controlled_pdf):
    _,candidates,book=setup(controlled_pdf)
    result=measure_candidates(candidates,book)
    with pytest.raises(ValueError,match="Reviewer"):
        approve_measurement(result,result.measurements[0].id," "," ")
    with pytest.raises(ValueError,match="different document"):
        measure_candidates(candidates,book.model_copy(update={"document_id":"other"}))


def test_changed_geometry_without_updated_evidence_cannot_claim_complete_support(controlled_pdf):
    _,candidates,book=setup(controlled_pdf)
    original=candidates.list_candidates("room")[0]
    changed_geometry=CandidateGeometry(type="Polygon",coordinates=[Point(x=x,y=y) for x,y in [(110,100),(900,100),(900,700),(110,700),(110,100)]])
    changed=original.model_copy(update={"geometry":changed_geometry})
    scene=candidates.model_copy(update={"candidates":[changed if c.id==original.id else c for c in candidates.candidates]})
    measured=measure_candidates(scene,book)
    area=next(m for m in measured.measurements if m.candidate_type=="room")
    assert area.value_si==pytest.approx(11.85)
    assert area.has_inferred_boundaries and area.verification_state=="needs_review"
    assert not next(check.passed for check in area.checks if check.code=="original_command_coverage")
