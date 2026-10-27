"""Stage 2 controls layered onto the existing Stage 1 Streamlit workflow."""

from hashlib import sha256
import json

import streamlit as st

from plantrace.candidate_models import Candidate, CandidateSet, DetectionConfig
from plantrace.candidates import extract_candidates
from plantrace.corrections import correct_candidate
from plantrace.calibration import digest
from plantrace.models import PageInspection
from plantrace.session_review import persist_active_review


def _candidate_selected(scope):
    # An explicit candidate choice must take precedence over an earlier
    # measurement highlight; otherwise the room correction form is unreachable.
    st.session_state[f"measurement_selection_{digest(scope)[:16]}"]=None
    st.session_state[f"last_measurement_candidate_{scope}"]=None


def candidate_controls(inspection: PageInspection,initial_config: DetectionConfig | None=None) -> tuple[CandidateSet, Candidate | None, tuple[str, ...]]:
    initial_config=initial_config or DetectionConfig()
    token=digest((inspection.document_id,inspection.page.page_number,st.session_state.get("review_ui_epoch",0)))[:16]
    with st.sidebar.expander("Candidate settings (PDF points)"):
        st.caption("Drawing-unit heuristics, not real-world dimensions. Thin strokes and small enclosures may be missed.")
        stroke = st.number_input("Minimum wall stroke width", min_value=0.0, max_value=100.0, value=initial_config.min_stroke_width, step=0.5,key=f"detect_min_stroke_width_{token}")
        run = st.number_input("Minimum isolated run length", min_value=1.0, max_value=1000.0, value=initial_config.min_segment_length, step=1.0,key=f"detect_min_segment_length_{token}")
        gap = st.number_input("Endpoint gap tolerance", min_value=0.0, max_value=20.0, value=initial_config.endpoint_gap_tolerance, step=0.5,key=f"detect_endpoint_gap_tolerance_{token}")
        area = st.number_input("Minimum enclosure area (drawing units squared)", min_value=1.0, value=initial_config.min_room_area, step=500.0,key=f"detect_min_room_area_{token}")
        span = st.number_input("Minimum enclosure span", min_value=1.0, value=initial_config.min_room_span, step=5.0,key=f"detect_min_room_span_{token}")
        opening = st.number_input("Maximum possible opening gap", min_value=12.0, max_value=500.0, value=initial_config.max_opening_gap, step=5.0,key=f"detect_max_opening_gap_{token}")
        use_cues = st.checkbox("Use supported opening hypotheses for room boundaries", value=initial_config.use_opening_hypotheses,key=f"detect_use_opening_hypotheses_{token}")
        st.caption("A facing gap plus a compatible curve can form a dashed provisional closure. Disable this to inspect only observed enclosures and tolerance repairs.")
    config = DetectionConfig(
        min_stroke_width=stroke, min_segment_length=run, endpoint_gap_tolerance=gap,
        min_room_area=area, min_room_span=span, max_opening_gap=opening,
        use_opening_hypotheses=use_cues,
    )
    key = (inspection.document_id, inspection.page.page_number, config.model_dump_json())
    cache = st.session_state.setdefault("candidate_cache", {})
    if key not in cache:
        with st.spinner("Reconstructing provisional wall runs and enclosed regions..."):
            cache[key] = extract_candidates(inspection, config)
        while len(cache) > 3:
            del cache[next(iter(cache))]
    automatic = cache[key]
    corrections = st.session_state.setdefault("manual_candidates", {}).get(key, {})
    result = automatic.model_copy(update={"candidates": automatic.candidates + list(corrections.values())})
    if corrections:
        from plantrace.source_audit import audit_source_lines
        result=result.model_copy(update={"source_line_audit":audit_source_lines(inspection,result)})
    counts = st.columns(3)
    for column, kind in zip(counts, ["wall", "room", "opening"]):
        column.metric(f"Automatic {kind} candidates", len(automatic.list_candidates(kind)))
    if corrections:
        st.caption(f"Manual corrections: {len(corrections)}. Automatic counts above retain the original detections; both versions remain selectable.")
    st.caption("All results remain unverified. Teal: wall runs · blue: room regions · orange dashes: gap hypotheses (rings mark short gaps) · purple: manual correction · pink: selected candidate.")
    controls = st.columns([1, 2])
    with controls[0]:
        visible = tuple(st.multiselect("Overlay layers", ["wall", "room", "opening"], default=["wall", "room", "opening"]))
    with controls[1]:
        choices = [c for c in result.candidates if c.candidate_type in visible]
        labels = {c.id: f"{c.candidate_type.title()} {index + 1} | {c.status} | {c.origin}" for kind in ["wall", "room", "opening"] for index, c in enumerate(result.list_candidates(kind))}
        fingerprint = sha256((repr(key) + repr(visible)).encode()).hexdigest()[:16]
        widget_key = f"candidate_{fingerprint}"
        pending = st.session_state.pop("pending_manual_selection", None)
        if pending is not None and pending in [c.id for c in choices]:
            st.session_state[widget_key] = pending
        candidate_id = st.selectbox(
            "Select candidate to highlight", [None] + [c.id for c in choices],
            format_func=lambda value: "Overview (all visible candidates)" if value is None else labels[value],
            key=widget_key,on_change=_candidate_selected,args=((result.document_id,result.page_number),),
        )
    selected = result.select_candidate(candidate_id) if candidate_id is not None else None
    if not result.candidates:
        st.warning("No candidates detected with these settings. Review the source evidence and diagnostics below.")
    return result, selected, visible


