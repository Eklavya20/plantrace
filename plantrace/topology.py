"""Rebuild topology from current geometry; candidate relationship hints are not authority."""

import math
import json

from pydantic import ValidationError
from shapely import STRtree,__version__ as shapely_version,geos_version_string
from shapely.geometry import LineString, Point, mapping
from shapely.ops import nearest_points, unary_union

from plantrace.assembly_models import HostAssignment, TopologyIssue, TopologyRelationship, TopologySnapshot
from plantrace.calibration import digest
from plantrace.candidate_models import Candidate, CandidateSet

TOLERANCE = 1e-6  # Numeric geometry tolerance, not endpoint-gap repair or building units.


def geometry_version(candidate: Candidate) -> str:
    return digest(candidate.geometry.model_dump(mode="json"))


def effective_candidates(scene: CandidateSet) -> tuple[list[Candidate], set[str], list[TopologyIssue]]:
    issues=[]
    by_id={}
    duplicates=set()
    for candidate in scene.candidates:
        if candidate.id in by_id:
            duplicates.add(candidate.id)
        by_id[candidate.id]=candidate
    for identity in sorted(duplicates):
        issues.append(TopologyIssue(severity="error",code="duplicate_candidate_id",message="Candidate IDs must be unique.",candidate_ids=[identity]))
    superseded={c.supersedes_id for c in scene.candidates if c.supersedes_id}
    valid=[]
    for candidate in scene.candidates:
        try:
            Candidate.model_validate(candidate.model_dump())
        except (ValidationError,ValueError) as exc:
            issues.append(TopologyIssue(severity="error",code="invalid_candidate_geometry",message=str(exc),candidate_ids=[candidate.id]))
            continue
        if any(e.source.document_id!=scene.document_id or e.source.page_number!=scene.page_number for e in candidate.evidence):
            issues.append(TopologyIssue(severity="error",code="source_scope_mismatch",message="Candidate evidence belongs to another document/page.",candidate_ids=[candidate.id]))
            continue
        visited={candidate.id}
        parent=candidate.supersedes_id
        while parent is not None:
            if parent in visited or parent not in by_id:
                issues.append(TopologyIssue(severity="error",code="invalid_correction_lineage",message="Correction ancestry is missing or cyclic.",candidate_ids=[candidate.id]))
                break
            visited.add(parent)
            parent=by_id[parent].supersedes_id
        else:
            if candidate.id not in superseded and candidate.id not in duplicates:
                valid.append(candidate)
    return valid,superseded,issues


def _overlap(boundary, line, tolerance: float):
    # Intersect the source line with a tiny boundary buffer to handle oblique noding.
    supported=line.intersection(boundary.buffer(tolerance,cap_style=2,join_style=2))
    return supported if supported.length>tolerance*10 else None


def _flanking_support(opening, wall, cosine: float, tolerance: float):
    a,b=list(opening.coords)
    length=opening.length
    for endpoint,opposite in [(a,b),(b,a)]:
        if Point(endpoint).distance(wall)>tolerance:
            continue
        for index,wall_end in enumerate(wall.coords):
            if math.dist(endpoint,wall_end)>tolerance:
                continue
            other=list(wall.coords)[1-index]
            outward=(endpoint[0]-other[0],endpoint[1]-other[1])
            gap=(opposite[0]-endpoint[0],opposite[1]-endpoint[1])
            if (outward[0]*gap[0]+outward[1]*gap[1])/(wall.length*length)>=cosine:
                return Point(endpoint)
    return None


