"""Conservative, explicit room recovery after a wall correction.

No wall is invented, snapped or extended here. Polygonization uses only current
wall geometry and still-valid, already recorded hypothesis bridges. Ambiguous
merges/splits and open networks require manual review instead of a guessed face.
"""
import math

from shapely.geometry import box
from shapely.ops import polygonize_full,unary_union

from plantrace.calibration import digest
from plantrace.candidate_models import Candidate,CandidateSet,RoomReconstructionBasis
from plantrace.topology import effective_candidates,geometry_version

EPS=1e-6
METHOD="current_wall_faces_v1"


def _support(scene):
    active,_,issues=effective_candidates(scene)
    if any(i.severity=="error" for i in issues):
        raise ValueError("Resolve invalid/duplicate/source-mismatched candidates before rebuilding a room.")
    active_ids={c.id for c in active}
    walls=[c for c in active if c.candidate_type=="wall"]
    # A bridge tied to a superseded wall/opening cannot close the new network.
    bridges=[b for b in scene.bridges if set(b.supporting_candidate_ids)<=active_ids]
    wall_versions={c.id:geometry_version(c) for c in walls}
    bridge_versions={b.id:digest(b.model_dump(mode="json")) for b in bridges}
    version=digest({"walls":wall_versions,"bridges":bridge_versions,"config":scene.config.model_dump(mode="json"),"method":METHOD})
    return active,walls,bridges,wall_versions,bridge_versions,version


def reconstruction_is_current(candidate,scene):
    basis=candidate.reconstruction_basis
    if basis is None or candidate.candidate_type!="room" or candidate.origin!="manual_correction" or candidate.extraction_method!=METHOD:
        return False
    try:
        original=scene.select_candidate(basis.original_room_id)
        _,_,_,wall_versions,bridge_versions,version=_support(scene)
    except (ValueError,KeyError):
        return False
    return (candidate.supersedes_id==original.id and original.candidate_type=="room" and
            geometry_version(original)==basis.original_geometry_version and
            basis.wall_geometry_versions==wall_versions and basis.bridge_geometry_versions==bridge_versions and basis.network_version==version)


def rebuild_room(scene: CandidateSet,original_room_id: str,page,reviewer: str,note: str) -> Candidate:
    from plantrace.candidates import _geometry,_unique,_boundary_overlap,_line_parts
    if not reviewer.strip() or not note.strip():
        raise ValueError("A reviewer and a reason are required; rebuilding records a manual intervention, not approval.")
    original=scene.select_candidate(original_room_id)
    if original.candidate_type!="room" or original.origin!="automatic":
        raise ValueError("Select the original automatic room candidate to rebuild it from current walls.")
    if page.units!="pdf_point" or page.page_number!=scene.page_number:
        raise ValueError("Room reconstruction requires its matching PDF source page.")
    active,walls,bridges,wall_versions,bridge_versions,network_version=_support(scene)
    from plantrace.topology import build_topology
    if any(i.severity=="error" and i.code=="overlapping_wall_runs" for i in build_topology(scene).issues):
        raise ValueError("Resolve overlapping current wall runs before reconstructing a room.")
    if not walls or len(walls)+len(bridges)>scene.config.max_segments:
        raise ValueError("No usable wall network, or reconstruction exceeds the configured segment budget.")
    network=unary_union([c.geometry.as_shapely() for c in walls]+[b.geometry.as_shapely() for b in bridges])
    if len(_line_parts(network))>scene.config.max_noded_segments:
        raise ValueError("Reconstruction exceeds the configured noded-segment budget; no partial room returned.")
    faces,_,_,invalids=polygonize_full(network)
    if not invalids.is_empty:
        raise ValueError("Current walls contain invalid rings; no automatic geometry repair was attempted.")
    old_shape=original.geometry.as_shapely()
    seed=old_shape.representative_point()
    # Exclude the selected room's current correction when checking other rooms.
    def parent(candidate):
        seen=set()
        while candidate.supersedes_id and candidate.id not in seen:
            seen.add(candidate.id)
            candidate=scene.select_candidate(candidate.supersedes_id)
        return candidate.id
    other_rooms=[c.geometry.as_shapely() for c in active if c.candidate_type=="room" and parent(c)!=original.id]
    matches=[face for face in faces.geoms if face.covers(seed)]
    if len(matches)!=1:
        raise ValueError("No unique closed face contains the original room's interior point. Open boundaries or a split remain unresolved.")
    polygon=matches[0]
    # A seed alone would silently choose one side of an off-centre split.
    # Any other face occupying the old room must already map unambiguously to
    # another room's interior point (for example a moved shared boundary).
    for face in faces.geoms:
        if face.equals(polygon) or face.intersection(old_shape).area<=EPS:
            continue
        if sum(face.contains(other.representative_point()) for other in other_rooms)!=1:
            raise ValueError("New faces split the original room without an unambiguous existing room identity. Review the split manually.")
    if any(polygon.intersection(other).area>EPS for other in other_rooms):
        raise ValueError("The reconstructed face overlaps another room. A possible merge/split needs manual review; no room was guessed.")
    if not polygon.is_valid or not box(0,0,page.width,page.height).covers(polygon):
        raise ValueError("Reconstructed geometry is invalid or outside the source page.")
    corners=list(polygon.minimum_rotated_rectangle.exterior.coords)
    span=min(math.dist(a,b) for a,b in zip(corners,corners[1:]))
    if polygon.area<scene.config.min_room_area or span<scene.config.min_room_span:
        raise ValueError("The reconstructed face fails the configured room size/span heuristics.")
    if polygon.equals(old_shape):
        raise ValueError("The original room already matches the current closed face; no changed room was reconstructed.")
    boundary=polygon.boundary
    supports=[w for w in walls if _boundary_overlap(boundary,w.geometry.as_shapely())>EPS]
    closures=[b for b in bridges if _boundary_overlap(boundary,b.geometry.as_shapely())>EPS]
    evidence=_unique([e for w in supports for e in w.evidence]+[e for b in closures for e in b.evidence])
    if not evidence:
        raise ValueError("The reconstructed face has no retained source evidence.")
    basis=RoomReconstructionBasis(original_room_id=original.id,original_geometry_version=geometry_version(original),
        wall_geometry_versions=wall_versions,bridge_geometry_versions=bridge_versions,network_version=network_version,
        reviewer=reviewer.strip(),note=note.strip())
    geometry=_geometry(polygon)
    identity=digest({"original":original.id,"geometry":geometry.model_dump(mode="json"),"basis":basis.model_dump(mode="json")})[:20]
    flags=["manual_geometry_unverified","room_reconstructed_from_current_walls","structural_completeness_not_established"]
    if closures:flags.append("hypothetical_boundary_closure")
    if polygon.interiors:flags.append("interior_holes_unresolved")
    return Candidate(id=f"{original.id}:rebuilt:{identity}",candidate_type="room",geometry=geometry,evidence=evidence,
        extraction_method=METHOD,status="unresolved",origin="manual_correction",supersedes_id=original.id,
        explanation="Explicitly rebuilt closed face of current wall geometry and existing valid hypothesis bridges. Source evidence and wall/bridge versions are retained. Manual wall geometry and inferred closures remain unverified; no engineering approval or completeness claim is made.",
        flags=flags,related_candidate_ids=sorted({w.id for w in supports}|{identity for b in closures for identity in b.supporting_candidate_ids}),
        boundary_bridge_ids=[b.id for b in closures],reconstruction_basis=basis)
