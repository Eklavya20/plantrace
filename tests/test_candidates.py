import math
import random

from pydantic import ValidationError
import pymupdf
import pytest
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from plantrace.candidate_models import CandidateGeometry, CandidateSet, DetectionConfig
from plantrace.candidates import extract_candidates
from plantrace.documents import inspect_page


def plan(draw):
    """Every automatic-detection test starts with a real PDF, not injected candidates."""
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=500)
        draw(page)
        return doc.tobytes()


def detect(data, config=None):
    return extract_candidates(inspect_page(data, "fixture.pdf"), config)


def partition(page, broken=False):
    page.draw_rect((100, 100, 500, 400), width=4)
    if broken:
        page.draw_line((300, 100), (300, 220), width=4)
        page.draw_line((300, 280), (300, 400), width=4)
    else:
        page.draw_line((300, 100), (300, 400), width=4)


def test_existing_synthetic_pdf_yields_evidenced_rooms_walls_and_opening(pdf_bytes):
    inspection = inspect_page(pdf_bytes, "synthetic.pdf")
    result = extract_candidates(inspection)
    walls, rooms, openings = [result.list_candidates(kind) for kind in ["wall", "room", "opening"]]
    assert len(walls) == 8  # Four boundary edges are split at partition intersections.
    assert len(rooms) == 2
    expected = [Polygon([(100, 100), (300, 100), (300, 400), (100, 400)]),
                Polygon([(300, 100), (500, 100), (500, 400), (300, 400)])]
    assert all(any(room.geometry.as_shapely().equals(polygon) for room in rooms) for polygon in expected)
    assert all(room.geometry.as_shapely().area == 60000 for room in rooms)  # Drawing units squared only.
    assert all("hypothetical_boundary_closure" in room.flags for room in rooms)
    assert len(result.bridges) == 1
    bridge = result.bridges[0]
    assert bridge.reason == "opening_hypothesis"
    assert all(room.boundary_bridge_ids == [bridge.id] for room in rooms)
    assert len(openings) == 1
    assert openings[0].geometry.as_shapely().equals(LineString([(300, 220), (300, 280)]))
    assert openings[0].status == "unresolved"
    assert any(e.operation == "cubic_bezier" for e in openings[0].evidence)
    assert all(c.origin == "automatic" for c in result.candidates)
    assert all(c.status in {"provisional", "unresolved"} for c in result.candidates)
    # No observed wall is extended across missing ink.
    actual = unary_union([wall.geometry.as_shapely() for wall in walls])
    assert actual.intersection(bridge.geometry.as_shapely()).length == 0
    assert not any("path4" in e.path_id for c in result.candidates for e in c.evidence)  # Furniture.
    source_by_id = {p.id: p for p in inspection.paths}
    for candidate in result.candidates:
        for evidence in candidate.evidence:
            path = source_by_id[evidence.path_id]
            assert evidence.source == path.source
            assert evidence.source_coordinates == path.commands[evidence.item_index].points


def test_closed_supported_partition_needs_no_bridge():
    result = detect(plan(partition))
    assert len(result.list_candidates("room")) == 2
    assert not result.bridges
    assert not result.list_candidates("opening")
    assert all(not c.boundary_bridge_ids for c in result.list_candidates("room"))


def test_gap_without_symbol_is_flagged_but_not_closed():
    result = detect(plan(lambda page: partition(page, broken=True)))
    assert len(result.list_candidates("opening")) == 1
    assert "no_symbol_support" in result.list_candidates("opening")[0].flags
    assert not result.bridges
    assert len(result.list_candidates("room")) == 1
    room = result.list_candidates("room")[0]
    assert room.status == "unresolved"
    assert "unclosed_interior_partition" in room.flags
    assert any(d.code == "dangling_boundaries" for d in result.diagnostics)


def test_disabling_hypothesis_does_not_claim_two_rooms(pdf_bytes):
    result = detect(pdf_bytes, DetectionConfig(use_opening_hypotheses=False))
    assert len(result.list_candidates("room")) == 1
    assert "unclosed_interior_partition" in result.list_candidates("room")[0].flags
    assert not result.bridges
    assert len(result.list_candidates("opening")) == 1


def broken_outer(page):
    for a, b in [((100, 100), (299, 100)), ((301, 100), (500, 100)),
                 ((500, 100), (500, 400)), ((500, 400), (100, 400)), ((100, 400), (100, 100))]:
        page.draw_line(a, b, width=4)


def test_configurable_small_gap_repair_preserves_source_wall_geometry():
    data = plan(broken_outer)
    strict = detect(data, DetectionConfig(endpoint_gap_tolerance=1))
    repaired = detect(data, DetectionConfig(endpoint_gap_tolerance=2.1))
    assert strict.list_candidates("room") == []
    assert len(repaired.list_candidates("room")) == 1
    assert repaired.bridges[0].reason == "endpoint_gap_tolerance"
    assert repaired.bridges[0].geometry.as_shapely().equals(LineString([(299, 100), (301, 100)]))
    assert [w.geometry for w in strict.list_candidates("wall")] == [w.geometry for w in repaired.list_candidates("wall")]
    assert "hypothetical_boundary_closure" in repaired.list_candidates("room")[0].flags


