"""Explainable geometry baseline over Stage 1 evidence, in source PDF points.

Observed wall runs are never stretched across missing ink. Separate hypothesis
bridges may close a topology gap, and rooms explicitly reference those bridges.
"""

from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256
import math

from shapely import STRtree
from shapely.errors import GEOSException
from shapely.geometry import LineString, Point as SPoint, Polygon, box
from shapely.ops import polygonize_full, unary_union

from plantrace.candidate_models import (
    BoundaryBridge, Candidate, CandidateGeometry, CandidateSet, CommandEvidence,
    DetectionConfig, Diagnostic,
)
from plantrace.models import PageInspection, Point, VectorPath

EPS = 1e-6
COMMON_FLAGS = ["graphics_heuristic_unverified", "pdf_visibility_unresolved"]


@dataclass
class Segment:
    line: LineString
    evidence: CommandEvidence


def _xy(point: Point) -> tuple[float, float]:
    return point.x, point.y


def _geometry(shape: LineString | Polygon) -> CandidateGeometry:
    shape = shape.normalize()
    polygon = shape.geom_type == "Polygon"
    return CandidateGeometry(
        type="Polygon" if polygon else "LineString",
        coordinates=[Point(x=x, y=y) for x, y in (shape.exterior.coords if polygon else shape.coords)],
        holes=[[Point(x=x, y=y) for x, y in ring.coords] for ring in shape.interiors] if polygon else [],
    )


def _evidence(path: VectorPath, command, edge: int | None = None) -> CommandEvidence:
    return CommandEvidence(
        path_id=path.id, source=path.source, item_index=command.item_index,
        edge_index=edge, operation=command.operation, source_coordinates=command.points,
    )


def _unique(items: list[CommandEvidence]) -> list[CommandEvidence]:
    return sorted({item.model_dump_json(): item for item in items}.values(),
                  key=lambda e: (e.path_id, e.item_index, -1 if e.edge_index is None else e.edge_index))


def _path_segments(path: VectorPath) -> list[Segment] | None:
    """Reject non-simple connected strokes instead of turning bowties into rooms."""
    segments: list[Segment] = []
    runs: list[list[tuple[float, float]]] = []
    for command in path.commands:
        points = [_xy(p) for p in command.points]
        if command.operation == "line":
            if runs and runs[-1] and runs[-1][0] != runs[-1][-1] and runs[-1][-1] == points[0]:
                runs[-1].append(points[1])
            else:
                runs.append(points)
            pairs = [(points[0], points[1])]
        elif command.operation in ("rectangle", "quad"):
            runs.append(points + [points[0]])
            pairs = list(zip(points, points[1:] + points[:1]))
        else:
            # Curves are retained by Stage 1, but not approximated as straight walls.
            runs.append([])
            continue
        for edge_index, (a, b) in enumerate(pairs):
            if a != b:
                segments.append(Segment(LineString([a, b]), _evidence(path, command, edge_index)))
    runs = [run for run in runs if len(run) >= 2]
    if path.close_path and len(runs) == 1 and all(c.operation == "line" for c in path.commands):
        run = runs[0]
        if run[0] != run[-1]:
            evidence = _evidence(path, path.commands[-1], -1).model_copy(update={
                "operation": "close_path", "source_coordinates": [Point(x=x, y=y) for x, y in [run[-1], run[0]]],
            })
            segments.append(Segment(LineString([run[-1], run[0]]), evidence))
            run.append(run[0])
    for run in runs:
        if run[0] == run[-1]:
            if len(run) < 4 or not Polygon(run).is_valid or Polygon(run).area <= EPS:
                return None
        elif not LineString(run).is_simple:
            return None
    return segments


def _line_parts(shape) -> list[LineString]:
    if shape.is_empty:
        return []
    if shape.geom_type == "LineString":
        coordinates = list(shape.coords)
        return [LineString([a, b]) for a, b in zip(coordinates, coordinates[1:]) if math.dist(a, b) > EPS]
    return [part for geometry in shape.geoms for part in _line_parts(geometry)]


