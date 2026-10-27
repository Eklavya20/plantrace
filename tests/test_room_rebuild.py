import json

import pymupdf
import pytest

from plantrace.calibration import put_reference
from plantrace.candidates import extract_candidates
from plantrace.corrections import correct_candidate
from plantrace.documents import inspect_page
from plantrace.measurements import measure_candidates,approval_blockers
from plantrace.room_rebuild import rebuild_room,reconstruction_is_current
from plantrace.topology import build_topology
from test_assembly import workspace,service


def edited(data):
    state=workspace(data)
    wall=next(c for c in state.candidates.list_candidates("wall") if c.geometry.as_shapely().bounds==(100,100,100,400))
    room=next(c for c in state.candidates.list_candidates("room") if c.geometry.as_shapely().bounds==(100,100,300,400))
    correction=correct_candidate(wall,[[110,100],[110,400]],state.inspection.page)
    scene=state.candidates.model_copy(update={"candidates":state.candidates.candidates+[correction]})
    return state,scene,room,correction


def test_actual_room_recovers_area_after_wall_edit_and_preserves_uncertainty(pdf_bytes):
    state,scene,room,wall=edited(pdf_bytes)
    original=next(m for m in state.measurements.measurements if m.candidate_id==room.id)
    assert original.value_si==pytest.approx(6,abs=1e-12)
    stale=next(m for m in measure_candidates(scene,state.calibration,1).measurements if m.candidate_id==room.id)
    assert stale.value_si is None
    rebuilt=rebuild_room(scene,room.id,state.inspection.page,"Test architect","Rebuild after moving the observed wall")
    after=scene.model_copy(update={"candidates":scene.candidates+[rebuilt]})
    values=measure_candidates(after,state.calibration,2)
    area=next(m for m in values.measurements if m.candidate_id==rebuilt.id)
    assert rebuilt.geometry.as_shapely().bounds==(110,100,300,400)
    assert rebuilt.geometry.as_shapely().area==57000
    assert area.value_si==pytest.approx(5.7,abs=1e-12)
    assert area.verification_state=="needs_review" and area.boundary_support=="manual_unverified"
    assert area.has_inferred_boundaries and approval_blockers(area)
    assert rebuilt.boundary_bridge_ids==room.boundary_bridge_ids
    assert all(e.source.document_id==state.document.document_id for e in rebuilt.evidence)
    assert rebuilt.reconstruction_basis.wall_geometry_versions[wall.id]
    assert rebuilt.reconstruction_basis.original_room_id==room.id
    assert reconstruction_is_current(rebuilt,after)
    assert not [i for i in build_topology(after).issues if i.severity=="error"]
    assert next(m for m in values.measurements if m.candidate_id==room.id).value_si is None
    assert rebuild_room(scene,room.id,state.inspection.page,"Test architect","Rebuild after moving the observed wall")==rebuilt


def test_scale_recalculation_keeps_geometry_but_second_wall_edit_invalidates_rebuild(pdf_bytes):
    state,scene,room,wall=edited(pdf_bytes)
    rebuilt=rebuild_room(scene,room.id,state.inspection.page,"Reviewer","First rebuild")
    after=scene.model_copy(update={"candidates":scene.candidates+[rebuilt]})
    book=state.calibration
    for reference in list(book.references):
        book=put_reference(book,reference.model_copy(update={"known_distance":reference.known_distance*1.1}))
    area=next(m for m in measure_candidates(after,book).measurements if m.candidate_id==rebuilt.id)
    assert area.value_si==pytest.approx(5.7*1.21,abs=1e-12)
    assert reconstruction_is_current(rebuilt,after)
    original_wall=scene.select_candidate(wall.supersedes_id)
    next_wall=correct_candidate(original_wall,[[120,100],[120,400]],state.inspection.page)
    changed=after.model_copy(update={"candidates":[c for c in after.candidates if c.id!=wall.id]+[next_wall]})
    assert not reconstruction_is_current(rebuilt,changed)
    stale=next(m for m in measure_candidates(changed,state.calibration).measurements if m.candidate_id==rebuilt.id)
    assert stale.value_si is None and stale.boundary_support=="stale"
    next_room=rebuild_room(changed,room.id,state.inspection.page,"Reviewer","Second wall change")
    latest=changed.model_copy(update={"candidates":[c for c in changed.candidates if c.id!=rebuilt.id]+[next_room]})
    assert next(m.value_si for m in measure_candidates(latest,state.calibration).measurements if m.candidate_id==next_room.id)==pytest.approx(5.4,abs=1e-12)


def test_open_boundaries_and_room_merge_are_rejected_without_returning_a_polygon(pdf_bytes):
    state,scene,room,wall=edited(pdf_bytes)
    open_scene=scene.model_copy(update={"candidates":[c for c in scene.candidates if c.id not in {wall.id,wall.supersedes_id}]})
    with pytest.raises(ValueError,match="unique closed face"):
        rebuild_room(open_scene,room.id,state.inspection.page,"Reviewer","Cannot close absent wall")
    partition=[w.id for w in state.candidates.list_candidates("wall") if w.geometry.as_shapely().bounds[0]==w.geometry.as_shapely().bounds[2]==300]
    merged=state.candidates.model_copy(update={"candidates":[c for c in state.candidates.candidates if c.id not in partition],"bridges":[]})
    with pytest.raises(ValueError,match="overlaps another room"):
        rebuild_room(merged,room.id,state.inspection.page,"Reviewer","A missing partition must not silently merge rooms")


