from collections import Counter
import json

import pytest
from pydantic import ValidationError

from plantrace.assembly import PlanTraceService, ModelValidationError
from plantrace.assembly_models import BuildingModel, ReviewWorkspace
from plantrace.calibration import make_reference,new_book,put_reference
from plantrace.candidates import extract_candidates
from plantrace.corrections import correct_candidate
from plantrace.documents import inspect_document,inspect_page
from plantrace.measurements import measure_candidates,approve_measurement
from plantrace.models import Point
from plantrace.repository import ReviewStore,ReviewConflict,PersistenceError
from plantrace.topology import build_topology,validate_topology
from plantrace.model_validation import validate_assembled_model
from support_provenance import with_physical_declarations


def workspace(data):
    inspection=inspect_page(data,"synthetic.pdf")
    candidates=extract_candidates(inspection)
    book=new_book(inspection)
    for points,distance,label in [([(100,100),(500,100)],4,"horizontal known"), ([(100,100),(100,400)],3,"vertical known")]:
        ref=make_reference(book,book.regions[0].id,tuple(Point(x=x,y=y) for x,y in points),distance,"m",label)
        book=put_reference(book,ref)
    book=with_physical_declarations(book)
    return ReviewWorkspace(document=inspect_document(data,"synthetic.pdf"),inspection=inspection,candidates=candidates,
        calibration=book,measurements=measure_candidates(candidates,book))


def service(tmp_path,data):
    state=workspace(data)
    store=ReviewStore(tmp_path/"review.sqlite3")
    store.save_source(data,state.document)
    store.save_workspace(state,expected_revision=0)
    return PlanTraceService(store,state.document.document_id,1),state


def test_synthetic_topology_is_derived_and_unresolved_status_preserved(pdf_bytes):
    state=workspace(pdf_bytes)
    topology=build_topology(state.candidates)
    assert Counter(r.kind for r in topology.relationships)=={"wall_connectivity":10,"room_boundary":10,"room_boundary_hypothesis":2,"room_adjacency":1,"opening_host":2}
    assert not [i for i in topology.issues if i.severity=="error"]
    assert all(r.status=="unresolved" for r in topology.relationships if r.kind=="opening_host")
    assert all(r.status=="inferred" for r in topology.relationships if r.kind in {"room_adjacency","room_boundary_hypothesis"})
    assert all(r.traversability=="not_assessed" for r in topology.relationships)
    assert all(r.support_geometry["coordinates"] for r in topology.relationships)
    assert not [i for i in validate_topology(state.candidates,topology) if i.severity=="error"]


def test_topology_ignores_arbitrary_source_relationship_hints(pdf_bytes):
    state=workspace(pdf_bytes)
    original=build_topology(state.candidates)
    scene=state.candidates.model_copy(update={"candidates":[c.model_copy(update={"related_candidate_ids":["fabricated-wall"]}) for c in state.candidates.candidates]})
    rebuilt=build_topology(scene)
    assert [(r.kind,r.from_candidate_id,r.to_candidate_id) for r in rebuilt.relationships]==[(r.kind,r.from_candidate_id,r.to_candidate_id) for r in original.relationships]
    assert not any("fabricated-wall" in (r.from_candidate_id,r.to_candidate_id) for r in rebuilt.relationships)


def test_forged_or_stale_topology_is_detected(pdf_bytes):
    state=workspace(pdf_bytes)
    topology=build_topology(state.candidates)
    forged=topology.model_copy(update={"relationships":[topology.relationships[0].model_copy(update={"to_candidate_id":"absent"})]+topology.relationships[1:]})
    assert any(i.code=="inconsistent_relationships" for i in validate_topology(state.candidates,forged))
    changed=state.candidates.model_copy(update={"candidates":state.candidates.candidates[:-1]})
    assert any(i.code=="stale_topology" for i in validate_topology(changed,topology))


