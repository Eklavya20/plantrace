import json
from pathlib import Path

from streamlit.testing.v1 import AppTest

from plantrace.repository import ReviewStore,database_path
from test_metrology_ui import APP,calibrated_app,item,example_app


def saved_app(document_id):
    app=AppTest.from_file(str(APP),default_timeout=10).run()
    item(app,"selectbox","Saved drawing").select(document_id).run()
    assert not app.exception
    return app


def test_synthetic_ui_assembles_and_restores_model_and_review_after_restart():
    app=calibrated_app()
    document_id=app.session_state.current_measurements.document_id
    opening=next(c for c in list(app.session_state.candidate_cache.values())[-1].candidates if c.candidate_type=="opening")
    item(app,"text_input","Architect/reviewer name").input("Synthetic test architect").run()
    item(app,"selectbox","Opening for host review").select(opening.id).run()
    support=next(r for r in app.session_state.current_topology.relationships if r.kind=="opening_host")
    item(app,"selectbox","Supported host wall or flank").select(support.to_candidate_id).run()
    item(app,"text_input","Host assignment note").input("Supported flank only; opening interpretation unresolved")
    item(app,"button","Record supported host assignment").click().run()
    assert not app.exception
    item(app,"selectbox","Candidate for architect decision").select(opening.id).run()
    item(app,"selectbox","Architect decision").select("needs_review")
    item(app,"text_input","Architect decision note").input("Door/window semantics not established")
    item(app,"button","Save architect decision").click().run()
    assert not app.exception
    item(app,"multiselect","Candidates to commit as provisional").set_value(app.session_state.current_topology.effective_candidate_ids)
    item(app,"button","Commit provisional entities").click().run()
    assert not app.exception
    exported=app.session_state.current_building_model
    assert exported["model"]["version"]==1
    assert len(exported["model"]["entities"])==11
    assert len(exported["model"]["relationships"])==25
    assert exported["current_assessment"]["matches_current_review"]
    assert len(app.get("download_button"))==2
    restarted=saved_app(document_id)
    assert restarted.session_state.current_measurements.assessments[0].status=="cross_checked"
    assert len(restarted.session_state.current_measurements.calibration.references)==2
    restored=restarted.session_state.current_building_model
    assert restored==exported
    assert len(restored["model"]["review_decisions"])==1
    assert len(restored["model"]["host_assignments"])==1
    opening_entity=next(entity for entity in restored["model"]["entities"] if entity["entity_type"]=="opening")
    assert opening_entity["candidate_resolution_status"]=="unresolved"
    assert opening_entity["review_status"]=="needs_review"
    assert all(not entity["engineering_handoff_eligible"] for entity in restored["model"]["entities"])


def test_manual_geometry_and_detection_settings_survive_restart():
    app=calibrated_app()
    item(app,"number_input","Minimum wall stroke width").set_value(3.0).run()
    item(app,"number_input","Endpoint gap tolerance").set_value(1.0).run()
    candidates=list(app.session_state.candidate_cache.values())[-1]
    wall=next(c for c in candidates.candidates if c.candidate_type=="wall" and c.geometry.as_shapely().bounds==(100,100,100,400))
    item(app,"selectbox","Select candidate to highlight").select(wall.id).run()
    app.text_area[0].input("[[110,100],[110,400]]")
    item(app,"button","Save manual correction").click().run()
    assert not app.exception
    document_id=app.session_state.current_measurements.document_id
    restarted=saved_app(document_id)
    assert item(restarted,"number_input","Minimum wall stroke width").value==3.0
    assert item(restarted,"number_input","Endpoint gap tolerance").value==1.0
    measurements=restarted.session_state.current_measurements
    assert measurements.geometry_revision==1
    assert any(m.candidate_origin=="manual_correction" for m in measurements.measurements)
    affected=next(m for m in measurements.measurements if m.candidate_type=="room" and m.source_geometry.as_shapely().bounds[0]==100)
    assert affected.value_si is None and affected.boundary_support=="stale"
    assert any(issue.code=="unsupported_room_boundary" for issue in restarted.session_state.current_topology.issues)


def test_stale_session_cannot_overwrite_another_sessions_calibration():
    first=calibrated_app()
    document_id=first.session_state.current_measurements.document_id
    second=saved_app(document_id)
    reference_id=first.session_state.current_measurements.calibration.references[0].id
    item(first,"selectbox","Reference to add or revise").select(reference_id).run()
    item(first,"number_input","Known real-world distance").set_value(4.4)
    item(first,"button","Save calibration reference").click().run()
    assert not first.exception
    item(second,"selectbox","Reference to add or revise").select(reference_id).run()
    item(second,"number_input","Known real-world distance").set_value(4.6)
    item(second,"button","Save calibration reference").click().run()
    assert not second.exception
    assert any("another session" in error.value for error in second.error)
    saved=ReviewStore(database_path()).load_workspace(document_id,1)
    assert saved.workspace.calibration.references[0].known_distance==4.4
    item(second,"button","Reload saved review").click().run()
    assert not second.exception
    assert second.session_state.current_measurements.calibration.references[0].known_distance==4.4


def test_failed_or_unnamed_commit_does_not_create_model():
    app=example_app()
    item(app,"multiselect","Candidates to commit as provisional").set_value(app.session_state.current_topology.effective_candidate_ids)
    item(app,"button","Commit provisional entities").click().run()
    assert not app.exception
    assert any("name" in error.value for error in app.error)
    assert app.session_state.current_building_model is None