def candidate_details(result: CandidateSet, selected: Candidate | None) -> None:
    st.markdown("**Candidate review**")
    if selected is None:
        st.write("Select a candidate above to highlight its geometry and inspect its source evidence.")
    else:
        if selected.status == "unresolved":
            st.warning("Unresolved structural interpretation. Source strokes remain selectable; this candidate cannot receive measurement approval.")
        if selected.boundary_bridge_ids:
            st.warning("This polygon uses inferred missing-ink boundaries, shown as dashed overlays. Its area remains unverified.")
        st.write(f"{selected.candidate_type.title()} · {selected.status} · {selected.origin}")
        st.write(selected.explanation)
        st.caption("Flags: " + ", ".join(selected.flags))
        st.json(selected.model_dump(mode="json"), expanded=False)
        if selected.boundary_bridge_ids:
            st.warning("This room uses missing-ink boundary hypotheses, shown as dashes. Its polygon is not an observed closed source path.")
            with st.expander("Boundary hypothesis evidence"):
                st.json({"boundary_bridges": [b.model_dump(mode="json") for b in result.bridges if b.id in selected.boundary_bridge_ids]})


def candidate_diagnostics(result: CandidateSet) -> None:
    if result.source_line_audit:
        audit=result.source_line_audit
        with st.expander("Source line coverage and missing-structure review"):
            st.warning("Structural completeness is not established. A wholly absent partition has no source ink to detect. Furniture and annotation interpretations remain hypotheses.")
            st.write(f"Audit: {audit.status}; {len(audit.lines)} straight source edges; {audit.unassessed_curve_commands} curve commands unassessed.")
            st.dataframe([{"source path":line.evidence.path_id,"classification":line.interpretation,
                "unexplained drawing length":line.unexplained_length,"dashed":line.dashed,"rooms":", ".join(line.affected_room_ids)}
                for line in audit.lines if line.interpretation!="represented"],hide_index=True)
            st.json(audit.model_dump(mode="json"),expanded=False)
    with st.expander(f"Unresolved and filtered geometry ({len(result.diagnostics)})"):
        if result.diagnostics:
            st.dataframe([
                {"code": d.code, "message": d.message, "source paths": ", ".join(d.path_ids), "candidates": ", ".join(d.candidate_ids)}
                for d in result.diagnostics
            ], hide_index=True)
        else:
            st.write("No topology/filter diagnostics. Candidates still require review.")
        st.json(result.config.model_dump(), expanded=False)


def manual_correction_form(inspection: PageInspection, result: CandidateSet, selected: Candidate | None) -> None:
    if selected is None or selected.candidate_type == "opening":
        return
    with st.expander("Manual coordinate correction"):
        st.caption("Corrections retain source lineage. After a wall edit, an original room can be explicitly rebuilt from current walls if its closed face is unambiguous. Values remain unverified.")
        if selected.origin != "automatic":
            st.write("Select the original automatic candidate to replace this correction. Source evidence remains unchanged.")
            return
        if selected.candidate_type=="room":
            with st.form(f"rebuild_{selected.id}"):
                st.caption("Rebuild only from current wall geometry and existing valid gap hypotheses. Open boundaries and ambiguous merges/splits are rejected. This does not establish room completeness or approve its area.")
                reviewer=st.text_input("Room rebuild reviewer")
                reason=st.text_input("Room rebuild reason")
                rebuild=st.form_submit_button("Rebuild room from current walls")
            if rebuild:
                try:
                    from plantrace.room_rebuild import rebuild_room
                    replacement=rebuild_room(result,selected.id,inspection.page,reviewer,reason)
                except (ValueError,KeyError) as exc:
                    st.error(f"Room not rebuilt: {exc}")
                else:
                    key=(result.document_id,result.page_number,result.config.model_dump_json())
                    st.session_state.manual_candidates.setdefault(key,{})[selected.id]=replacement
                    scope=(result.document_id,result.page_number)
                    epochs=st.session_state.setdefault("geometry_revision",{})
                    epochs[scope]=epochs.get(scope,0)+1
                    st.session_state.pending_manual_selection=replacement.id
                    st.session_state.pending_measurement_candidate=replacement.id
                    persist_active_review("room_rebuilt",actor=reviewer,note=reason)
                    st.rerun()
        with st.form(f"correct_{selected.id}"):
            entered = st.text_area(
                "Corrected coordinates (source PDF points)",
                value=json.dumps([[p.x, p.y] for p in selected.geometry.coordinates]),
                help="Use [[x,y], ...]. Walls need two endpoints; room rings must repeat the first point at the end. Existing holes are retained and validated.",
            )
            submitted = st.form_submit_button("Save manual correction")
        if submitted:
            try:
                correction = correct_candidate(selected, json.loads(entered), inspection.page)
            except (ValueError, TypeError) as exc:
                st.error(f"Correction not saved: {exc}")
                return
            key = (result.document_id, result.page_number, result.config.model_dump_json())
            st.session_state.manual_candidates.setdefault(key, {})[selected.id] = correction
            scope = (result.document_id, result.page_number)
            epochs = st.session_state.setdefault("geometry_revision", {})
            epochs[scope] = epochs.get(scope, 0) + 1
            st.session_state.pending_manual_selection = correction.id
            st.session_state.pending_measurement_candidate = correction.id
            persist_active_review("geometry_corrected",note=f"Coordinate correction of {selected.id}; dependent geometry requires validation.")
            st.rerun()
