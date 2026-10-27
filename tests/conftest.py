import pytest
from pathlib import Path
from uuid import uuid4

from plantrace.demo import raster_plan, vector_plan_pdf


def pytest_configure(config):
    # Keep new SQLite fixtures inside the workspace; the host's shared pytest
    # temp directory may be inaccessible in the Windows sandbox.
    if config.option.basetemp is None:
        root=Path(__file__).resolve().parents[1]
        target=root/"support"/"artifacts"/"pytest"/f"run-{uuid4().hex}"
        assert target.is_relative_to(root) and not target.exists()
        target.parent.mkdir(parents=True,exist_ok=True)
        config.option.basetemp=str(target)


@pytest.fixture(autouse=True)
def isolate_review_database(monkeypatch,tmp_path):
    # App restarts within one test share a database; different tests do not.
    monkeypatch.setenv("PLANTRACE_DB_PATH",str(tmp_path/"app_reviews.sqlite3"))


@pytest.fixture
def pdf_bytes() -> bytes:
    return vector_plan_pdf()


@pytest.fixture
def png_bytes() -> bytes:
    return raster_plan()
