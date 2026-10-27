"""A reproducible room correction workflow: extract -> edit -> rebuild -> restore.

python -m scripts.demonstrate_room_rebuild --output support/artifacts/room-rebuild/room-case
Uses actual existing PDF extraction and explicit synthetic calibration assumptions.
No candidate geometry or measurement result is injected into the application.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from shapely.ops import unary_union

from plantrace.assembly import PlanTraceService
from plantrace.assembly_models import CorrectionContext
from plantrace.documents import render_page
from plantrace.measurements import measure_candidates
from plantrace.overlays import render_overlay
from plantrace.repository import ReviewStore
from plantrace.room_rebuild import rebuild_room
from plantrace.source_audit import audit_source_lines
from plantrace.topology import effective_candidates
from scripts.evaluate_stage4 import run as existing_workflow


def run(output: Path):
    # Reuse the existing actual-extraction, correction and persistence workflow.
    before=existing_workflow(output)
    store=ReviewStore(output/"review.sqlite3")
    api=PlanTraceService(store,before["source_sha256"],1)
    context=api.context()
    state=context.workspace
    stale_rooms={m.candidate_id for m in state.measurements.measurements if m.candidate_type=="room" and m.boundary_support=="stale"}
    if len(stale_rooms)!=1:
        raise ValueError("Expected one demonstrably stale room from the preceding workflow; no fallback room selected.")
    original_id=next(iter(stale_rooms))
    rebuilt=rebuild_room(state.candidates,original_id,state.inspection.page,"Synthetic technical demonstration",
                         "Explicitly recover the closed face after correcting an extracted wall")
    scene=state.candidates.model_copy(update={"candidates":state.candidates.candidates+[rebuilt]})
    scene=scene.model_copy(update={"source_line_audit":audit_source_lines(state.inspection,scene)})
    revision=state.geometry_revision+1
    measurements=measure_candidates(scene,state.calibration,revision,state.measurement_approvals)
    correction_contexts=[]
    for previous in state.corrections:
        items=previous.candidates+[rebuilt] if previous.config==scene.config else previous.candidates
        correction_contexts.append(CorrectionContext(config=previous.config,candidates=items))
    changed=state.model_copy(update={"candidates":scene,"geometry_revision":revision,"measurements":measurements,"corrections":correction_contexts})
    store.save_workspace(changed,expected_revision=context.stored.revision,action="room_rebuilt",
                         actor="Synthetic technical demonstration",note=rebuilt.reconstruction_basis.note)
    model=api.commit_entities([rebuilt.id],"Synthetic technical demonstration","Recover a current provisional room, preserving uncertainty")
    exported=api.export_model(model.version)
    (output/"model_v4_room_rebuild.json").write_text(json.dumps(exported,indent=2),encoding="utf-8")
    current=next(e.measurement for e in model.entities if e.candidate_id==rebuilt.id)
    first=api.export_model(1)["model"]
    initial=next(m for m in first["source_review"]["measurements"]["measurements"] if m["candidate_id"]==original_id)
    # Boundary provenance, not an architectural classification or uncertainty bound.
    active,_,_=effective_candidates(scene)
    boundary=rebuilt.geometry.as_shapely().boundary
    def overlap(kind):
        shapes=[c.geometry.as_shapely() for c in active if c.candidate_type=="wall" and c.origin==kind]
        return boundary.intersection(unary_union(shapes)).length
    hypotheses=boundary.intersection(unary_union([b.geometry.as_shapely() for b in scene.bridges if b.id in rebuilt.boundary_bridge_ids])).length
    reopened=PlanTraceService(ReviewStore(output/"review.sqlite3"),before["source_sha256"],1)
    restored=reopened.export_model(model.version)
    assert restored==exported
    source,_=store.load_source(before["source_sha256"])
    preview=render_page(source,"synthetic_plan.pdf",max_edge=1000)
    from plantrace.candidate_models import CandidateSet
    first_scene=CandidateSet.model_validate(first["source_review"]["candidates"])
    for filename,candidates,selection in [("room_before.png",first_scene,original_id),("room_stale.png",state.candidates,original_id),("room_rebuilt.png",scene,rebuilt.id)]:
        (output/filename).write_bytes(render_overlay(preview,candidates,selection))
    result={"source_sha256":before["source_sha256"],"source_kind":"existing generated synthetic PDF; not a real-plan validation",
        "calibration_basis":before["calibration_assumptions"],"original_room_id":original_id,"rebuilt_room_id":rebuilt.id,
        "before_m2":initial["value_si"],"after_edit_before_rebuild_m2":next(m.value_si for m in state.measurements.measurements if m.candidate_id==original_id),
        "after_rebuild_m2":current.value_si,"change_m2":current.value_si-initial["value_si"],
        "room_bounds_before":list(first_scene.select_candidate(original_id).geometry.as_shapely().bounds),
        "room_bounds_after":list(rebuilt.geometry.as_shapely().bounds),
        "boundary_drawing_units":{"perimeter":boundary.length,"automatic_source_stroke_runs":overlap("automatic"),"manual_wall_geometry":overlap("manual_correction"),"explicit_hypothesis_bridges":hypotheses},
        "measurement_state":current.verification_state,"boundary_support":current.boundary_support,"inferred_boundaries":current.has_inferred_boundaries,
        "structure_completeness":current.structure_completeness,"reconstruction_basis":rebuilt.reconstruction_basis.model_dump(mode="json"),
        "active_entities":len(model.entities),"retired_snapshots":len(model.retired_entities),"relationships":len(model.relationships),
        "model_versions":store.list_versions(before["source_sha256"],1),"restart_export_matches":True,
        "engineering_handoff_eligible":exported["engineering_handoff_gate"]["eligible"]}
    project=Path(__file__).resolve().parents[1]
    result["implementation_sha256"]={name:sha256((project/name).read_bytes()).hexdigest() for name in
        ("plantrace/room_rebuild.py","plantrace/candidate_models.py","plantrace/measurements.py","plantrace/review_ui.py","scripts/demonstrate_room_rebuild.py")}
    (output/"room_case.json").write_text(json.dumps(result,indent=2,allow_nan=False),encoding="utf-8")
    return result


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=Path("support/artifacts/room-rebuild")/f"room-{uuid4().hex[:8]}")
    print(json.dumps(run(parser.parse_args().output),indent=2))