def test_public_tools_commit_actual_candidates_and_roundtrip_versioned_json(tmp_path,pdf_bytes):
    api,state=service(tmp_path,pdf_bytes)
    assert len(api.list_candidates())==11
    assert api.select_candidate(api.list_candidates()[0].id)==api.list_candidates()[0]
    assert set(api.tool_definitions())=={"list_candidates","select_candidate","link_opening_to_wall","validate_model","commit_entities"}
    assert len(api.call_tool("list_candidates",{}))==11
    assert api.validate_model().structurally_valid
    committed=api.commit_entities([c.id for c in api.list_candidates()],"Test architect","Assemble provisional synthetic model")
    assert committed.version==1
    assert Counter(e.entity_type for e in committed.entities)=={"wall":8,"room":2,"opening":1}
    assert len(committed.relationships)==25
    assert committed.validation.structurally_valid
    assert all(e.status=="provisional" and not e.engineering_handoff_eligible for e in committed.entities)
    opening=next(e for e in committed.entities if e.entity_type=="opening")
    assert opening.candidate_resolution_status=="unresolved" and "unresolved_opening" in opening.flags
    rooms=[e for e in committed.entities if e.entity_type=="room"]
    assert all(e.measurement.value_si==6 and e.measurement.has_inferred_boundaries and e.measurement.verification_state=="needs_review" for e in rooms)
    exported=json.loads(json.dumps(api.export_model()))
    assert exported["current_assessment"]["matches_current_review"]
    assert BuildingModel.model_validate(exported["model"])==committed
    assert not exported["model"]["engineering_handoff_eligible"]


def test_room_commit_includes_real_dependencies_without_committing_every_room(tmp_path,pdf_bytes):
    api,_=service(tmp_path,pdf_bytes)
    room=api.list_candidates("room")[0]
    model=api.commit_entities([room.id])
    assert Counter(e.entity_type for e in model.entities)=={"wall":5,"room":1,"opening":1}
    ids={e.id for e in model.entities}
    assert all(r.from_entity_id in ids and r.to_entity_id in ids for r in model.relationships)


def test_supported_host_assignment_is_persistent_but_does_not_resolve_opening(tmp_path,pdf_bytes):
    api,_=service(tmp_path,pdf_bytes)
    relation=next(r for r in api.context().topology.relationships if r.kind=="opening_host")
    assignment=api.link_opening_to_wall(relation.from_candidate_id,relation.to_candidate_id,"Architect A","Observed flanking run, not a confirmed continuous host")
    restarted=PlanTraceService(ReviewStore(api.store.path),api.document_id,1)
    topology=restarted.context().topology
    linked=next(r for r in topology.relationships if r.assignment_id==assignment.id)
    assert linked.status=="unresolved" and linked.assignment_origin=="manual_supported_assignment"
    assert restarted.select_candidate(relation.from_candidate_id).status=="unresolved"
    unsupported=next(c for c in api.list_candidates("wall") if c.id not in {r.to_candidate_id for r in topology.relationships if r.kind=="opening_host"})
    with pytest.raises(ModelValidationError,match="unsupported"):
        api.link_opening_to_wall(relation.from_candidate_id,unsupported.id)
    assert len(api.context().assignments)==1


def test_decisions_and_manual_measurement_review_survive_new_store_instance(tmp_path,pdf_bytes):
    api,state=service(tmp_path,pdf_bytes)
    wall=api.list_candidates("wall")[0]
    api.record_decision(wall.id,"accept_provisional","Named architect","Geometry accepted for provisional assembly only")
    measurement=next(m for m in state.measurements.measurements if m.candidate_id==wall.id)
    approval=approve_measurement(state.measurements,measurement.id,"Dimension reviewer","Compared dimensions")
    updated=state.model_copy(update={"measurement_approvals":[approval],"measurements":measure_candidates(state.candidates,state.calibration,approvals=[approval])})
    api.store.save_workspace(updated,expected_revision=1)
    restarted=PlanTraceService(ReviewStore(api.store.path),api.document_id,1)
    context=restarted.context()
    assert restarted.decision_status(context,wall)=="accepted_provisional"
    assert context.workspace.measurements.select_measurement(measurement.id).verification_state=="manually_approved"
    assert not context.workspace.measurements.select_measurement(measurement.id).engineering_approved
    data,info=restarted.store.load_source(api.document_id)
    assert data==pdf_bytes and info==state.document
    assert len(restarted.store.history(api.document_id,1))>=3


