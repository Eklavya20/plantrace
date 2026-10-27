import json
from pathlib import Path

from streamlit.testing.v1 import AppTest
import streamlit as st
from plantrace.metrology_models import MeasurementSet

APP=Path(__file__).resolve().parents[1]/"app.py"


def item(app,kind,label):
    return next(widget for widget in getattr(app,kind) if widget.label==label)


def example_app():
    app=AppTest.from_file(str(APP),default_timeout=10).run()
    item(app,"checkbox","Use synthetic example").check().run()
    assert not app.exception
    return app


def add_reference(app,points,distance,label,unit="m",verified=False):
    item(app,"radio","Reference point entry").set_value("Source coordinates").run()
    for label_text,value in zip(["Reference X1","Reference Y1","Reference X2","Reference Y2"],[value for pair in points for value in pair]):
        item(app,"number_input",label_text).set_value(float(value))
    item(app,"number_input","Known real-world distance").set_value(float(distance))
    item(app,"text_input","Known dimension label").input(label)
    item(app,"selectbox","Known distance unit").select(unit)
    if verified:
        from support_provenance import physical_record
        record=physical_record(label)
        item(app,"selectbox","How was this reference established?").select(record.kind)
        item(app,"text_input","Shared evidence/error-source group").input(record.independence_group)
        item(app,"text_input","Evidence record or source annotation locator").input(record.evidence_record)
        item(app,"text_input","Evidence SHA-256 (required for physical verification)").input(record.evidence_sha256)
        item(app,"text_input","Reference established by").input(record.established_by)
        item(app,"text_input","Evidence date or record timestamp").input(record.established_at)
        item(app,"selectbox","How were the units established?").select(record.unit_basis)
        item(app,"text_input","Unit evidence or assumption").input(record.unit_statement)
    item(app,"button","Save calibration reference").click().run()
    assert not app.exception
    return app


def calibrated_app(verified=False):
    app=example_app()
    add_reference(app,[(100,100),(500,100)],4,"Horizontal known dimension",verified=verified)
    add_reference(app,[(100,100),(100,400)],3000,"Independent vertical dimension","mm",verified=verified)
    return app


def test_missing_calibration_shows_no_si_and_blocks_manual_review():
    app=example_app()
    measurements=app.session_state.current_measurements
    assert all(m.value_si is None for m in measurements.measurements)
    assert any("Missing calibration" in warning.value for warning in app.warning)
    selected=measurements.measurements[0]
    item(app,"selectbox","Select measurement to inspect").select(selected.id).run()
    assert not app.exception
    assert item(app,"button","Record manual measurement approval").disabled
    assert any("SI value unavailable" in str(markdown.value) for markdown in app.markdown)


def test_source_coordinate_references_produce_lengths_and_inferred_areas():
    app=calibrated_app()
    measurements=app.session_state.current_measurements
    assert measurements.assessments[0].status=="cross_checked"
    assert len(measurements.calibration.references)==2
    assert all(r.selection_method=="manual_source_coordinates" for r in measurements.calibration.references)
    rooms=[m for m in measurements.measurements if m.candidate_type=="room"]
    assert [m.value_si for m in rooms]==[6,6]
    assert all(m.verification_state=="needs_review" for m in rooms)
    overview=app.get("image")[0].value
    item(app,"selectbox","Select measurement to inspect").select(rooms[0].id).run()
    assert not app.exception
    assert app.get("image")[0].value!=overview
    assert item(app,"button","Record manual measurement approval").disabled
    evidence=next(json.loads(e.value) for e in app.json if json.loads(e.value).get("candidate_id")==rooms[0].candidate_id)
    assert evidence["has_inferred_boundaries"]
    assert len(evidence["calibration_references"])==2
    assert not evidence["engineering_approved"]


def test_explicit_manual_review_is_invalidated_by_reference_revision():
    app=calibrated_app(verified=True)
    wall=next(m for m in app.session_state.current_measurements.measurements if m.candidate_type=="wall" and m.value_si==3)
    item(app,"selectbox","Select measurement to inspect").select(wall.id).run()
    assert not item(app,"button","Record manual measurement approval").disabled
    item(app,"text_input","Measurement reviewer name").input("Local reviewer")
    item(app,"text_input","Measurement review note").input("Compared both dimensions and the source line.")
    item(app,"button","Record manual measurement approval").click().run()
    assert not app.exception
    current=app.session_state.current_measurements
    assert current.select_measurement(wall.id).verification_state=="manually_approved"
    assert current.approvals[0].status=="current"
    first=current.calibration.references[0]
    item(app,"selectbox","Reference to add or revise").select(first.id).run()
    item(app,"number_input","Known real-world distance").set_value(4.4)
    item(app,"button","Save calibration reference").click().run()
    assert not app.exception
    current=app.session_state.current_measurements
    assert current.assessments[0].status=="conflicting"
    assert current.approvals[0].status=="stale"
    assert any("Conflicting calibration references" in error.value for error in app.error)
    assert item(app,"button","Record manual measurement approval").disabled