def _linear_overlap(a: LineString, b: LineString) -> float:
    """Collinear interval overlap robust to GEOS noding's floating-point endpoints.

    EPS is only a numerical provenance tolerance; it does not close topology gaps.
    Requiring both endpoints on the other line avoids attributing mere crossings.
    """
    b0, b1 = list(b.coords)
    length = b.length
    if length <= EPS:
        return 0.0
    ux, uy = (b1[0] - b0[0]) / length, (b1[1] - b0[1]) / length
    offsets = [(p[0] - b0[0], p[1] - b0[1]) for p in a.coords]
    if any(abs(dx * uy - dy * ux) > EPS for dx, dy in offsets):
        return 0.0
    projected = [dx * ux + dy * uy for dx, dy in offsets]
    return max(0.0, min(length, max(projected)) - max(0.0, min(projected)))


def _boundary_overlap(boundary, line: LineString) -> float:
    return sum(_linear_overlap(part, line) for part in _line_parts(boundary))


def _straight_runs(lines: list[LineString]) -> list[LineString]:
    """Node intersections, deduplicate overlaps, merge straight degree-two joins."""
    edges = _line_parts(unary_union(lines))
    nodes: dict[tuple, list[int]] = defaultdict(list)
    for i, edge in enumerate(edges):
        for coordinate in edge.coords:
            nodes[coordinate].append(i)
    parent = list(range(len(edges)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for coordinate, indices in nodes.items():
        if len(indices) != 2:
            continue
        vectors = []
        for index in indices:
            other = next(p for p in edges[index].coords if p != coordinate)
            vectors.append((other[0] - coordinate[0], other[1] - coordinate[1]))
        (ax, ay), (bx, by) = vectors
        scale = math.hypot(ax, ay) * math.hypot(bx, by)
        if abs(ax * by - ay * bx) <= EPS * scale and ax * bx + ay * by < 0:
            parent[root(indices[1])] = root(indices[0])
    groups: dict[int, list[tuple]] = defaultdict(list)
    for i, edge in enumerate(edges):
        groups[root(i)].extend(edge.coords)
    runs = [LineString([min(points), max(points)]) for points in groups.values()]
    return sorted(runs, key=lambda line: line.normalize().wkb_hex)


def _facing(line: LineString, endpoint: tuple, direction: tuple, cosine: float) -> bool:
    other = next(p for p in line.coords if p != endpoint)
    dx, dy = endpoint[0] - other[0], endpoint[1] - other[1]
    return (dx * direction[0] + dy * direction[1]) / math.hypot(dx, dy) >= cosine


def _free_ends(lines: list[LineString]) -> list[tuple[tuple, int]]:
    nodes: dict[tuple, list[int]] = defaultdict(list)
    for index, line in enumerate(lines):
        for coordinate in line.coords:
            nodes[coordinate].append(index)
    return [(point, indices[0]) for point, indices in sorted(nodes.items()) if len(indices) == 1]


def _interior_clear(gap: LineString, network) -> bool:
    # Ignore only tiny endpoint neighborhoods, not the missing run itself.
    interior = gap.difference(unary_union([SPoint(p).buffer(EPS * 10) for p in gap.coords]))
    return not interior.intersects(network)


def _swing_cue(gap: LineString, points: list[Point], tolerance: float) -> bool:
    """Quarter-turn cubic geometry at a flanked gap; never a semantic door label."""
    radius = gap.length
    if radius <= EPS:
        return False
    a, b = list(gap.coords)
    allowed = max(tolerance, radius * 0.08)
    for hinge, tip in [(a, b), (b, a)]:
        ux, uy = (tip[0] - hinge[0]) / radius, (tip[1] - hinge[1]) / radius
        for ordered in [points, list(reversed(points))]:
            start, control1, control2, end = [_xy(p) for p in ordered]
            if math.dist(start, tip) > allowed:
                continue
            dx, dy = end[0] - hinge[0], end[1] - hinge[1]
            if abs(dx * ux + dy * uy) > allowed or abs(abs(dx * uy - dy * ux) - radius) > allowed:
                continue
            tangent1 = (control1[0] - start[0], control1[1] - start[1])
            tangent2 = (end[0] - control2[0], end[1] - control2[1])
            lengths = [math.hypot(*v) for v in [tangent1, tangent2]]
            if min(lengths) < radius * 0.15 or max(lengths) > radius:
                continue
            if abs(tangent1[0] * ux + tangent1[1] * uy) / lengths[0] < 0.15 and abs(tangent2[0] * ux + tangent2[1] * uy) / lengths[1] > 0.98:
                return True
    return False


def extract_candidates(inspection: PageInspection, config: DetectionConfig | None = None) -> CandidateSet:
    result=_extract_candidates(inspection,config)
    if result.source_line_audit is None:
        from plantrace.source_audit import audit_source_lines
        result=result.model_copy(update={"source_line_audit":audit_source_lines(inspection,result)})
    return result


def _extract_candidates(inspection: PageInspection, config: DetectionConfig | None = None) -> CandidateSet:
    """Deterministic extraction; unsupported inputs return diagnostics, not demo geometry."""
    config = config or DetectionConfig()
    base = dict(document_id=inspection.document_id, page_number=inspection.page.page_number, config=config)
    diagnostics: list[Diagnostic] = []

    def note(code: str, message: str, paths=(), candidates=()) -> None:
        diagnostics.append(Diagnostic(code=code, message=message, path_ids=list(paths), candidate_ids=list(candidates)))

    if inspection.page.units != "pdf_point" or not inspection.paths:
        note("no_vector_geometry", "No supported vector geometry. Raster tracing and automatic raster candidates are unavailable.")
        return CandidateSet(**base, diagnostics=diagnostics)

    signature = sha256(config.model_dump_json().encode()).hexdigest()[:12]

    def identity(kind: str, shape, evidence: list[CommandEvidence]) -> str:
        payload = shape.normalize().wkb_hex + "|" + "|".join(e.model_dump_json() for e in _unique(evidence))
        token = sha256((signature + "geometry-baseline-0.2" + payload).encode()).hexdigest()[:16]
        return f"{inspection.document_id}:p{inspection.page.page_number}:{kind}:{token}"

    segments: list[Segment] = []
    curves: list[tuple[list[Point], CommandEvidence]] = []
    extent = box(0, 0, inspection.page.width, inspection.page.height)
    for path in inspection.paths:
        if "s" not in path.paint_type or path.stroke_opacity == 0:
            note("non_stroked_path", "Fill-only or transparent path excluded from wall inference.", [path.id])
            continue
        if path.dashes and path.dashes.split("]", 1)[0].lstrip("[").strip():
            note("dashed_path", "Dashed graphics excluded from wall inference.", [path.id])
            continue
        extracted = _path_segments(path)
        if extracted is None:
            note("invalid_source_path", "Non-simple or invalid closed source path excluded; no automatic polygon repair was attempted.", [path.id])
            continue
        curves.extend((c.points, _evidence(path, c)) for c in path.commands if c.operation == "cubic_bezier")
        if len(curves) + len(segments) > config.max_segments:
            note("complexity_limit", "Candidate source-primitive limit exceeded. No partial detections returned.")
            return CandidateSet(**base, diagnostics=diagnostics)
        if path.stroke_width is None or path.stroke_width < config.min_stroke_width:
            note("thin_stroke", "Stroke is below the wall-width threshold; retained as source evidence only.", [path.id])
            continue
        if not extracted:
            note("curved_wall_unsupported", "Curve-only path is not converted into a straight wall.", [path.id])
        for segment in extracted:
            if not extent.buffer(EPS).covers(segment.line):
                note("off_page_segment", "Segment extends beyond the visible page; excluded from candidate topology.", [path.id])
                continue
            segments.append(segment)
        if len(segments) + len(curves) > config.max_segments:
            note("complexity_limit", "Candidate segment limit exceeded. No partial detections returned.")
            return CandidateSet(**base, diagnostics=diagnostics)

    if not segments:
        note("no_supported_walls", "No strokes meet the wall heuristics. Adjust thresholds or review the source; no rooms were inferred.")
        return CandidateSet(**base, diagnostics=diagnostics)
    try:
        result = _reconstruct(segments, curves, inspection, config, base, diagnostics, identity, note)
        return _review_ambiguities(result, inspection)
    except GEOSException as exc:
        note("geometry_failure", f"Geometry reconstruction failed: {exc}. No partial detections returned.")
        return CandidateSet(**base, diagnostics=diagnostics)


def _review_ambiguities(result: CandidateSet, inspection: PageInspection) -> CandidateSet:
    """Keep inspectable detections, but prevent ambiguous graphics gaining SI review.

    An isolated single-path closed component is not structural evidence merely
    because its corners touch. Ignored thin/dashed strokes spanning a room can
    indicate a missed partition; do not cross-check the aggregate face as a room.
    """
    walls = result.list_candidates("wall")
    by_id = {w.id: w for w in walls}
    ambiguous = set()
    visited = set()
    for wall in walls:
        if wall.id in visited:
            continue
        pending, component = [wall.id], []
        while pending:
            identity = pending.pop()
            if identity in visited:
                continue
            visited.add(identity)
            current = by_id[identity]
            component.append(current)
            pending.extend(i for i in current.related_candidate_ids if i in by_id and i not in visited)
        if len(component) >= 3 and len({e.path_id for c in component for e in c.evidence}) == 1:
            faces, _, dangles, invalids = polygonize_full([c.geometry.as_shapely() for c in component])
            if not faces.is_empty and dangles.is_empty and invalids.is_empty:
                ambiguous.update(c.id for c in component)
    ignored = []
    review_limited = False
    for path in inspection.paths:
        dashed = path.dashes and path.dashes.split("]", 1)[0].lstrip("[").strip()
        thin = path.stroke_width is None or path.stroke_width < result.config.min_stroke_width
        if "s" in path.paint_type and path.stroke_opacity != 0 and (dashed or thin):
            ignored.extend(_path_segments(path) or [])
            if len(ignored) > result.config.max_segments:
                ignored=[]
                review_limited=True
                break
    ignored_tree = STRtree([s.line for s in ignored]) if ignored else None
    candidates, diagnostics = [], list(result.diagnostics)
    for candidate in result.candidates:
        flag = None
        review_paths = {e.path_id for e in candidate.evidence}
        if candidate.id in ambiguous:
            flag = "isolated_closed_graphic_ambiguous"
        elif candidate.candidate_type == "room":
            polygon = candidate.geometry.as_shapely()
            if review_limited:
                flag = "ignored_stroke_review_limit"
            for index in ignored_tree.query(polygon) if ignored_tree is not None else []:
                segment=ignored[int(index)]
                pieces = _line_parts(segment.line.intersection(polygon))
                if any(part.length >= result.config.min_room_span and
                       polygon.contains(part.interpolate(.5, normalized=True)) and
                       all(polygon.boundary.distance(SPoint(p)) <= EPS for p in part.coords)
                       for part in pieces):
                    flag = "ignored_stroke_possible_partition"
                    review_paths.add(segment.evidence.path_id)
                    break
        if flag:
            candidate = candidate.model_copy(update={"status":"unresolved", "flags":sorted(set(candidate.flags + [flag])),
                "explanation":candidate.explanation + " Structural interpretation requires review: " + flag + "."})
            diagnostics.append(Diagnostic(code=flag, message="Source geometry is retained, but ambiguous structural interpretation blocks measurement verification.",
                                          candidate_ids=[candidate.id], path_ids=sorted(review_paths)))
        candidates.append(candidate)
    reviewed=result.model_copy(update={"candidates":candidates, "diagnostics":diagnostics})
    from plantrace.source_audit import audit_source_lines
    audit=audit_source_lines(inspection,reviewed)
    suspect_ids={identity for line in audit.lines if line.interpretation=="possible_missed_wall" for identity in line.affected_room_ids}
    if audit.status=="review_limit_reached":
        suspect_ids.update(c.id for c in candidates if c.candidate_type=="room")
    reviewed_candidates=[]
    for candidate in candidates:
        if candidate.id in suspect_ids:
            candidate=candidate.model_copy(update={"status":"unresolved","flags":sorted(set(candidate.flags+["unexplained_source_wall_like_evidence"]))})
        reviewed_candidates.append(candidate)
    for line in audit.lines:
        if line.interpretation=="possible_missed_wall":
            diagnostics.append(Diagnostic(code="possible_missed_source_wall",message="Significant unexplained source stroke reaches supported walls and enters a room. It may be a partial partition; structural interpretation remains unresolved.",
                path_ids=[line.evidence.path_id],candidate_ids=line.affected_room_ids))
    return reviewed.model_copy(update={"candidates":reviewed_candidates,"diagnostics":diagnostics,"source_line_audit":audit})


def _reconstruct(segments, curves, inspection, config, base, diagnostics, identity, note) -> CandidateSet:
    raw_lines = [s.line for s in segments]
    lines = _straight_runs(raw_lines)
    if len(lines) > config.max_noded_segments:
        note("complexity_limit", "Noded geometry limit exceeded. No partial detections returned.")
        return CandidateSet(**base, diagnostics=diagnostics)
    source_tree = STRtree(raw_lines)
    line_tree = STRtree(lines)
    walls: list[Candidate] = []
    kept_lines: list[LineString] = []
    for line in lines:
        neighbors = [int(i) for i in line_tree.query(line) if not lines[int(i)].equals(line) and lines[int(i)].intersects(line)]
        if line.length < config.min_segment_length and not neighbors:
            note("short_isolated_segment", "Isolated segment is shorter than the minimum run length.")
            continue
        evidence = _unique([segments[int(i)].evidence for i in source_tree.query(line.buffer(EPS)) if _linear_overlap(line, raw_lines[int(i)]) > EPS])
        if not evidence:
            note("unresolved_wall_provenance", "Noded run could not be linked reliably to original commands; it was excluded.")
            continue
        flags = list(COMMON_FLAGS)
        if not neighbors:
            flags.append("isolated_stroke_ambiguous")
        walls.append(Candidate(
            id=identity("wall", line, evidence), candidate_type="wall", geometry=_geometry(line),
            evidence=evidence, extraction_method="stroked_lines_noded_collinear_runs_v1",
            status="provisional" if neighbors else "unresolved", flags=flags,
            explanation="Straight stroked run after overlap removal, intersection noding and collinear continuity merging. Stroke width is a heuristic, not a wall classification.",
        ))
        kept_lines.append(line)
    lines = kept_lines
    if not walls:
        note("no_supported_walls", "No supported wall runs remain after filtering.")
        return CandidateSet(**base, diagnostics=diagnostics)
    tree = STRtree(lines)
    for i, wall in enumerate(walls):
        connected = [walls[int(j)].id for j in tree.query(lines[i]) if int(j) != i and lines[i].intersects(lines[int(j)])]
        walls[i] = wall.model_copy(update={"related_candidate_ids": sorted(connected)})

    network = unary_union(lines)
    free = _free_ends(lines)
    bridges: list[BoundaryBridge] = []
    ambiguous_coordinates: set[tuple] = set()
    bridge_keys: set[str] = set()
    cosine = math.cos(math.radians(config.orientation_tolerance_degrees))
    for coordinate, wall_index in free:
        point = SPoint(coordinate)
        choices: dict[tuple, list[int]] = defaultdict(list)
        if config.endpoint_gap_tolerance <= 0:
            break
        for target in tree.query(point.buffer(config.endpoint_gap_tolerance)):
            j = int(target)
            if j == wall_index:
                continue
            nearest = lines[j].interpolate(lines[j].project(point))
            distance = point.distance(nearest)
            if not EPS < distance <= config.endpoint_gap_tolerance:
                continue
            q = tuple(nearest.coords[0])
            direction = ((q[0] - coordinate[0]) / distance, (q[1] - coordinate[1]) / distance)
            if q not in list(lines[j].coords) and not _facing(lines[wall_index], coordinate, direction, cosine):
                continue
            gap = LineString([coordinate, q])
            if _interior_clear(gap, network):
                choices[q].append(j)
        if len(choices) > 1:
            ambiguous_coordinates.add(coordinate)
            note("ambiguous_endpoint_gap", "Endpoint has multiple nearby targets; tolerance repair was not applied.", candidates=[walls[wall_index].id])
            continue
        if not choices:
            continue
        target_point, indices = next(iter(choices.items()))
        gap = LineString([coordinate, target_point])
        direction = ((target_point[0] - coordinate[0]) / gap.length, (target_point[1] - coordinate[1]) / gap.length)
        # Nearby parallel outlines are not endpoint continuations across their width.
        p0, p1 = list(lines[wall_index].coords)
        ax, ay = p1[0] - p0[0], p1[1] - p0[1]
        supported_indices = []
        for j in indices:
            q0, q1 = list(lines[j].coords)
            bx, by = q1[0] - q0[0], q1[1] - q0[1]
            parallel = abs(ax * by - ay * bx) / (lines[wall_index].length * lines[j].length) <= math.sin(math.radians(config.orientation_tolerance_degrees))
            if not parallel or (_facing(lines[wall_index], coordinate, direction, cosine) and
                                target_point in list(lines[j].coords) and
                                _facing(lines[j], target_point, (-direction[0], -direction[1]), cosine)):
                supported_indices.append(j)
        if not supported_indices:
            continue
        indices = supported_indices
        key = gap.normalize().wkb_hex
        if key in bridge_keys:
            continue
        evidence = _unique(walls[wall_index].evidence + [e for j in indices for e in walls[j].evidence])
        bridges.append(BoundaryBridge(
            id=identity("gap", gap, evidence), geometry=_geometry(gap), reason="endpoint_gap_tolerance",
            evidence=evidence, supporting_candidate_ids=[walls[wall_index].id] + [walls[j].id for j in indices],
        ))
        bridge_keys.add(key)

    bridges = [bridge for bridge in bridges if not any(tuple(point) in ambiguous_coordinates for point in bridge.geometry.as_shapely().coords)]

    # Larger gaps need facing collinear flanks. Only a compatible curve cue can
    # license a hypothetical boundary, and only when pairing is unambiguous.
    unresolved_free = [(p, i) for p, i in free if not any(b.geometry.as_shapely().distance(SPoint(p)) < EPS for b in bridges)]
    end_tree = STRtree([SPoint(p) for p, _ in unresolved_free]) if unresolved_free else None
    possible: list[tuple] = []
    pair_checks = 0
    for index, (p, i) in enumerate(unresolved_free):
        for other in end_tree.query(SPoint(p).buffer(config.max_opening_gap)):
            pair_checks += 1
            if pair_checks > 50000:
                note("complexity_limit", "Endpoint-pair search limit exceeded. No partial detections returned.")
                return CandidateSet(**base, diagnostics=diagnostics)
            j = int(other)
            if j <= index:
                continue
            q, k = unresolved_free[j]
            gap = LineString([p, q])
            length = gap.length
            if length < config.min_opening_gap or length <= config.endpoint_gap_tolerance or length > config.max_opening_gap:
                continue
            direction = ((q[0] - p[0]) / length, (q[1] - p[1]) / length)
            if not (_facing(lines[i], p, direction, cosine) and _facing(lines[k], q, (-direction[0], -direction[1]), cosine)):
                continue
            if not _interior_clear(gap, network):
                continue
            cues = [e for points, e in curves if _swing_cue(gap, points, config.endpoint_gap_tolerance)]
            possible.append((gap, i, k, cues, index, j))
    opening_usage: dict[int, int] = defaultdict(int)
    cue_usage: dict[str, int] = defaultdict(int)
    for _, _, _, cues, i, j in possible:
        opening_usage[i] += 1
        opening_usage[j] += 1
        for cue in cues:
            cue_usage[cue.model_dump_json()] += 1
    openings = []
    for gap, i, j, cues, end_i, end_j in possible:
        ambiguous = opening_usage[end_i] > 1 or opening_usage[end_j] > 1 or any(cue_usage[c.model_dump_json()] > 1 for c in cues)
        evidence = _unique(walls[i].evidence + walls[j].evidence + cues)
        supported = bool(cues) and not ambiguous
        opening = Candidate(
            id=identity("opening", gap, evidence), candidate_type="opening", geometry=_geometry(gap),
            evidence=evidence, extraction_method="facing_wall_gap_with_cubic_cue_v1" if cues else "facing_wall_gap_only_v1",
            status="unresolved", flags=COMMON_FLAGS + (["compatible_curve_cue"] if cues else ["no_symbol_support"]) + (["ambiguous_gap_pairing"] if ambiguous else []),
            related_candidate_ids=[walls[i].id, walls[j].id],
            explanation="Possible opening between facing wall ends. A compatible cubic swing-like cue is present; door/window semantics are unverified." if cues else "Aligned wall ends leave a gap. No compatible opening symbol was found; this could be incomplete linework and is not used to split rooms.",
        )
        openings.append(opening)
        if supported and config.use_opening_hypotheses:
            bridges.append(BoundaryBridge(
                id=identity("closure", gap, evidence), geometry=_geometry(gap), reason="opening_hypothesis",
                evidence=evidence, supporting_candidate_ids=[opening.id, walls[i].id, walls[j].id],
            ))

    topology = unary_union(lines + [bridge.geometry.as_shapely() for bridge in bridges])
    polygons, cuts, dangles, invalids = polygonize_full(topology)
    for code, leftover in [("cut_edges", cuts), ("dangling_boundaries", dangles), ("invalid_rings", invalids)]:
        if not leftover.is_empty:
            affected = [walls[int(i)].id for part in _line_parts(leftover) for i in tree.query(part.buffer(EPS)) if _linear_overlap(part, lines[int(i)]) > EPS]
            note(code, f"{len(leftover.geoms)} unresolved topology component(s); these lines do not close a valid room boundary.", candidates=sorted(set(affected)))
    rooms = []
    for polygon in sorted(polygons.geoms, key=lambda p: p.normalize().wkb_hex):
        if not polygon.is_valid:
            note("invalid_polygon", "Invalid reconstructed polygon rejected without repair.")
            continue
        rectangle = list(polygon.minimum_rotated_rectangle.exterior.coords)
        span = min(math.dist(a, b) for a, b in zip(rectangle, rectangle[1:]))
        boundary_walls = [walls[int(i)] for i in tree.query(polygon.boundary.buffer(EPS)) if _boundary_overlap(polygon.boundary, lines[int(i)]) > EPS]
        boundary_bridges = [b for b in bridges if _boundary_overlap(polygon.boundary, b.geometry.as_shapely()) > EPS]
        if polygon.area < config.min_room_area or span < config.min_room_span:
            note("small_or_narrow_enclosure", "Closed region fails the configured room-size/span heuristic; it may be a symbol, furniture or wall outline.", candidates=[w.id for w in boundary_walls])
            continue
        evidence = _unique([e for w in boundary_walls for e in w.evidence] + [e for b in boundary_bridges for e in b.evidence])
        if not evidence:
            note("unresolved_room_provenance", "Enclosed face has no reliable boundary evidence and was excluded.")
            continue
        flags = list(COMMON_FLAGS)
        if boundary_bridges:
            flags.append("hypothetical_boundary_closure")
        if polygon.interiors:
            flags.append("interior_holes_unresolved")
        # A face containing an unclosed interior partition can aggregate spaces.
        if any(polygon.contains(part.interpolate(0.5, normalized=True)) for part in _line_parts(dangles) + _line_parts(cuts)):
            flags.append("unclosed_interior_partition")
        if len({e.path_id for e in evidence}) == 1:
            flags.append("single_path_enclosure_ambiguous")
        rooms.append(Candidate(
            id=identity("room", polygon, evidence), candidate_type="room", geometry=_geometry(polygon),
            evidence=evidence, extraction_method="noded_wall_network_polygonize_full_v1",
            status="unresolved" if any(f in flags for f in ["interior_holes_unresolved", "unclosed_interior_partition", "single_path_enclosure_ambiguous"]) else "provisional",
            flags=flags, related_candidate_ids=[w.id for w in boundary_walls] + [c for b in boundary_bridges for c in b.supporting_candidate_ids if c not in {w.id for w in boundary_walls}],
            boundary_bridge_ids=[b.id for b in boundary_bridges],
            explanation="Enclosed face of the supported wall network passing drawing-unit size/span filters. Any missing-ink closures are separately recorded hypotheses. This is a room candidate, not a verified room.",
        ))
    if not rooms:
        note("no_room_boundaries", "No valid enclosed regions pass the room heuristics; unresolved wall runs remain visible.")
    return CandidateSet(**base, candidates=walls + rooms + openings, bridges=bridges, diagnostics=diagnostics)
