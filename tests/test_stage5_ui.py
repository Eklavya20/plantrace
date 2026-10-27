"""Programmatic Streamlit upload/picker QA; no browser file dialog is tested."""
import json

from streamlit.runtime.uploaded_file_manager import UploadedFile,UploadedFileRec
from streamlit.testing.v1 import AppTest
import streamlit as st

from plantrace.repository import ReviewStore,database_path
from plantrace.assembly import PlanTraceService
from test_metrology_ui import APP,item,add_reference
from test_persistent_ui import saved_app


def test_upload_picker_conflict_edit_assemble_export_and_restart(monkeypatch):
    data=(APP.parent/"examples/synthetic_plan.pdf").read_bytes()
    upload=UploadedFile(UploadedFileRec("qa-upload","uploaded_plan.pdf","application/pdf",data),None)
    exports=[]
    real_download=st.download_button
    def capture(*args,**kwargs):
        exports.append(json.loads(kwargs["data"]))
        return real_download(*args,**kwargs)
    monkeypatch.setattr(st,"download_button",capture)
    with monkeypatch.context() as transport:
        transport.setattr(st,"file_uploader",lambda *a,**kw:upload)
        app=AppTest.from_file(str(APP),default_timeout=15).run()
        assert not app.exception
        assert app.session_state.document.filename=="uploaded_plan.pdf"
        scene=list(app.session_state.candidate_cache.values())[-1]
        assert len(scene.candidates)==11
        wall=next(c for c in scene.list_candidates("wall") if c.geometry.as_shapely().bounds==(100,100,100,400))
        item(app,"selectbox","Select candidate to highlight").select(wall.id).run()
        for coordinates,distance,label in [([(100,100),(500,100)],4,"QA horizontal"), ([(100,100),(100,400)],3,"QA vertical")]:
            args=json.loads(app.get("component_instance")[0].proto.json_args)
            a,b,c,d,e,f=args["transform"]
            app.session_state["picker_"+args["frame"]]={"frame":args["frame"],"reset":args["reset"],"region_id":args["region"]["id"],
                "points":[{"x":a*x+c*y+e,"y":b*x+d*y+f} for x,y in coordinates]}
            app.run()
            item(app,"number_input","Known real-world distance").set_value(float(distance))
            item(app,"selectbox","Known distance unit").select("m")
            item(app,"text_input","Known dimension label").input(label)
            item(app,"button","Save calibration reference").click().run()
        values=app.session_state.current_measurements
        assert values.assessments[0].status=="cross_checked"
        assert values.assessments[0].correctness_status=="unverified"
        assert all(r.selection_method=="preview_click" for r in values.calibration.references)
        selected=next(m for m in values.measurements if m.candidate_id==wall.id)
        assert selected.value_si==3 and selected.source_evidence
        item(app,"selectbox","Select measurement to inspect").select(selected.id).run()
        assert item(app,"button","Record manual measurement approval").disabled
        add_reference(app,[(100,100),(500,400)],5.5,"Injected conflicting diagonal")
        assert app.session_state.current_measurements.assessments[0].status=="conflicting"
        assert item(app,"button","Record manual measurement approval").disabled
        assert any("Conflicting calibration" in error.value for error in app.error)
        item(app,"selectbox","Select candidate to highlight").select(wall.id).run()
        item(app,"text_area","Corrected coordinates (source PDF points)").input("[[110,100],[110,400]]")
        item(app,"button","Save manual correction").click().run()
        assert not app.exception
        values=app.session_state.current_measurements
        stale=[m for m in values.measurements if m.candidate_type=="room" and m.boundary_support=="stale"]
        assert len(stale)==1 and stale[0].value_si is None
        assert any(i.code=="unsupported_room_boundary" for i in app.session_state.current_topology.issues)
        correction=next(m for m in values.measurements if m.candidate_origin=="manual_correction")
        item(app,"text_input","Architect/reviewer name").input("Stage 5 QA actor").run()
        item(app,"selectbox","Candidate for architect decision").select(correction.candidate_id).run()
        item(app,"text_input","Architect decision note").input("Provisional correction; stale room remains blocked")
        item(app,"button","Save architect decision").click().run()
        item(app,"multiselect","Candidates to commit as provisional").set_value([correction.candidate_id])
        item(app,"button","Commit provisional entities").click().run()
        assert not app.exception
        exported=next(e for e in reversed(exports) if e["artifact_type"]=="versioned_provisional_building_model")
        model=exported["model"]
        assert model["source_review"]["geometry_revision"]==1
        assert len(model["source_review"]["calibration"]["references"])==3
        assert len(model["source_review"]["corrections"])==1
        assert model["entities"][0]["measurement"]["verification_state"]=="needs_review"
        assert not model["entities"][0]["engineering_handoff_eligible"]
        document_id=values.document_id
    restarted=saved_app(document_id)
    assert restarted.session_state.current_building_model==exported
    assert restarted.session_state.current_measurements.assessments[0].status=="conflicting"
    store=ReviewStore(database_path())
    assert store.load_source(document_id)[0]==data
    assert len(PlanTraceService(store,document_id,1).context().decisions)==1


def test_concurrent_review_buttons_reject_a_stale_view():
    from test_metrology_ui import calibrated_app
    app=calibrated_app()
    document_id=app.session_state.current_measurements.document_id
    wall=next(c for c in list(app.session_state.candidate_cache.values())[-1].candidates if c.candidate_type=="wall")
    item(app,"text_input","Architect/reviewer name").input("Reviewer B").run()
    item(app,"selectbox","Candidate for architect decision").select(wall.id).run()
    item(app,"text_input","Architect decision note").input("Review current geometry")
    concurrent=PlanTraceService(ReviewStore(database_path()),document_id,1)
    assert item(app,"selectbox","Candidate for architect decision").value==wall.id
    assert not item(app,"button","Save architect decision").disabled
    assert item(app,"text_input","Architect/reviewer name").value=="Reviewer B"
    assert item(app,"text_input","Architect decision note").value=="Review current geometry"
    assert app.session_state[f"assembly_view_version_{document_id}_1"]==concurrent.context().version
    concurrent.record_decision(wall.id,"needs_review","Reviewer A","Independent concurrent writer")
    assert app.session_state[f"assembly_view_version_{document_id}_1"]!=concurrent.context().version
    item(app,"button","Save architect decision").click().run()
    assert not app.exception
    assert any("Review changed in another session" in e.value for e in app.error), ([e.value for e in app.error], [d.actor for d in concurrent.context().decisions],item(app,"button","Save architect decision").value)
    context=PlanTraceService(ReviewStore(database_path()),document_id,1).context()
    assert len(context.decisions)==1 and context.decisions[0].actor=="Reviewer A"