def test_endpoint_to_wall_interior_gap():
    def draw(page):
        page.draw_rect((100, 100, 500, 400), width=4)
        page.draw_line((300, 102), (300, 400), width=4)
    result = detect(plan(draw), DetectionConfig(endpoint_gap_tolerance=2.1))
    assert len(result.list_candidates("room")) == 2
    assert len(result.bridges) == 1
    assert result.bridges[0].geometry.as_shapely().equals(LineString([(300, 100), (300, 102)]))


def test_partial_walls_remain_without_rooms():
    def draw(page):
        page.draw_line((100, 100), (300, 100), width=4)
        page.draw_line((300, 100), (300, 250), width=4)
    result = detect(plan(draw))
    assert len(result.list_candidates("wall")) == 2
    assert not result.list_candidates("room")
    assert any(d.code == "dangling_boundaries" for d in result.diagnostics)


def test_decorative_lines_furniture_and_dashes_do_not_split_rooms():
    plain = detect(plan(partition))
    def draw(page):
        partition(page)
        page.draw_rect((130, 130, 200, 170), width=0.5)
        page.draw_line((120, 150), (480, 350), width=0.5)
        page.draw_line((100, 250), (500, 250), width=4, dashes="[3 2] 0")
    decorated = detect(plan(draw))
    assert [c.geometry for c in plain.candidates] == [c.geometry for c in decorated.candidates]
    assert {d.code for d in decorated.diagnostics} >= {"thin_stroke", "dashed_path"}


def test_isolated_thick_decorative_line_is_an_explicit_false_wall_candidate():
    def draw(page):
        partition(page)
        page.draw_line((20, 20), (80, 20), width=4)
    result = detect(plan(draw))
    isolated = [w for w in result.list_candidates("wall") if "isolated_stroke_ambiguous" in w.flags]
    assert len(isolated) == 1
    assert isolated[0].status == "unresolved"
    assert len(result.list_candidates("room")) == 2


def test_overlaps_and_fragmented_collinear_strokes_merge_with_all_evidence():
    def draw(page):
        for x in range(100, 200, 5):
            page.draw_line((x, 100), (x + 5, 100), width=4)
        page.draw_line((140, 100), (250, 100), width=4)
    result = detect(plan(draw))
    walls = result.list_candidates("wall")
    assert len(walls) == 1
    assert walls[0].geometry.as_shapely().equals(LineString([(100, 100), (250, 100)]))
    assert len(walls[0].evidence) == 21


def test_intersections_split_source_runs_at_real_junctions():
    def draw(page):
        page.draw_line((100, 200), (500, 200), width=4)
        page.draw_line((300, 100), (300, 400), width=4)
    result = detect(plan(draw))
    assert len(result.list_candidates("wall")) == 4
    assert all((300, 200) in list(w.geometry.as_shapely().coords) for w in result.list_candidates("wall"))
    assert all(len(w.related_candidate_ids) == 3 for w in result.list_candidates("wall"))


def test_invalid_bowtie_is_flagged_without_polygon_repair():
    data = plan(lambda page: page.draw_polyline([(100, 100), (500, 400), (100, 400), (500, 100)], closePath=True, width=4))
    result = detect(data)
    assert not result.candidates
    assert any(d.code == "invalid_source_path" for d in result.diagnostics)


@pytest.mark.parametrize("width,expected_walls", [(0.5, 0), (4, 4)])
def test_small_closed_shape_is_not_automatically_a_room(width, expected_walls):
    result = detect(plan(lambda page: page.draw_rect((150, 150, 210, 190), width=width)))
    assert len(result.list_candidates("wall")) == expected_walls
    assert not result.list_candidates("room")


def test_narrow_large_closed_shape_is_not_a_room():
    result = detect(plan(lambda page: page.draw_rect((50, 50, 550, 65), width=4)))
    assert not result.list_candidates("room")
    assert any(d.code == "small_or_narrow_enclosure" for d in result.diagnostics)


@pytest.mark.parametrize("style", ["fill", "transparent"])
def test_unpainted_or_fill_only_shapes_are_not_walls(style):
    def draw(page):
        if style == "fill":
            page.draw_rect((100, 100, 500, 400), color=None, fill=(0, 0, 0))
        else:
            page.draw_rect((100, 100, 500, 400), width=4, stroke_opacity=0)
    result = detect(plan(draw))
    assert not result.candidates


def test_off_page_stroke_does_not_create_wall():
    result = detect(plan(lambda page: page.draw_line((-20, 100), (200, 100), width=4)))
    assert not result.candidates
    assert any(d.code == "off_page_segment" for d in result.diagnostics)


def test_oblique_room_geometry_is_supported_without_axis_snapping():
    def draw(page):
        points = []
        angle = math.radians(30)
        for x, y in [(-100, -100), (100, -100), (100, 100), (-100, 100)]:
            points.append((300 + x * math.cos(angle) - y * math.sin(angle), 250 + x * math.sin(angle) + y * math.cos(angle)))
        page.draw_polyline(points, closePath=True, width=4)
    result = detect(plan(draw))
    assert len(result.list_candidates("room")) == 1
    assert result.list_candidates("room")[0].geometry.as_shapely().area == pytest.approx(40000, abs=1)
    assert result.list_candidates("room")[0].status == "unresolved"  # One isolated closed path.