def test_model_versions_are_immutable_and_calibration_changes_mark_old_export_historical(tmp_path,pdf_bytes):
    api,state=service(tmp_path,pdf_bytes)
    first=api.commit_entities([c.id for c in api.list_candidates()])
    book=put_reference(state.calibration,state.calibration.references[0].model_copy(update={"known_distance":4.4}))
    updated=state.model_copy(update={"calibration":book,"measurements":measure_candidates(state.candidates,book)})
    api.store.save_workspace(updated,expected_revision=1)
    historical=api.export_model(1)
    assert not historical["current_assessment"]["matches_current_review"]
    assert historical["current_assessment"]["historical_snapshot"]
    second=api.commit_entities([],"Architect","Refresh measurements under changed calibration")
    assert second.version==2
    assert api.store.list_versions(api.document_id,1)==[1,2]
    assert BuildingModel.model_validate(api.export_model(1)["model"])==first
    assert api.export_model(2)["current_assessment"]["matches_current_review"]
    assert all(e.measurement.verification_state=="needs_review" for e in second.entities if e.measurement)


def test_wall_edit_invalidates_topology_room_commit_and_prior_measurements(tmp_path,pdf_bytes):
    api,state=service(tmp_path,pdf_bytes)
    first=api.commit_entities([c.id for c in api.list_candidates()])
    wall=next(c for c in api.list_candidates("wall") if c.geometry.as_shapely().bounds==(100,100,100,400))
    correction=correct_candidate(wall,[[110,100],[110,400]],state.inspection.page)
    scene=state.candidates.model_copy(update={"candidates":state.candidates.candidates+[correction]})
    updated=state.model_copy(update={"candidates":scene,"geometry_revision":1,"measurements":measure_candidates(scene,state.calibration,1)})
    api.store.save_workspace(updated,expected_revision=1)
    context=api.context()
    assert wall.id not in context.topology.effective_candidate_ids
    affected=next(c for c in api.list_candidates("room") if c.geometry.as_shapely().bounds[0]==100)
    report=api.validate_model([affected.id])
    assert not report.structurally_valid
    assert any(i.code=="unsupported_room_boundary" for i in report.issues)
    assert next(m for m in context.workspace.measurements.measurements if m.candidate_id==affected.id).value_si is None
    with pytest.raises(ModelValidationError):
        api.commit_entities([affected.id])
    assert api.store.list_versions(api.document_id,1)==[1]
    historical=api.export_model()
    assert historical["current_assessment"]["stale_entity_ids"]
    assert BuildingModel.model_validate(historical["model"])==first
    refreshed=api.commit_entities([correction.id])
    assert refreshed.version==2
    assert any(e.candidate_id==affected.id and e.status=="stale" for e in refreshed.retired_entities)


def test_rejecting_candidate_excludes_it_from_model_and_can_be_explicitly_reconsidered(tmp_path,pdf_bytes):
    api,_=service(tmp_path,pdf_bytes)
    opening=api.list_candidates("opening")[0]
    api.record_decision(opening.id,"reject","Architect","Opening interpretation rejected")
    assert not api.list_candidates("opening")
    assert len(api.list_candidates(include_inactive=True))==11
    with pytest.raises(ModelValidationError):
        api.commit_entities([opening.id])
    # Excluding an opening invalidates its room-closure hypothesis, not its existence in history.
    assert any(i.code=="unsupported_room_boundary" for i in api.validate_model().issues)
    api.record_decision(opening.id,"accept_provisional","Architect","Restore as unresolved possibility")
    assert len(api.list_candidates("opening"))==1
    assert api.decision_status(api.context(),opening)=="accepted_provisional"
    assert api.select_candidate(opening.id).status=="unresolved"


