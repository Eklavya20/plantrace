from streamlit.testing.v1 import AppTest
from test_metrology_ui import APP,item,example_app,add_reference
from test_persistent_ui import saved_app


def test_units_require_explicit_choice_and_assumed_agreement_stays_unverified():
    app=example_app()
    item(app,"radio","Reference point entry").set_value("Source coordinates").run()
    assert item(app,"selectbox","Known distance unit").value is None
    item(app,"text_input","Known dimension label").input("No unit provided")
    item(app,"button","Save calibration reference").click().run()
    assert not app.exception
    assert any("explicit distance unit" in e.value for e in app.error)
    assert app.session_state.current_measurements.calibration.references==[]
    add_reference(app,[(100,100),(500,100)],4,"Assumed horizontal")
    add_reference(app,[(100,100),(100,400)],3,"Assumed vertical")
    values=app.session_state.current_measurements
    assert values.assessments[0].status=="cross_checked"
    assert values.assessments[0].correctness_status=="unverified"
    assert all(m.verification_state=="needs_review" for m in values.measurements)
    assert all(r.establishment.kind=="manual_assumption" for r in values.calibration.references)
    assert all(r.establishment.unit_basis=="assumed" for r in values.calibration.references)
    selected=next(m for m in values.measurements if m.candidate_type=="wall")
    item(app,"selectbox","Select measurement to inspect").select(selected.id).run()
    assert item(app,"button","Record manual measurement approval").disabled
    restored=saved_app(values.document_id).session_state.current_measurements
    assert restored.calibration==values.calibration
    assert restored.assessments[0].correctness_status=="unverified"