def test_ambiguous_nearby_endpoint_targets_are_not_repaired():
    def draw(page):
        page.draw_line((100, 100), (100, 200), width=4)
        page.draw_line((99, 99), (99, 50), width=4)
        page.draw_line((101, 99), (101, 50), width=4)
    result = detect(plan(draw), DetectionConfig(endpoint_gap_tolerance=2.1))
    assert not result.bridges
    assert any(d.code == "ambiguous_endpoint_gap" for d in result.diagnostics)


@pytest.mark.parametrize("config", [DetectionConfig(max_segments=1), DetectionConfig(max_noded_segments=2)])
def test_complexity_limit_returns_no_partial_success(config):
    result = detect(plan(partition), config)
    assert not result.candidates
    assert any(d.code == "complexity_limit" for d in result.diagnostics)


def test_raster_and_scanned_page_have_no_invented_detection(pdf_bytes, png_bytes):
    for inspection in [inspect_page(pdf_bytes, "plan.pdf", 2), inspect_page(png_bytes, "plan.png")]:
        result = extract_candidates(inspection)
        assert not result.candidates
        assert not result.bridges
        assert result.diagnostics[0].code == "no_vector_geometry"


def test_candidates_are_stable_and_json_roundtrip_preserves_provenance(pdf_bytes):
    first = detect(pdf_bytes)
    second = detect(pdf_bytes)
    assert first == second
    assert len({c.id for c in first.candidates}) == len(first.candidates)
    assert CandidateSet.model_validate_json(first.model_dump_json()) == first
    assert first.select_candidate(first.candidates[0].id) == first.candidates[0]
    with pytest.raises(KeyError, match="not present"):
        first.select_candidate("missing")


@pytest.mark.parametrize("coordinates", [
    [(0, 0), (100, 100), (0, 100), (100, 0), (0, 0)],
    [(0, 0), (100, 0), (100, 100), (0, 100)],
])
def test_candidate_schema_rejects_invalid_or_unclosed_polygons(coordinates):
    with pytest.raises(ValidationError):
        CandidateGeometry(type="Polygon", coordinates=[{"x": x, "y": y} for x, y in coordinates])


def test_config_rejects_nonfinite_or_inverted_thresholds():
    with pytest.raises(ValidationError):
        DetectionConfig(endpoint_gap_tolerance=float("nan"))
    with pytest.raises(ValidationError):
        DetectionConfig(min_opening_gap=200, max_opening_gap=100)


def test_unrelated_curve_does_not_license_opening_closure():
    def draw(page):
        partition(page, broken=True)
        # A cubic starts at the gap but its endpoint and tangents are not swing-like.
        page.draw_bezier((300, 280), (310, 270), (350, 300), (370, 310), width=1)
    result = detect(plan(draw))
    assert len(result.list_candidates("opening")) == 1
    assert "no_symbol_support" in result.list_candidates("opening")[0].flags
    assert not result.bridges


def test_parallel_close_outlines_are_not_bridged_sideways():
    def draw(page):
        page.draw_line((100, 100), (100, 400), width=4)
        page.draw_line((102, 100), (102, 400), width=4)
    result = detect(plan(draw), DetectionConfig(endpoint_gap_tolerance=2.1))
    assert not result.bridges
    assert not result.list_candidates("room")


def test_large_thick_decorative_loop_is_a_documented_false_room_candidate():
    result = detect(plan(lambda page: page.draw_rect((100, 100, 300, 300), width=4)))
    assert len(result.list_candidates("room")) == 1
    assert result.list_candidates("room")[0].status == "unresolved"
    assert "single_path_enclosure_ambiguous" in result.list_candidates("room")[0].flags


def test_fractional_oblique_intersections_keep_source_provenance():
    def draw(page):
        page.draw_line((100.3, 100.2), (400.6, 300.4), width=4)
        page.draw_line((100.1, 300.1), (400.4, 100.7), width=4)
    result = detect(plan(draw))
    assert len(result.list_candidates("wall")) == 4
    assert all(len(wall.evidence) == 1 for wall in result.list_candidates("wall"))
    assert all(len(wall.related_candidate_ids) == 3 for wall in result.list_candidates("wall"))


def test_dense_oblique_noding_never_emits_evidence_free_candidates():
    rng = random.Random(143)
    for _ in range(3):
        def draw(page):
            for _ in range(12):
                page.draw_line((rng.uniform(30, 570), rng.uniform(30, 470)),
                               (rng.uniform(30, 570), rng.uniform(30, 470)), width=4)
        result = detect(plan(draw))
        assert len(result.list_candidates("wall")) >= 12
        assert all(candidate.evidence and candidate.geometry.as_shapely().is_valid for candidate in result.candidates)
        assert not any(d.code.startswith("unresolved_wall_provenance") for d in result.diagnostics)