@pytest.mark.parametrize("split_x",[150,200])
def test_split_is_ambiguous_even_away_from_seed_using_actual_extracted_geometry(split_x):
    with pymupdf.open() as doc:
        page=doc.new_page(width=600,height=500)
        page.draw_rect((100,100,500,400),width=4)
        page.draw_line((300,100),(300,400),width=4)
        page.draw_line((50,100),(50,400),width=4)
        data=doc.tobytes()
    inspection=inspect_page(data,"split.pdf")
    scene=extract_candidates(inspection)
    room=next(c for c in scene.list_candidates("room") if c.geometry.as_shapely().bounds==(100,100,300,400))
    outside=next(c for c in scene.list_candidates("wall") if c.geometry.as_shapely().bounds==(50,100,50,400))
    split=correct_candidate(outside,[[split_x,100],[split_x,400]],inspection.page)
    changed=scene.model_copy(update={"candidates":scene.candidates+[split]})
    with pytest.raises(ValueError,match="unique closed face|split the original"):
        rebuild_room(changed,room.id,inspection.page,"Reviewer","Ambiguous split")


def test_rebuild_requires_actual_change_and_explicit_reviewer_and_uses_budgets(pdf_bytes):
    state,scene,room,_=edited(pdf_bytes)
    for reviewer,note in [(" ","Reason"),("Reviewer"," ")]:
        with pytest.raises(ValueError,match="reviewer"):
            rebuild_room(scene,room.id,state.inspection.page,reviewer,note)
    with pytest.raises(ValueError,match="already matches"):
        rebuild_room(state.candidates,room.id,state.inspection.page,"Reviewer","No change")
    limited=scene.model_copy(update={"config":scene.config.model_copy(update={"max_segments":1})})
    with pytest.raises(ValueError,match="segment budget"):
        rebuild_room(limited,room.id,state.inspection.page,"Reviewer","Limited")


def test_room_holes_are_preserved_from_current_network():
    with pymupdf.open() as doc:
        page=doc.new_page(width=600,height=500)
        page.draw_rect((100,100,500,400),width=4)
        page.draw_rect((200,200,300,300),width=4)
        data=doc.tobytes()
    inspection=inspect_page(data,"holes.pdf")
    scene=extract_candidates(inspection)
    room=next(c for c in scene.list_candidates("room") if c.geometry.holes)
    wall=next(c for c in scene.list_candidates("wall") if c.geometry.as_shapely().bounds==(100,100,100,400))
    correction=correct_candidate(wall,[[110,100],[110,400]],inspection.page)
    changed=scene.model_copy(update={"candidates":scene.candidates+[correction]})
    rebuilt=rebuild_room(changed,room.id,inspection.page,"Reviewer","Preserve the interior void")
    assert len(rebuilt.geometry.holes)==1
    assert rebuilt.geometry.as_shapely().area==107000
    assert "interior_holes_unresolved" in rebuilt.flags
    assert rebuilt.status=="unresolved"


def test_bridge_tied_to_superseded_wall_cannot_be_reused_to_force_two_rooms(pdf_bytes):
    state=workspace(pdf_bytes)
    scene=state.candidates
    room=next(c for c in scene.list_candidates("room") if c.geometry.as_shapely().bounds==(100,100,300,400))
    top_partition=next(c for c in scene.list_candidates("wall") if c.geometry.as_shapely().bounds==(300,100,300,220))
    moved=correct_candidate(top_partition,[[310,100],[310,220]],state.inspection.page)
    changed=scene.model_copy(update={"candidates":scene.candidates+[moved]})
    with pytest.raises(ValueError,match="overlaps another room"):
        rebuild_room(changed,room.id,state.inspection.page,"Reviewer","Cannot reuse an obsolete opening closure")


def test_rebuilt_room_commits_and_restores_without_rewriting_historical_model(tmp_path,pdf_bytes):
    api,state=service(tmp_path,pdf_bytes)
    initial=api.commit_entities([c.id for c in state.candidates.candidates],"Architect","Before correction")
    _,scene,room,wall=edited(pdf_bytes)
    rebuilt=rebuild_room(scene,room.id,state.inspection.page,"Architect","Room area recovery")
    scene=scene.model_copy(update={"candidates":scene.candidates+[rebuilt]})
    from plantrace.assembly_models import CorrectionContext
    changed=state.model_copy(update={"candidates":scene,"geometry_revision":2,
        "corrections":[CorrectionContext(config=scene.config,candidates=[wall,rebuilt])],
        "measurements":measure_candidates(scene,state.calibration,2)})
    api.store.save_workspace(changed,expected_revision=1)
    updated=api.commit_entities([wall.id,rebuilt.id],"Architect","Current provisional rooms")
    assert len(updated.entities)==11
    assert len(updated.retired_entities)==2
    assert next(e.measurement.value_si for e in updated.entities if e.candidate_id==rebuilt.id)==pytest.approx(5.7,abs=1e-12)
    assert api.export_model(1)["model"]==initial.model_dump(mode="json")
    from plantrace.repository import ReviewStore
    from plantrace.assembly import PlanTraceService
    reopened=PlanTraceService(ReviewStore(tmp_path/"review.sqlite3"),state.document.document_id,1)
    exported=reopened.export_model(updated.version)
    assert exported["current_assessment"]["matches_current_review"]
    assert not exported["engineering_handoff_gate"]["eligible"]
    assert exported["model"]["source_review"]["corrections"]
    assert json.dumps(exported,allow_nan=False)
