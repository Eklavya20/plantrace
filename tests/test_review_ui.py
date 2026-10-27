from io import BytesIO
import json
from pathlib import Path

import streamlit as st
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / "app.py"


def example_app():
    app = AppTest.from_file(str(APP)).run()
    app.checkbox[0].check().run()
    assert not app.exception
    return app


def automatic_result(app):
    return list(app.session_state.candidate_cache.values())[-1]


def selection(app):
    return next(item for item in app.selectbox if item.label == "Select candidate to highlight")


def metric(app, label):
    return next(item.value for item in app.metric if item.label == label)


def test_real_synthetic_candidates_can_be_selected_and_highlighted():
    app = example_app()
    assert metric(app, "Automatic wall candidates") == "8"
    assert metric(app, "Automatic room candidates") == "2"
    assert metric(app, "Automatic opening candidates") == "1"
    overview_image = app.get("image")[0].value
    room = automatic_result(app).list_candidates("room")[0]
    selection(app).select(room.id).run()
    assert not app.exception
    assert app.get("image")[0].value != overview_image
    record = next(json.loads(item.value) for item in app.json if json.loads(item.value).get("id") == room.id)
    assert record["origin"] == "automatic"
    assert record["status"] == "provisional"
    assert record["evidence"]
    assert record["boundary_bridge_ids"]
    assert any("missing-ink boundary hypotheses" in warning.value for warning in app.warning)
    app.multiselect[0].set_value(["wall"]).run()
    assert not app.exception
    assert selection(app).value is None
    assert metric(app, "Automatic room candidates") == "2"


def test_settings_disable_hypothetical_split_and_never_fall_back_to_demo():
    app = example_app()
    next(item for item in app.checkbox if item.label == "Use supported opening hypotheses for room boundaries").uncheck().run()
    assert not app.exception
    assert metric(app, "Automatic room candidates") == "1"
    assert "unclosed_interior_partition" in automatic_result(app).list_candidates("room")[0].flags
    next(item for item in app.number_input if item.label == "Minimum wall stroke width").set_value(5.0).run()
    assert not app.exception
    assert metric(app, "Automatic wall candidates") == "0"
    assert metric(app, "Automatic room candidates") == "0"
    assert any("No candidates detected" in warning.value for warning in app.warning)
    assert len(app.get("image")) == 1


def test_actual_uploaded_pdf_uses_same_candidate_pipeline(monkeypatch, pdf_bytes):
    upload = BytesIO(pdf_bytes)
    upload.name = "uploaded_plan.pdf"
    monkeypatch.setattr(st, "file_uploader", lambda *args, **kwargs: upload)
    app = AppTest.from_file(str(APP)).run()
    assert not app.exception
    assert metric(app, "Automatic room candidates") == "2"
    assert automatic_result(app).candidates[0].evidence[0].source.page_number == 1


def test_manual_edit_is_selected_unverified_and_keeps_automatic_counts():
    app = example_app()
    original = automatic_result(app).list_candidates("room")[0]
    selection(app).select(original.id).run()
    app.text_area[0].input(json.dumps([[110, 100], [300, 100], [300, 400], [110, 400], [110, 100]]))
    next(button for button in app.button if button.label == "Save manual correction").click().run()
    assert not app.exception
    assert ":manual:" in selection(app).value
    corrected = next(json.loads(item.value) for item in app.json if json.loads(item.value).get("origin") == "manual_correction")
    assert corrected["supersedes_id"] == original.id
    assert corrected["status"] == "unresolved"
    assert corrected["evidence"] == original.model_dump(mode="json")["evidence"]
    assert metric(app, "Automatic room candidates") == "2"
    assert automatic_result(app).select_candidate(original.id) == original


def test_invalid_manual_polygon_stays_unsaved():
    app = example_app()
    original = automatic_result(app).list_candidates("room")[0]
    selection(app).select(original.id).run()
    app.text_area[0].input("[[100,100],[300,400],[100,400],[300,100],[100,100]]")
    next(button for button in app.button if button.label == "Save manual correction").click().run()
    assert not app.exception
    assert any("Correction not saved" in error.value for error in app.error)
    assert not app.session_state.manual_candidates
    assert selection(app).value == original.id


def test_all_layers_can_be_hidden_and_raster_page_reports_no_detection():
    app = example_app()
    app.multiselect[0].set_value([]).run()
    assert not app.exception
    assert selection(app).options == ["Overview (all visible candidates)"]
    next(item for item in app.selectbox if item.label == "Page").select(2).run()
    assert not app.exception
    assert metric(app, "Automatic room candidates") == "0"
    assert any("No candidates detected" in warning.value for warning in app.warning)