def test_stale_or_tampered_measurement_is_not_silently_committed(tmp_path,pdf_bytes):
    api,state=service(tmp_path,pdf_bytes)
    first=state.measurements.measurements[0]
    tampered=state.measurements.model_copy(update={"measurements":[first.model_copy(update={"value_si":999})]+state.measurements.measurements[1:]})
    api.store.save_workspace(state.model_copy(update={"measurements":tampered}),expected_revision=1)
    assert not api.validate_model([first.candidate_id]).structurally_valid
    with pytest.raises(ModelValidationError,match="measurement_provenance_mismatch"):
        api.commit_entities([first.candidate_id])
    assert not api.store.list_versions(api.document_id,1)


def test_optimistic_concurrency_protects_saved_work(tmp_path,pdf_bytes):
    api,state=service(tmp_path,pdf_bytes)
    foreign=state.model_copy(update={"geometry_revision":1,"measurements":measure_candidates(state.candidates,state.calibration,1)})
    api.store.save_workspace(foreign,expected_revision=1)
    changed=state.model_copy(update={"geometry_revision":2,"measurements":measure_candidates(state.candidates,state.calibration,2)})
    with pytest.raises(ReviewConflict,match="another session"):
        api.store.save_workspace(changed,expected_revision=1)
    assert api.store.load_workspace(api.document_id,1).workspace.geometry_revision==1


def test_invalid_tool_or_empty_initial_commit_does_not_create_version(tmp_path,pdf_bytes):
    api,_=service(tmp_path,pdf_bytes)
    with pytest.raises(ValueError,match="Unknown"):
        api.call_tool("delete_everything",{})
    with pytest.raises(ModelValidationError):
        api.commit_entities([])
    with pytest.raises(KeyError):
        api.select_candidate("absent")
    assert not api.store.list_versions(api.document_id,1)


def test_corrupt_database_is_reported_without_reset(tmp_path):
    path=tmp_path/"corrupt.sqlite3"
    path.write_bytes(b"this is not sqlite")
    with pytest.raises(PersistenceError):
        ReviewStore(path)
    assert path.read_bytes()==b"this is not sqlite"


def test_literal_engineering_flags_cannot_be_enabled(tmp_path,pdf_bytes):
    api,_=service(tmp_path,pdf_bytes)
    model=api.commit_entities([c.id for c in api.list_candidates()])
    with pytest.raises(ValidationError):
        BuildingModel.model_validate({**model.model_dump(),"engineering_handoff_eligible":True})


def test_dangling_entity_relations_and_changed_measurement_values_fail_validation(tmp_path,pdf_bytes):
    api,_=service(tmp_path,pdf_bytes)
    model=api.commit_entities([c.id for c in api.list_candidates()])
    relation=model.relationships[0].model_copy(update={"to_entity_id":"missing-entity"})
    forged=model.model_copy(update={"relationships":[relation]+model.relationships[1:]})
    assert any(issue.code=="dangling_entity_relationship" for issue in validate_assembled_model(forged).issues)
    entity=next(entity for entity in model.entities if entity.measurement is not None)
    bad=entity.model_copy(update={"measurement":entity.measurement.model_copy(update={"value_si":999})})
    forged=model.model_copy(update={"entities":[bad if e.id==entity.id else e for e in model.entities]})
    codes={issue.code for issue in validate_assembled_model(forged).issues}
    assert {"entity_measurement_mismatch","invalid_si_provenance"}<=codes


