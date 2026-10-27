import pytest
from pydantic import ValidationError

from plantrace.candidate_models import CandidateSet
from plantrace.candidates import extract_candidates
from plantrace.corrections import correct_candidate
from plantrace.documents import inspect_page
from plantrace.overlays import MANUAL, render_overlay
from plantrace.documents import render_page
from PIL import Image
from io import BytesIO


def test_manual_room_correction_retains_automatic_evidence_and_lineage(pdf_bytes):
    inspection = inspect_page(pdf_bytes, "plan.pdf")
    automatic = extract_candidates(inspection)
    original = automatic.list_candidates("room")[0]
    before = original.model_dump_json()
    coordinates = [[110, 100], [300, 100], [300, 400], [110, 400], [110, 100]]
    correction = correct_candidate(original, coordinates, inspection.page)
    assert correction.origin == "manual_correction"
    assert correction.status == "unresolved"
    assert correction.supersedes_id == original.id
    assert correction.id != original.id
    assert correction.evidence == original.evidence
    assert original.model_dump_json() == before
    assert correction.boundary_bridge_ids == []
    assert "automatic_relationships_not_recomputed" in correction.flags
    result = automatic.model_copy(update={"candidates": automatic.candidates + [correction]})
    assert CandidateSet.model_validate_json(result.model_dump_json()) == result
    preview = render_page(pdf_bytes, "plan.pdf", max_edge=600)
    with Image.open(BytesIO(render_overlay(preview, result))) as overlay:
        assert overlay.getpixel((110, 180)) == MANUAL[:3]


def test_manual_wall_correction_requires_two_finite_in_bounds_points(pdf_bytes):
    inspection = inspect_page(pdf_bytes, "plan.pdf")
    original = extract_candidates(inspection).list_candidates("wall")[0]
    correction = correct_candidate(original, [[105, 100], [105, 400]], inspection.page)
    assert correction.origin == "manual_correction"
    for points in [[[0, 0], [1, 1], [2, 2]], [[0, 0], [0, 0]], [[-1, 0], [1, 1]], [[float("nan"), 0], [1, 1]], [[1], [2]]]:
        with pytest.raises((ValueError, ValidationError)):
            correct_candidate(original, points, inspection.page)


def test_invalid_or_unchanged_room_correction_is_rejected(pdf_bytes):
    inspection = inspect_page(pdf_bytes, "plan.pdf")
    original = extract_candidates(inspection).list_candidates("room")[0]
    for points in [
        [[100, 100], [300, 400], [100, 400], [300, 100], [100, 100]],
        [[100, 100], [300, 100], [300, 400], [100, 400]],
        [[p.x, p.y] for p in original.geometry.coordinates],
        {"wrong": "shape"},
    ]:
        with pytest.raises(ValueError):
            correct_candidate(original, points, inspection.page)


def test_opening_and_wrong_page_are_not_silently_edited(pdf_bytes):
    inspection = inspect_page(pdf_bytes, "plan.pdf")
    automatic = extract_candidates(inspection)
    with pytest.raises(ValueError, match="automatic wall or room"):
        correct_candidate(automatic.list_candidates("opening")[0], [[100, 100], [200, 100]], inspection.page)
    with pytest.raises(ValueError, match="source page"):
        correct_candidate(automatic.list_candidates("wall")[0], [[100, 100], [200, 100]], inspection.page.model_copy(update={"page_number": 2}))
