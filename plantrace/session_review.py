"""Bridge existing Streamlit review state to durable page snapshots."""

import streamlit as st

from plantrace.assembly_models import CorrectionContext, ReviewWorkspace
from plantrace.calibration import digest,new_book
from plantrace.candidate_models import DetectionConfig
from plantrace.measurements import measure_candidates
from plantrace.repository import PersistenceError,ReviewStore
from plantrace.source_audit import audit_source_lines


def restore_page_review(store,inspection,state=None):
    state=st.session_state if state is None else state
    scope=(inspection.document_id,inspection.page.page_number)
    hydrated=state.setdefault("hydrated_review_pages",set())
    initial=state.setdefault("initial_detection_configs",{})
    if scope in hydrated:
        return initial.get(scope,DetectionConfig())
    stored=store.load_workspace(*scope)
    state.setdefault("review_revisions",{})[scope]=stored.revision if stored else 0
    config=stored.workspace.candidates.config if stored else DetectionConfig()
    initial[scope]=config
    if stored:
        workspace=stored.workspace
        state.setdefault("calibration_books",{})[scope]=workspace.calibration
        state.setdefault("measurement_approvals",{})[scope]=workspace.measurement_approvals
        state.setdefault("geometry_revision",{})[scope]=workspace.geometry_revision
        for context in workspace.corrections:
            state.setdefault("manual_candidates",{})[(scope[0],scope[1],context.config.model_dump_json())]={c.supersedes_id or c.id:c for c in context.candidates}
    hydrated.add(scope)
    return config


def activate_review(store,inspection,candidates):
    st.session_state.active_review_context={"path":str(store.path),"inspection":inspection,"candidates":candidates}


def capture_workspace(inspection,candidates,state) -> ReviewWorkspace:
    scope=(inspection.document_id,inspection.page.page_number)
    key=(scope[0],scope[1],candidates.config.model_dump_json())
    manual=state.setdefault("manual_candidates",{})
    corrections=list(manual.get(key,{}).values())
    automatic=[candidate for candidate in candidates.candidates if candidate.origin=="automatic"]
    scene=candidates.model_copy(update={"candidates":automatic+corrections})
    scene=scene.model_copy(update={"source_line_audit":audit_source_lines(inspection,scene)})
    contexts=[CorrectionContext(config=DetectionConfig.model_validate_json(k[2]),candidates=list(values.values()))
              for k,values in manual.items() if k[:2]==scope]
    book=state.setdefault("calibration_books",{}).setdefault(scope,new_book(inspection))
    epoch=state.setdefault("geometry_revision",{}).get(scope,0)
    approvals=state.setdefault("measurement_approvals",{}).get(scope,[])
    return ReviewWorkspace(document=state["document"],inspection=inspection,candidates=scene,calibration=book,
        geometry_revision=epoch,corrections=contexts,measurement_approvals=approvals,
        measurements=measure_candidates(scene,book,epoch,approvals))


def persist_active_review(action="workspace_saved",actor="Unspecified local editor",note=""):
    context=st.session_state.get("active_review_context")
    if context is None:
        return None
    inspection=context["inspection"]
    scope=(inspection.document_id,inspection.page.page_number)
    workspace=capture_workspace(inspection,context["candidates"],st.session_state)
    try:
        saved=ReviewStore(context["path"]).save_workspace(workspace,
            expected_revision=st.session_state.setdefault("review_revisions",{}).get(scope,0),
            action=action,actor=actor,note=note)
        st.session_state.review_revisions[scope]=saved.revision
        return saved
    except PersistenceError as exc:
        st.error(str(exc))
        st.info("The in-memory change was not saved. Use Reload saved review to load the current database state.")
        st.stop()
