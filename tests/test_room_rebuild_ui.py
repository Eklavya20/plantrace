import pytest

from test_metrology_ui import calibrated_app,item
from test_persistent_ui import saved_app


def test_room_rebuild_is_reachable_after_measurement_selection_and_restores():
    app=calibrated_app()
    scene=list(app.session_state.candidate_cache.values())[-1]
    wall=next(c for c in scene.list_candidates("wall") if c.geometry.as_shapely().bounds==(100,100,100,400))
    room=next(c for c in scene.list_candidates("room") if c.geometry.as_shapely().bounds==(100,100,300,400))
    selected=next(m for m in app.session_state.current_measurements.measurements if m.candidate_id==wall.id)
    item(app,"selectbox","Select measurement to inspect").select(selected.id).run()
    item(app,"selectbox","Select candidate to highlight").select(room.id).run()
    assert item(app,"selectbox","Select measurement to inspect").value is None
    assert item(app,"button","Rebuild room from current walls")
    item(app,"selectbox","Select candidate to highlight").select(wall.id).run()
    item(app,"text_area","Corrected coordinates (source PDF points)").input("[[110,100],[110,400]]")
    item(app,"button","Save manual correction").click().run()
    assert next(m.value_si for m in app.session_state.current_measurements.measurements if m.candidate_id==room.id) is None
    item(app,"selectbox","Select candidate to highlight").select(room.id).run()
    item(app,"text_input","Room rebuild reviewer").input("Synthetic UI reviewer")
    item(app,"text_input","Room rebuild reason").input("Recover the room face after a wall correction")
    item(app,"button","Rebuild room from current walls").click().run()
    assert not app.exception
    current=app.session_state.current_measurements
    rebuilt=next(m for m in current.measurements if ":rebuilt:" in m.candidate_id)
    assert rebuilt.value_si==pytest.approx(5.7,abs=1e-12)
    assert rebuilt.verification_state=="needs_review" and rebuilt.has_inferred_boundaries
    assert item(app,"selectbox","Select measurement to inspect").value==rebuilt.id
    assert item(app,"button","Record manual measurement approval").disabled
    restarted=saved_app(current.document_id)
    restored=next(m for m in restarted.session_state.current_measurements.measurements if m.candidate_id==rebuilt.candidate_id)
    assert restored==rebuilt


def test_failed_rebuild_does_not_save_a_room_or_advance_geometry_revision():
    app=calibrated_app()
    scene=list(app.session_state.candidate_cache.values())[-1]
    room=scene.list_candidates("room")[0]
    item(app,"selectbox","Select candidate to highlight").select(room.id).run()
    before=app.session_state.current_measurements.geometry_revision
    item(app,"text_input","Room rebuild reviewer").input("Reviewer")
    item(app,"text_input","Room rebuild reason").input("Try unchanged geometry")
    item(app,"button","Rebuild room from current walls").click().run()
    assert not app.exception
    assert any("already matches" in e.value for e in app.error)
    assert app.session_state.current_measurements.geometry_revision==before
    assert not any(":rebuilt:" in m.candidate_id for m in app.session_state.current_measurements.measurements)
