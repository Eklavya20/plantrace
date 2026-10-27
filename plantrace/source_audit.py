"""Compare current wall runs with source ink, without inventing absent structure."""
from shapely.geometry import Point,box
from shapely.ops import unary_union,polygonize_full

from plantrace.candidate_models import SourceLineAudit,SourceLineReview
from plantrace.calibration import digest
from plantrace.topology import effective_candidates


def audit_source_lines(inspection,scene):
    from plantrace.candidates import _path_segments,_geometry
    if (inspection.document_id,inspection.page.page_number)!=(scene.document_id,scene.page_number):
        raise ValueError("Source-line audit document/page mismatch.")
    active,_,_=effective_candidates(scene)
    walls=[c for c in active if c.candidate_type=="wall"]
    rooms=[c for c in active if c.candidate_type=="room"]
    network=unary_union([c.geometry.as_shapely() for c in walls])
    buffered=network.buffer(1e-6)
    threshold=scene.config.min_room_span
    lines=[]
    curves=0
    invalid_paths=0
    limited=False
    extent=box(0,0,inspection.page.width,inspection.page.height).buffer(1e-6)
    for path in inspection.paths:
        if "s" not in path.paint_type or path.stroke_opacity==0:
            continue
        curves+=sum(c.operation=="cubic_bezier" for c in path.commands)
        segments=_path_segments(path)
        if segments is None:
            invalid_paths+=1
            continue
        if len(lines)+len(segments)>scene.config.max_segments:
            limited=True
            break
        faces,cuts,dangles,invalids=polygonize_full([s.line for s in segments])
        closed_graphic=not faces.is_empty and cuts.is_empty and dangles.is_empty and invalids.is_empty
        for segment in segments:
            if not extent.covers(segment.line):
                continue
            unexplained=segment.line.difference(buffered).length
            affected=[]
            if unexplained<=1e-5:
                interpretation="represented"
            elif closed_graphic or segment.line.length<threshold:
                interpretation="small_or_closed_graphic_ambiguous"
            else:
                # A partial line reaching supported boundaries can be a missed
                # partition even when it does not span the entire polygon.
                near_boundary=any(network.distance(Point(p))<=scene.config.endpoint_gap_tolerance+1e-6 for p in segment.line.coords)
                affected=[r.id for r in rooms if segment.line.intersection(r.geometry.as_shapely().buffer(-1e-6)).length>1e-5] if near_boundary else []
                interpretation="possible_missed_wall" if affected else "unexplained_linework"
            lines.append(SourceLineReview(evidence=segment.evidence,geometry=_geometry(segment.line),stroke_width=path.stroke_width,
                dashed=bool(path.dashes and path.dashes.split("]",1)[0].lstrip("[").strip()),
                covered_length=max(0,segment.line.length-unexplained),unexplained_length=unexplained,
                interpretation=interpretation,affected_room_ids=affected,
                explanation="Straight-stroke coverage in drawing units only. Closed/small graphics may be furniture or annotations; architectural class and wholly absent structure cannot be inferred."))
    return SourceLineAudit(document_id=scene.document_id,page_number=scene.page_number,
        wall_geometry_version=digest([{ "id":c.id,"geometry":c.geometry.model_dump(mode="json")} for c in walls]),
        significant_length_threshold=threshold,status="source_geometry_unavailable" if not inspection.paths else "review_limit_reached" if limited else "assessed_supported_straight_strokes",
        lines=lines,unassessed_curve_commands=curves,unassessed_invalid_paths=invalid_paths)
