from io import BytesIO
from pathlib import Path
import json

from streamlit.testing.v1 import AppTest
import streamlit as st

APP = Path(__file__).resolve().parents[1] / "app.py"


def test_empty_app_has_upload_and_scope():
    app = AppTest.from_file(str(APP)).run()
    assert not app.exception
    assert app.title[0].value == "PlanTrace"
    assert "Start with a floor plan" in app.subheader[0].value
    assert len(app.get("file_uploader")) == 1


def test_example_selection_page_switch_and_path_selection():
    app = AppTest.from_file(str(APP)).run()
    app.checkbox[0].check().run()
    assert not app.exception
    assert app.metric[2].value == "5"
    path_selector = next(selector for selector in app.selectbox if selector.label == "Inspect a raw path")
    last_id = path_selector.value.rsplit("path", 1)[0] + "path4"
    path_selector.select(last_id).run()
    assert not app.exception
    assert any(json.loads(element.value).get("id") == last_id for element in app.json)
    next(selector for selector in app.selectbox if selector.label == "Page").select(2).run()
    assert not app.exception
    assert app.metric[1].value == "Raster"
    assert app.metric[2].value == "0"
    assert len(app.get("image")) == 1
    assert len(app.get("download_button")) == 1


def test_uploaded_raster_uses_real_upload_branch(monkeypatch, png_bytes):
    upload = BytesIO(png_bytes)
    upload.name = "uploaded.png"
    monkeypatch.setattr(st, "file_uploader", lambda *args, **kwargs: upload)
    app = AppTest.from_file(str(APP)).run()
    assert not app.exception
    assert app.metric[1].value == "Raster"
    assert app.metric[2].value == "0"
    assert app.subheader[0].value == "uploaded.png"


def test_invalid_upload_shows_error_without_traceback(monkeypatch):
    upload = BytesIO(b"broken")
    upload.name = "broken.pdf"
    monkeypatch.setattr(st, "file_uploader", lambda *args, **kwargs: upload)
    app = AppTest.from_file(str(APP)).run()
    assert not app.exception
    assert "contents do not match" in app.error[0].value