def test_point_contact_does_not_create_room_adjacency():
    import pymupdf
    with pymupdf.open() as document:
        page=document.new_page(width=600,height=600)
        page.draw_rect((100,100,300,300),width=4)
        page.draw_rect((300,300,500,500),width=4)
        scene=extract_candidates(inspect_page(document.tobytes(),"point.pdf"))
    topology=build_topology(scene)
    assert len(scene.list_candidates("room"))==2
    assert not any(relation.kind=="room_adjacency" for relation in topology.relationships)


def test_invalid_overlapping_manual_rooms_are_not_structurally_valid(pdf_bytes):
    state=workspace(pdf_bytes)
    room=next(c for c in state.candidates.list_candidates("room") if c.geometry.as_shapely().bounds[0]==300)
    corrected=correct_candidate(room,[[250,100],[500,100],[500,400],[250,400],[250,100]],state.inspection.page)
    scene=state.candidates.model_copy(update={"candidates":state.candidates.candidates+[corrected]})
    topology=build_topology(scene)
    assert any(issue.code=="overlapping_rooms" and issue.severity=="error" for issue in topology.issues)


def test_small_gap_hypothesis_can_be_committed_without_inventing_an_opening(tmp_path):
    import pymupdf
    from plantrace.assembly_models import ReviewWorkspace
    with pymupdf.open() as document:
        page=document.new_page(width=600,height=500)
        for a,b in [((100,100),(299,100)),((301,100),(500,100)),((500,100),(500,400)),((500,400),(100,400)),((100,400),(100,100))]:
            page.draw_line(a,b,width=4)
        data=document.tobytes()
    inspection=inspect_page(data,"gap.pdf")
    scene=extract_candidates(inspection)
    assert len(scene.list_candidates("room"))==1 and not scene.list_candidates("opening")
    book=new_book(inspection)
    state=ReviewWorkspace(document=inspect_document(data,"gap.pdf"),inspection=inspection,candidates=scene,calibration=book,measurements=measure_candidates(scene,book))
    store=ReviewStore(tmp_path/"gap.sqlite3")
    store.save_source(data,state.document)
    store.save_workspace(state)
    api=PlanTraceService(store,state.document.document_id,1)
    model=api.commit_entities([scene.list_candidates("room")[0].id])
    assert not any(entity.entity_type=="opening" for entity in model.entities)
    assert any(relation.candidate_relationship.kind=="room_boundary_hypothesis" for relation in model.relationships)
    assert model.validation.structurally_valid


def test_source_and_review_snapshot_integrity_failures_are_explicit(tmp_path,pdf_bytes):
    api,state=service(tmp_path,pdf_bytes)
    with api.store.transaction() as connection:
        row=connection.execute("SELECT snapshot_id,payload_json FROM workspace_snapshots LIMIT 1").fetchone()
        payload=json.loads(row["payload_json"])
        payload["geometry_revision"]=99
        connection.execute("UPDATE workspace_snapshots SET payload_json=? WHERE snapshot_id=?",(json.dumps(payload),row["snapshot_id"]))
    with pytest.raises(PersistenceError,match="integrity"):
        api.store.load_workspace(api.document_id,1)
    with api.store.transaction() as connection:
        connection.execute("UPDATE sources SET content=? WHERE document_id=?",(b"corrupt",api.document_id))
    with pytest.raises(PersistenceError,match="integrity"):
        api.store.load_source(api.document_id)


def test_agent_tool_schema_rejects_invalid_types_and_unknown_fields(tmp_path,pdf_bytes):
    api,_=service(tmp_path,pdf_bytes)
    assert api.tool_definitions()["commit_entities"]["parameters"]["properties"]["ids"]["type"]=="array"
    with pytest.raises(ValueError,match="Invalid type"):
        api.call_tool("commit_entities",{"ids":"not-an-array"})
    with pytest.raises(ValueError,match="unsupported"):
        api.call_tool("list_candidates",{"extra":True})
    with pytest.raises(ValueError,match="Candidate type"):
        api.list_candidates("door")