def build_topology(scene: CandidateSet, assignments: list[HostAssignment] | None=None,
                   tolerance: float=TOLERANCE, excluded_ids: set[str] | None=None) -> TopologySnapshot:
    if not math.isfinite(tolerance) or not 0<tolerance<=0.01:
        raise ValueError("Topology numerical tolerance must be positive and at most 0.01 source units.")
    assignments=assignments or []
    active,superseded,issues=effective_candidates(scene)
    excluded_ids=excluded_ids or set()
    active=[c for c in active if c.id not in excluded_ids]
    by_id={c.id:c for c in active}
    walls=[c for c in active if c.candidate_type=="wall"]
    rooms=[c for c in active if c.candidate_type=="room"]
    openings=[c for c in active if c.candidate_type=="opening"]
    wall_shapes=[c.geometry.as_shapely() for c in walls]
    wall_tree=STRtree(wall_shapes) if walls else None
    relationships=[]

    def issue(severity,code,message,ids):
        issues.append(TopologyIssue(severity=severity,code=code,message=message,candidate_ids=list(ids)))

    def relate(kind,first,second,shape,status="supported",support_kind="observed_geometry",bridge_ids=()):
        token=digest([kind,first,second,shape.normalize().wkb_hex,support_kind])
        relationships.append(TopologyRelationship(id=f"topology:{token}",kind=kind,from_candidate_id=first,to_candidate_id=second,
            status=status,support_kind=support_kind,support_geometry=json.loads(json.dumps(mapping(shape),allow_nan=False)),supporting_bridge_ids=list(bridge_ids)))

    for index,wall in enumerate(walls):
        line=wall_shapes[index]
        for neighbor in wall_tree.query(line.buffer(tolerance)):
            other=int(neighbor)
            if other<=index or line.distance(wall_shapes[other])>tolerance:
                continue
            contact=line.intersection(wall_shapes[other])
            if contact.is_empty:
                contact=nearest_points(line,wall_shapes[other])[0]
            if contact.length>tolerance*10:
                issue("error","overlapping_wall_runs","Distinct effective wall runs overlap; resolve duplicate/overlapping geometry before committing them.",[wall.id,walls[other].id])
            relate("wall_connectivity",wall.id,walls[other].id,contact,
                   "inferred" if wall.origin!="automatic" or walls[other].origin!="automatic" else "supported")

    usable_bridges=[]
    for bridge in scene.bridges:
        missing=[identity for identity in bridge.supporting_candidate_ids if identity not in by_id]
        if missing:
            issue("warning","stale_boundary_hypothesis","A hypothesis depends on superseded or absent candidates and is not used for current topology.",missing)
        else:
            usable_bridges.append(bridge)

    for room in rooms:
        polygon=room.geometry.as_shapely()
        boundary=polygon.boundary
        supports=[]
        if wall_tree is not None:
            for index in wall_tree.query(boundary.buffer(tolerance)):
                wall=walls[int(index)]
                segment=_overlap(boundary,wall_shapes[int(index)],tolerance)
                if segment is None:
                    continue
                supports.append(segment)
                relate("room_boundary",room.id,wall.id,segment,
                       "inferred" if wall.origin!="automatic" or room.origin!="automatic" else "supported")
        for bridge in usable_bridges:
            segment=_overlap(boundary,bridge.geometry.as_shapely(),tolerance)
            if segment is None:
                continue
            supports.append(segment)
            target=next((identity for identity in bridge.supporting_candidate_ids if by_id[identity].candidate_type=="opening"),None)
            # Endpoint-repair hypotheses without openings stay in boundary evidence;
            # relation targets use a real supporting wall, never an invented entity.
            target=target or next(identity for identity in bridge.supporting_candidate_ids if by_id[identity].candidate_type=="wall")
            relate("room_boundary_hypothesis",room.id,target,segment,"inferred",bridge.reason,[bridge.id])
        covered=unary_union(supports) if supports else LineString()
        if boundary.difference(covered.buffer(tolerance*2)).length>tolerance*20:
            issue("error","unsupported_room_boundary","Current effective walls and valid explicit hypotheses do not cover the complete room boundary. Rebuild/correct the polygon; stale geometry cannot be committed.",[room.id])
        if any(r.from_candidate_id==room.id and r.kind=="room_boundary_hypothesis" for r in relationships):
            issue("warning","inferred_room_boundary","Room uses an explicitly inferred boundary and remains unverified.",[room.id])

    room_shapes=[room.geometry.as_shapely() for room in rooms]
    room_tree=STRtree(room_shapes) if rooms else None
    for index,room in enumerate(rooms):
        polygon=room_shapes[index]
        for neighbor in room_tree.query(polygon.buffer(tolerance)):
            other=int(neighbor)
            if other<=index:
                continue
            intersection=polygon.intersection(room_shapes[other])
            if intersection.area>tolerance:
                issue("error","overlapping_rooms","Effective room polygons have overlapping interiors.",[room.id,rooms[other].id])
                continue
            shared=polygon.boundary.intersection(room_shapes[other].boundary)
            if shared.length<=tolerance*10:
                continue  # Point contact is not room adjacency.
            bridge_ids=[b.id for b in usable_bridges if _overlap(shared,b.geometry.as_shapely(),tolerance) is not None]
            invalid_room=any(issue.severity=="error" and {room.id,rooms[other].id}&set(issue.candidate_ids) for issue in issues)
            status="unresolved" if invalid_room else "inferred" if bridge_ids else "supported"
            relate("room_adjacency",room.id,rooms[other].id,shared,status,"shared_boundary_only",bridge_ids)

    cosine=math.cos(math.radians(scene.config.orientation_tolerance_degrees))
    for opening in openings:
        line=opening.geometry.as_shapely()
        eligible=[]
        if wall_tree is not None:
            for index in wall_tree.query(line.buffer(tolerance)):
                wall=walls[int(index)]
                wall_line=wall_shapes[int(index)]
                contact=_flanking_support(line,wall_line,cosine,tolerance)
                role="flanking_gap_support"
                if wall_line.buffer(tolerance).covers(line):
                    contact=line
                    role="coincident_run_support"
                if contact is not None:
                    relate("opening_host",opening.id,wall.id,contact,"unresolved",role)
                    eligible.append(wall.id)
        issue("warning","unresolved_opening","Host support does not establish door/window semantics or a continuous host across a gap. The opening remains unresolved.",[opening.id])
        if not eligible:
            issue("warning","unhosted_opening","No supported current host/flanking wall was found.",[opening.id])

    for assignment in assignments:
        opening=by_id.get(assignment.opening_id)
        wall=by_id.get(assignment.wall_id)
        match=next((r for r in relationships if r.kind=="opening_host" and r.from_candidate_id==assignment.opening_id and r.to_candidate_id==assignment.wall_id),None)
        if opening is None or wall is None or match is None or geometry_version(opening)!=assignment.opening_geometry_version or geometry_version(wall)!=assignment.wall_geometry_version:
            issue("warning","stale_host_assignment","Stored manual host assignment no longer has matching geometry support and is not applied.",[assignment.opening_id,assignment.wall_id])
            continue
        relationships[relationships.index(match)]=match.model_copy(update={"assignment_origin":"manual_supported_assignment","assignment_id":assignment.id})

    relationships.sort(key=lambda r:(r.kind,r.from_candidate_id,r.to_candidate_id,r.id))
    return TopologySnapshot(version=digest({"scene":scene.model_dump(mode="json"),"assignments":[a.model_dump(mode="json") for a in assignments],
        "excluded":sorted(excluded_ids),"tolerance":tolerance,"method":"topology_v1","engine":[shapely_version,geos_version_string],
        "relationships":[r.model_dump(mode="json") for r in relationships],"issues":[i.model_dump(mode="json") for i in issues]}),
        document_id=scene.document_id,page_number=scene.page_number,tolerance=tolerance,effective_candidate_ids=sorted(by_id),
        superseded_candidate_ids=sorted(superseded),excluded_candidate_ids=sorted(excluded_ids),relationships=relationships,issues=issues)


def validate_topology(scene: CandidateSet, topology: TopologySnapshot, assignments: list[HostAssignment] | None=None,
                      excluded_ids: set[str] | None=None) -> list[TopologyIssue]:
    actual=build_topology(scene,assignments,topology.tolerance,excluded_ids)
    issues=list(actual.issues)
    if topology.version!=actual.version or (topology.document_id,topology.page_number)!=(scene.document_id,scene.page_number):
        issues.append(TopologyIssue(severity="error",code="stale_topology",message="Topology does not match the current source geometry/assignments."))
    expected={r.id:r.model_dump(mode="json") for r in actual.relationships}
    supplied={r.id:r.model_dump(mode="json") for r in topology.relationships}
    if len(supplied)!=len(topology.relationships) or supplied!=expected:
        issues.append(TopologyIssue(severity="error",code="inconsistent_relationships",message="Relationship endpoints, support geometry or statuses differ from actual geometry."))
    if topology.effective_candidate_ids!=actual.effective_candidate_ids or topology.superseded_candidate_ids!=actual.superseded_candidate_ids:
        issues.append(TopologyIssue(severity="error",code="invalid_candidate_membership",message="Topology includes superseded, invalid or absent candidates."))
    return issues
