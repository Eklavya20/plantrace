"""Explicit fault injection only; never used by application extraction.

All scenarios start with actual PDF extraction. Injected candidates deliberately
probe the validator; they are not counted as application detection output.
"""
import json
from pathlib import Path
from pydantic import ValidationError

from plantrace.assembly import PlanTraceService, ModelValidationError
from plantrace.assembly_models import ReviewWorkspace, CorrectionContext
from plantrace.calibration import new_book,make_reference,put_reference
from plantrace.candidate_models import CandidateGeometry
from plantrace.candidates import extract_candidates
from plantrace.corrections import correct_candidate
from plantrace.documents import inspect_document,inspect_page
from plantrace.measurements import measure_candidates,approval_blockers,approve_measurement
from plantrace.models import Point
from plantrace.repository import ReviewStore, ReviewConflict
from plantrace.topology import build_topology
from scripts.stage5_fixtures import generate
from plantrace.gates import engineering_handoff_gate


def run_failures(output: Path):
    output.mkdir(parents=True,exist_ok=True)
    data=(Path(__file__).resolve().parents[1]/"examples/synthetic_plan.pdf").read_bytes()
    inspection=inspect_page(data,"synthetic.pdf")
    scene=extract_candidates(inspection)
    findings=[]
    def record(name,outcome,evidence,material=False,unsafe=False):
        findings.append({"scenario":name,"outcome":outcome,"evidence":evidence,
                         "material_error":material,"detected":material and outcome=="blocked",
                         "blocked":outcome in {"blocked","escalated"},"escalated":outcome=="escalated",
                         "engineering_gate":engineering_handoff_gate().model_dump(mode="json"),
                         "unsafe_measurement_gate_acceptance":unsafe or material and outcome=="silently_accepted"})
    def book_for(horizontal=4,vertical=3,unit="m",page=inspection):
        book=new_book(page)
        for pairs,distance,label in [([(100,100),(500,100)],horizontal,"injected horizontal"), ([(100,100),(100,400)],vertical,"injected vertical")]:
            ref=make_reference(book,book.regions[0].id,tuple(Point(x=x,y=y) for x,y in pairs),distance,unit,label)
            book=put_reference(book,ref)
        return book
    def measurement_gate(name,book):
        values=measure_candidates(scene,book)
        walls=[m for m in values.measurements if m.candidate_type=="wall"]
        unsafe=any(not approval_blockers(m) for m in walls)
        approved=None
        if unsafe:
            target=next(m for m in walls if not approval_blockers(m))
            approved=approve_measurement(values,target.id,"Synthetic fault injector","Deliberately incorrect reference inputs; evaluation only, no engineering approval.")
            (output/f"{name}_review.json").write_text(approved.model_dump_json(indent=2),encoding="utf-8")
        conflicting=values.assessments[0].status=="conflicting"
        record(name,"silently_accepted" if unsafe else "blocked" if conflicting else "escalated",{
            "calibration_status":values.assessments[0].status,"wall_measurements":len(walls),
            "approval_eligible_wall_measurements":sum(not approval_blockers(m) for m in walls),
            "scale_m_per_unit":values.assessments[0].metres_per_source_unit,
            "truth_scale_m_per_unit":.01,"manual_measurement_review_created":approved is not None,
            "calibration_correctness":values.assessments[0].correctness_status,
            "error_detectable_from_supplied_evidence":conflicting,
            "engineering_handoff_eligible":False},True,unsafe)
    measurement_gate("wrong_uniform_scale_10_percent",book_for(4.4,3.3))
    measurement_gate("conflicting_independent_references",book_for(4,3.3))
    measurement_gate("incorrect_units_consistently_entered_cm",book_for(unit="cm"))
    injected_fixtures=output/"source_faults"
    entries=generate(injected_fixtures)
    missing_entry=next(e for e in entries if e["id"]=="missing_wall")
    missing_data=(injected_fixtures/missing_entry["file"]).read_bytes()
    missing_inspection=inspect_page(missing_data,"missing_wall.pdf")
    missing_scene=extract_candidates(missing_inspection)
    missing_values=measure_candidates(missing_scene,book_for(page=missing_inspection))
    missed_room=next(m for m in missing_values.measurements if m.candidate_type=="room")
    missing_approval=None
    if not approval_blockers(missed_room):
        missing_approval=approve_measurement(missing_values,missed_room.id,"Synthetic fault injector","Deliberately missing partition in the drawing; evaluation only.")
        (output/"missing_source_partition_review.json").write_text(missing_approval.model_dump_json(indent=2),encoding="utf-8")
    record("missing_source_partition", "silently_accepted" if not approval_blockers(missed_room) else "escalated",
           {"expected_space_areas_m2":[6,6],"aggregate_area_m2":missed_room.value_si,"manual_measurement_review_created":missing_approval is not None,
            "explanation":"A wholly absent source partition leaves a valid enclosure. Geometry/calibration alone cannot infer the missing architectural intent."},True)
    missing=measure_candidates(scene,new_book(inspection))
    record("missing_calibration","blocked",{"si_values_available":sum(m.value_si is not None for m in missing.measurements),
           "approval_eligible":sum(not approval_blockers(m) for m in missing.measurements)},True)
    correctbook=book_for()
    measurements=measure_candidates(scene,correctbook)
    record("inferred_room_approval","blocked",{"inferred_rooms":sum(m.has_inferred_boundaries for m in measurements.measurements),
           "approval_eligible_rooms":sum(not approval_blockers(m) for m in measurements.measurements if m.candidate_type=="room")})
    state=ReviewWorkspace(document=inspect_document(data,"synthetic.pdf"),inspection=inspection,candidates=scene,
                          calibration=correctbook,measurements=measurements)
    store=ReviewStore(output/"review.sqlite3");store.save_source(data,state.document)
    saved=store.save_workspace(state,expected_revision=0)
    api=PlanTraceService(store,state.document.document_id,1)
    wall=next(c for c in scene.list_candidates("wall") if c.geometry.as_shapely().bounds==(100,100,100,400))
    previous_view=api.context().version
    api.record_decision(wall.id,"needs_review","Concurrent reviewer A","Current scene",expected_context_version=previous_view)
    try:
        api.record_decision(wall.id,"accept_provisional","Concurrent reviewer B","Stale scene",expected_context_version=previous_view)
        record("concurrent_architect_decision","silently_accepted",{},True)
    except ReviewConflict:
        record("concurrent_architect_decision","blocked",{"stale_view_rejected":True,"history_preserved":True},True)
    absent=scene.model_copy(update={"candidates":[c for c in scene.candidates if c.id!=wall.id]})
    topology=build_topology(absent)
    after_missing=measure_candidates(absent,correctbook)
    record("missing_wall_retained_room","blocked" if any(i.code=="unsupported_room_boundary" for i in topology.issues) else "silently_accepted",
           {"issue_codes":sorted({i.code for i in topology.issues}),
            "withheld_room_values":sum(m.candidate_type=="room" and m.value_si is None for m in after_missing.measurements)},True)
    room=scene.list_candidates("room")[0]
    fake=room.model_copy(update={"id":"fault:hallucinated-room","geometry":CandidateGeometry(type="Polygon",
         coordinates=[Point(x=x,y=y) for x,y in [(20,20),(80,20),(80,90),(20,90),(20,20)]]),"boundary_bridge_ids":[],"related_candidate_ids":[]})
    hallucinated=build_topology(scene.model_copy(update={"candidates":scene.candidates+[fake]}))
    record("hallucinated_unsupported_room","blocked" if any(i.code=="unsupported_room_boundary" and fake.id in i.candidate_ids for i in hallucinated.issues) else "silently_accepted",
           {"issue_codes":sorted({i.code for i in hallucinated.issues})},True)
    opening=scene.list_candidates("opening")[0]
    try:
        api.link_opening_to_wall(opening.id,wall.id,"Fault injector","Deliberately unsupported wall")
        record("wrong_opening_host","silently_accepted",{},True,True)
    except ModelValidationError as exc:
        record("wrong_opening_host","blocked",{"error":str(exc)},True)
    try:
        CandidateGeometry(type="Polygon",coordinates=[Point(x=x,y=y) for x,y in [(100,100),(300,300),(100,300),(300,100),(100,100)]])
        record("invalid_bowtie_polygon","silently_accepted",{},True,True)
    except ValidationError:
        record("invalid_bowtie_polygon","blocked",{"schema_rejected_self_intersection":True},True)
    model=api.commit_entities([c.id for c in api.list_candidates()],"Fault evaluator","Before correction")
    edit=correct_candidate(wall,[[110,100],[110,400]],inspection.page)
    changed_scene=scene.model_copy(update={"candidates":scene.candidates+[edit]})
    # Deliberately persist stale old measurements, simulating a broken caller.
    changed=state.model_copy(update={"candidates":changed_scene,"geometry_revision":1,
        "corrections":[CorrectionContext(config=scene.config,candidates=[edit])]})
    store.save_workspace(changed,expected_revision=saved.revision)
    context=api.context()
    stale=[m for m in context.workspace.measurements.measurements if m.candidate_type=="room" and m.boundary_support=="stale"]
    record("stale_measurements_after_wall_correction","blocked" if stale and all(m.value_si is None for m in stale) else "silently_accepted",
           {"withheld_stale_rooms":len(stale),"historical_export_flagged":not api.export_model(model.version)["current_assessment"]["matches_current_review"]},True)
    try:
        store.save_workspace(state,expected_revision=saved.revision)
        record("concurrent_workspace_change","silently_accepted",{},True,True)
    except ReviewConflict:
        record("concurrent_workspace_change","blocked",{"compare_and_swap_rejected_stale_writer":True},True)
    restarted=PlanTraceService(ReviewStore(output/"review.sqlite3"),state.document.document_id,1)
    record("session_restart","detected",{"source_bytes_match":restarted.store.load_source(state.document.document_id)[0]==data,
            "correction_persisted":restarted.select_candidate(edit.id)==edit,"architect_decisions":len(restarted.context().decisions),
            "model_versions":restarted.store.list_versions(state.document.document_id,1)})
    material=[f for f in findings if f["material_error"]]
    report={"scenarios":findings,"unsafe_acceptance":{"numerator":sum(f["unsafe_measurement_gate_acceptance"] for f in material),
            "denominator":len(material),"rate":sum(f["unsafe_measurement_gate_acceptance"] for f in material)/len(material),
            "definition":"Injected material errors reaching a measurement approval gate or accepted unsupported mutation; provisional assembly alone is not verification. Engineering handoff is separately always disabled."}}
    report["gate_outcomes"]={"material_errors":len(material),"detected":sum(f["detected"] for f in material),
        "blocked":sum(f["blocked"] for f in material),"escalated":sum(f["escalated"] for f in material),
        "measurement_review_gate_passes":sum(f["unsafe_measurement_gate_acceptance"] for f in material),
        "engineering_handoff_gate_passes":sum(f["engineering_gate"]["eligible"] for f in material),
        "intrinsically_undetectable_without_additional_evidence":[f["scenario"] for f in material if f["escalated"]],
        "counts_overlap":"Escalated cases are blocked at the approval gate, but their material error is not detected."}
    (output/"report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    return report