def test_manual_wall_edit_invalidates_stale_neighbors_and_selects_current_geometry():
    app=calibrated_app()
    selected=next(m for m in app.session_state.current_measurements.measurements if m.candidate_type=="wall" and m.source_geometry.as_shapely().bounds==(100,100,100,400))
    item(app,"selectbox","Select measurement to inspect").select(selected.id).run()
    app.text_area[0].input("[[110,100],[110,400]]")
    item(app,"button","Save manual correction").click().run()
    assert not app.exception
    current=app.session_state.current_measurements
    assert current.geometry_revision==1
    rooms=[m for m in current.measurements if m.candidate_type=="room"]
    affected=next(m for m in rooms if m.source_geometry.as_shapely().bounds[0]==100)
    unaffected=next(m for m in rooms if m.source_geometry.as_shapely().bounds[0]==300)
    assert affected.value_si is None and affected.boundary_support=="stale"
    assert unaffected.value_si==6 and unaffected.verification_state=="needs_review"
    selected_id=item(app,"selectbox","Select measurement to inspect").value
    assert current.select_measurement(selected_id).candidate_origin=="manual_correction"
    assert item(app,"button","Record manual measurement approval").disabled


def test_invalid_reference_stays_unsaved_and_region_creation_changes_scope():
    app=example_app()
    add_reference(app,[(100,100),(100,100)],4,"Invalid zero span")
    assert not app.session_state.current_measurements.calibration.references
    assert any("Reference not saved" in error.value for error in app.error)
    item(app,"text_input","New region name").input("Left detail")
    item(app,"number_input","Region X max").set_value(300.0)
    item(app,"button","Add scale region").click().run()
    assert not app.exception
    assert len(app.session_state.current_measurements.calibration.regions)==2
    region_id=item(app,"selectbox","Calibration region").value
    assert app.session_state.current_measurements.calibration.regions[-1].id==region_id


def test_printed_hypothesis_remains_unverified_and_page_switch_is_isolated():
    app=example_app()
    item(app,"button","Store printed-scale hypothesis").click().run()
    assert not app.exception
    current=app.session_state.current_measurements
    assert current.assessments[0].status=="hypothesis_only"
    assert all(m.verification_state=="needs_review" for m in current.measurements)
    item(app,"selectbox","Page").select(2).run()
    assert not app.exception
    current=app.session_state.current_measurements
    assert not current.measurements
    assert not current.calibration.printed_scales


def test_preview_component_payload_uses_real_affine_and_preserves_click_evidence():
    app=example_app()
    for source_points,distance,label in [([(100,100),(500,100)],4,"Clicked horizontal"), ([(100,100),(100,400)],3,"Clicked vertical")]:
        component=app.get("component_instance")[0]
        args=json.loads(component.proto.json_args)
        a,b,c,d,e,f=args["transform"]
        pixels=[{"x":a*x+c*y+e,"y":b*x+d*y+f} for x,y in source_points]
        # Simulate the widget event emitted by the independently tested JS click handlers.
        app.session_state["picker_"+args["frame"]]={"frame":args["frame"],"reset":args["reset"],"region_id":args["region"]["id"],"points":pixels}
        app.run()
        item(app,"text_input","Known dimension label").input(label)
        item(app,"number_input","Known real-world distance").set_value(float(distance))
        item(app,"selectbox","Known distance unit").select("m")
        item(app,"button","Save calibration reference").click().run()
        assert not app.exception
    result=app.session_state.current_measurements
    assert result.assessments[0].status=="cross_checked"
    assert all(r.selection_method=="preview_click" and r.preview_frame_id and r.preview_points for r in result.calibration.references)
    assert [(p.x,p.y) for p in result.calibration.references[0].points]==[(100,100),(500,100)]
    assert next(m.value_si for m in result.measurements if m.candidate_type=="room")==6


def test_download_contains_current_measurements_and_reference_provenance(monkeypatch):
    exports=[]
    original=st.download_button
    def capture(*args,**kwargs):
        exports.append(json.loads(kwargs["data"]))
        return original(*args,**kwargs)
    monkeypatch.setattr(st,"download_button",capture)
    app=calibrated_app()
    exported=exports[-1]
    assert exported["artifact_type"]=="candidate_measurement_evidence_not_engineering_approved"
    assert MeasurementSet.model_validate(exported["measurement_review"])==app.session_state.current_measurements
    assert len(exported["measurement_review"]["calibration"]["references"])==2
    assert all(not m["engineering_approved"] and m["source_geometry"]["coordinates"] for m in exported["measurement_review"]["measurements"])
