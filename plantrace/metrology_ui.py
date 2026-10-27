"""Scale references, regional diagnostics and revision-bound measurement review."""

import math

import pandas as pd
import streamlit as st

from plantrace.calibration import (
    add_region, assess_region, digest, make_reference, new_book, put_printed_scale,
    put_reference, remove_region, update_book,
)
from plantrace.candidate_models import CandidateSet
from plantrace.measurements import approval_blockers, approve_measurement, measure_candidates
from plantrace.metrology_models import CalibrationBook, Measurement, MeasurementSet, UNIT_TO_METRES, ReferenceEstablishment
from plantrace.models import PageInspection, Point, Preview
from plantrace.point_picker import pick_reference_points, picker_frame
from plantrace.session_review import persist_active_review


def _scope(inspection: PageInspection) -> tuple[str, int]:
    return inspection.document_id, inspection.page.page_number


def _save_book(scope: tuple, book: CalibrationBook) -> None:
    st.session_state.calibration_books[scope] = book
    persist_active_review("calibration_updated",note=f"Calibration revision {book.revision}")
    st.rerun()


def calibration_controls(inspection: PageInspection, preview: Preview) -> CalibrationBook:
    scope = _scope(inspection)
    token = digest((scope,st.session_state.get("review_ui_epoch",0)))[:16]
    books = st.session_state.setdefault("calibration_books", {})
    book = books.setdefault(scope, new_book(inspection))
    with st.expander("Scale calibration and regions"):
        st.caption("Associate known dimensions manually. Cross-checking compares these references; it does not approve the drawing or its engineering use.")
        tolerance = st.number_input("Scale agreement diagnostic tolerance (%)", min_value=0.0, max_value=50.0,
                                    value=book.diagnostic_tolerance*100, step=0.5, key=f"scale_tolerance_{token}")
        if not math.isclose(tolerance / 100, book.diagnostic_tolerance, abs_tol=1e-12):
            _save_book(scope, update_book(book, diagnostic_tolerance=tolerance / 100))
        region_key = f"scale_region_{token}"
        region_ids = [r.id for r in book.regions]
        pending = st.session_state.pop("pending_scale_region", None)
        if pending in region_ids:
            st.session_state[region_key] = pending
        if st.session_state.get(region_key) not in region_ids:
            st.session_state[region_key] = region_ids[0]
        region_id = st.selectbox("Calibration region", region_ids,
                                 format_func=lambda rid: next(f"{index+1}. {r.name}" for index,r in enumerate(book.regions) if r.id == rid), key=region_key)
        region = next(r for r in book.regions if r.id == region_id)
        reference_tab, region_tab, printed_tab = st.tabs(["Known dimensions", "Scale regions", "Printed hypothesis"])
        with reference_tab:
            _reference_controls(inspection, preview, book, region_id, token)
        with region_tab:
            st.caption("Explicit rectangles override the page fallback only for geometry fully contained within one region. Crossing or overlapping regions block a single-scale SI result.")
            st.dataframe([{"name":r.name,"scope":r.scope,"bounds":str(r.bounds)} for r in book.regions], hide_index=True)
            with st.form(f"new_scale_region_{token}"):
                name = st.text_input("New region name")
                columns = st.columns(4)
                bounds = []
                for column, label, default, maximum in zip(columns,
                    ["Region X min","Region Y min","Region X max","Region Y max"],
                    [0.0,0.0,inspection.page.width,inspection.page.height],
                    [inspection.page.width,inspection.page.height,inspection.page.width,inspection.page.height]):
                    with column:
                        bounds.append(st.number_input(label,min_value=0.0,max_value=float(maximum),value=float(default)))
                add = st.form_submit_button("Add scale region")
            if add:
                try:
                    changed = add_region(book,name,tuple(bounds))
                    st.session_state.pending_scale_region = changed.regions[-1].id
                    _save_book(scope,changed)
                except ValueError as exc:
                    st.error(str(exc))
            if st.button("Remove selected scale region",disabled=region.scope=="page",key=f"remove_region_{token}"):
                _save_book(scope,remove_region(book,region_id))
        with printed_tab:
            hypothesis = next((h for h in book.printed_scales if h.region_id==region_id),None)
            st.warning("Printed 1:N depends on the original reproduction size. It remains a hypothesis and never substitutes for an independent known-dimension check.")
            with st.form(f"printed_scale_{token}_{region_id}"):
                denominator = st.number_input("Printed scale denominator N",min_value=0.000001,value=hypothesis.denominator if hypothesis else 100.0)
                st.caption("Known-distance references take precedence over a printed hypothesis.")
                set_printed = st.form_submit_button("Store printed-scale hypothesis",disabled=region.source_units!="pdf_point")
            if set_printed:
                _save_book(scope,put_printed_scale(book,region_id,denominator))
            if st.button("Remove printed-scale hypothesis",disabled=hypothesis is None,key=f"clear_printed_{token}_{region_id}"):
                _save_book(scope,put_printed_scale(book,region_id,None))
        assessment = assess_region(book,region)
        st.write(f"Selected region: **{assessment.status}** (internal reference agreement)")
        st.write(f"Independent scale/unit correctness: **{assessment.correctness_status}**")
        if assessment.residuals:
            st.dataframe([{
                "dimension": next(r.dimension_label for r in assessment.references if r.id==res.reference_id),
                "entered": f"{res.distance_entered:g} {res.unit_entered}", "known m":res.known_metres,
                "predicted m":res.predicted_metres,"residual m":res.residual_metres,
                "relative residual %":res.relative_residual*100,"distinct span from anchor":res.comparison_span_distinct,
                "independent physical evidence from anchor":res.evidence_independent_of_primary,
            } for res in assessment.residuals],hide_index=True)
        st.json({"calibration_assessment":assessment.model_dump(mode="json")},expanded=False)
    assessments = [assess_region(book,r) for r in book.regions]
    if all(a.status=="missing" for a in assessments):
        st.warning("Missing calibration: SI values are unavailable. Add known dimensions under Scale calibration and regions.")
    if any(a.status=="conflicting" for a in assessments):
        names=", ".join(a.region.name for a in assessments if a.status=="conflicting")
        st.error(f"Conflicting calibration references in {names}. Affected SI values are nominal and unverified; manual approval is blocked.")
    if any(a.status=="hypothesis_only" for a in assessments):
        st.warning("Printed-scale-only values are hypotheses and cannot be approved.")
    if any(a.status=="invalid" for a in assessments):
        st.error("Calibration references no longer match their region scopes. Move, remove or re-associate those references before using affected SI values.")
    if any(a.references and a.correctness_status=="unverified" for a in assessments):
        st.warning("Independent scale/unit correctness is unverified. Agreeing assumptions or annotations from one drawing do not establish independent proof. SI values remain provisional; measurement approval is blocked.")
    if any(a.evidence_conflicts for a in assessments):
        st.error("Independent physical reference evidence conflicts beyond the measurement-review agreement limit. A looser diagnostic tolerance cannot enable approval.")
    st.caption("Scale status: "+" | ".join(f"{a.region.name}: {a.status}"+(f" ({a.metres_per_source_unit:.8g} m/source unit)" if a.metres_per_source_unit is not None else "") for a in assessments))
    return book


def _reference_controls(inspection: PageInspection, preview: Preview, book: CalibrationBook, region_id: str, token: str) -> None:
    scope = _scope(inspection)
    refs = [r for r in book.references if r.region_id==region_id]
    options=["new"]+[r.id for r in refs]
    edit_key=f"reference_editor_{token}_{region_id}"
    if st.session_state.get(edit_key) not in options:
        st.session_state[edit_key]="new"
    editing_id=st.selectbox("Reference to add or revise",options,
                             format_func=lambda rid:"Add a new reference" if rid=="new" else next(f"{index+1}. {r.dimension_label}" for index,r in enumerate(refs) if r.id==rid),key=edit_key)
    editing=next((r for r in refs if r.id==editing_id),None)
    method=st.radio("Reference point entry",["Click preview","Source coordinates"],horizontal=True,key=f"point_entry_{token}")
    region=next(r for r in book.regions if r.id==region_id)
    picker_context=(region_id,editing_id)
    if st.session_state.get(f"picker_context_{token}")!=picker_context:
        st.session_state[f"picker_context_{token}"]=picker_context
        st.session_state[f"picker_reset_{token}"]=st.session_state.get(f"picker_reset_{token}",0)+1
    if st.button("Clear preview point selection",key=f"clear_picker_{token}"):
        st.session_state[f"picker_reset_{token}"]+=1
    points=None
    reused_stored_points=False
    selection_method="manual_source_coordinates"
    if method=="Click preview":
        try:
            points=pick_reference_points(inspection,preview,region,[assess_region(book,r) for r in book.regions],st.session_state[f"picker_reset_{token}"])
        except ValueError as exc:
            st.error(str(exc))
        if points is not None:
            selection_method="preview_click"
            st.caption("Selected source points: "+str([(p.x,p.y) for p in points]))
        elif editing:
            points=editing.points
            reused_stored_points=True
            selection_method=editing.selection_method
            st.caption("Editing the stored endpoints. Select two new points to replace them.")
        else:
            st.caption("Select two endpoints on the preview, then enter the known distance below.")
    with st.form(f"calibration_reference_{token}_{region_id}_{editing_id}"):
        if method=="Source coordinates":
            defaults=[editing.points[0].x,editing.points[0].y,editing.points[1].x,editing.points[1].y] if editing else [0.0,0.0,1.0,0.0]
            columns=st.columns(4)
            values=[]
            for column,label,value,maximum in zip(columns,["Reference X1","Reference Y1","Reference X2","Reference Y2"],defaults,
                                                   [inspection.page.width,inspection.page.height,inspection.page.width,inspection.page.height]):
                with column:
                    values.append(st.number_input(label,min_value=0.0,max_value=float(maximum),value=float(value)))
            points=(Point(x=values[0],y=values[1]),Point(x=values[2],y=values[3]))
            selection_method="manual_source_coordinates"
        label=st.text_input("Known dimension label",value=editing.dimension_label if editing else "",help="Use distinct labels for genuinely independent known dimensions; duplicate/overlapping spans are not an independent comparison.")
        distance=st.number_input("Known real-world distance",min_value=0.000001,value=editing.known_distance if editing else 1.0,format="%.6f")
        units=list(UNIT_TO_METRES)
        unit=st.selectbox("Known distance unit",units,index=units.index(editing.unit) if editing else None,placeholder="Select an explicit unit")
        note=st.text_input("Reference evidence note",value=editing.note if editing else "",help="Describe the manually associated source dimension or external measurement. No OCR association is implied.")
        evidence=editing.establishment if editing else None
        kinds=["manual_assumption","source_annotation","independently_verified_physical"]
        kind=st.selectbox("How was this reference established?",kinds,index=kinds.index(evidence.kind) if evidence else 0)
        group=st.text_input("Shared evidence/error-source group",value=evidence.independence_group if evidence else f"drawing:{inspection.document_id}",help="Use the same group for dimensions from the same survey, instrument/session or other common error source. Different dimension labels do not establish independence.")
        record=st.text_input("Evidence record or source annotation locator",value=evidence.evidence_record if evidence else "Manual assumption; no independent evidence supplied")
        evidence_hash=st.text_input("Evidence SHA-256 (required for physical verification)",value=evidence.evidence_sha256 or "" if evidence else "")
        established_by=st.text_input("Reference established by",value=evidence.established_by if evidence else "Unspecified local user")
        established_at=st.text_input("Evidence date or record timestamp",value=evidence.established_at if evidence else "Not recorded (assumption)")
        bases=["assumed","explicit_source_annotation","explicit_physical_reference"]
        unit_basis=st.selectbox("How were the units established?",bases,index=bases.index(evidence.unit_basis) if evidence else 0)
        unit_statement=st.text_input("Unit evidence or assumption",value=evidence.unit_statement if evidence else "User-entered unit assumption; not independently checked")
        st.caption("Physical evidence metadata is a manual declaration, not authentication. Reusing a survey/error-source group or evidence artifact cannot establish independence. Room completeness remains unresolved even with supported scale.")
        save=st.form_submit_button("Save calibration reference")
    if save:
        try:
            if points is None:
                raise ValueError("Select two preview points before saving a reference.")
            if unit is None:
                raise ValueError("Select an explicit distance unit; no unit is assumed automatically.")
            establishment=ReferenceEstablishment(kind=kind,independence_group=group,evidence_record=record,
                evidence_sha256=evidence_hash.strip() or None,established_by=established_by,established_at=established_at,
                unit_basis=unit_basis,unit_statement=unit_statement)
            reference=make_reference(book,region_id,points,distance,unit,label,selection_method,note,
                                     preview=preview if selection_method=="preview_click" else None,
                                     frame_id=picker_frame(inspection,preview) if selection_method=="preview_click" else None,establishment=establishment)
            if editing:
                fields={**reference.model_dump(),"id":editing.id}
                if reused_stored_points:
                    fields.update({"preview_frame_id":editing.preview_frame_id,"preview_transform":editing.preview_transform,"preview_points":editing.preview_points})
                reference=type(reference).model_validate(fields)
            st.session_state[f"picker_reset_{token}"]+=1
            _save_book(scope,put_reference(book,reference))
        except ValueError as exc:
            st.error(f"Reference not saved: {exc}")
    if st.button("Remove selected reference",disabled=editing is None,key=f"remove_reference_{token}_{region_id}"):
        _save_book(scope,update_book(book,references=[r for r in book.references if r.id!=editing.id]))
    st.caption("The first saved reference is the scale anchor; every other reference is compared with it and with all other references. Conflicts are not averaged away.")


def measurement_controls(inspection: PageInspection, candidates: CandidateSet, book: CalibrationBook) -> tuple[MeasurementSet, Measurement | None]:
    scope=_scope(inspection)
    geometry_revision=st.session_state.setdefault("geometry_revision",{}).get(scope,0)
    approvals=st.session_state.setdefault("measurement_approvals",{}).get(scope,[])
    result=measure_candidates(candidates,book,geometry_revision,approvals)
    st.session_state.current_measurements=result
    st.markdown("**SI measurements**")
    st.caption("Blue: calculated · teal: cross-checked references · amber: needs review · green: manually approved measurement review. None is engineering-approved.")
    if result.measurements:
        rows=[{"candidate":f"{m.candidate_type.title()} {index+1}","SI value":f"{m.value_si:.6g}" if m.value_si is not None else "Unavailable",
               "unit":"m²" if m.si_unit=="m2" else "m","state":m.verification_state,
               "scale/unit correctness":m.calibration_correctness,"structure completeness":m.structure_completeness,
               "boundary support":m.boundary_support,"region":next((r.name for r in book.regions if r.id==m.region_id),"Ambiguous")}
              for index,m in enumerate(result.measurements)]
        colors={"calculated":"#e5efff","cross_checked":"#d9f1ee","needs_review":"#fff0d1","manually_approved":"#d7f1d8"}
        st.dataframe(pd.DataFrame(rows).style.map(lambda state:f"background-color: {colors[state]}",subset=["state"]),hide_index=True)
    else:
        st.info("No measurable wall or room candidates on this page.")
    ids=[m.id for m in result.measurements]
    lookup={m.id:m for m in result.measurements}
    ordinals={m.id:index+1 for index,m in enumerate(result.measurements)}
    widget_key=f"measurement_selection_{digest(scope)[:16]}"
    pending_candidate=st.session_state.pop("pending_measurement_candidate",None)
    if pending_candidate is not None:
        st.session_state[widget_key]=next((m.id for m in result.measurements if m.candidate_id==pending_candidate),None)
    elif st.session_state.get(widget_key) not in [None]+ids:
        previous=st.session_state.get(f"last_measurement_candidate_{scope}")
        st.session_state[widget_key]=next((m.id for m in result.measurements if m.candidate_id==previous),None)
    selection=st.selectbox("Select measurement to inspect",[None]+ids,
                            format_func=lambda mid:"No measurement selected" if mid is None else f"{lookup[mid].candidate_type.title()} {ordinals[mid]} | "+(f"{lookup[mid].value_si:.6g} {lookup[mid].si_unit}" if lookup[mid].value_si is not None else "SI unavailable")+f" | {lookup[mid].verification_state}",
                            key=widget_key)
    selected=lookup.get(selection)
    st.session_state[f"last_measurement_candidate_{scope}"]=selected.candidate_id if selected else None
    if any(a.status=="stale" for a in result.approvals):
        st.warning("Prior measurement approvals were invalidated by calibration or geometry changes. Their stale review records remain in the export.")
    return result,selected


def measurement_details(inspection: PageInspection, result: MeasurementSet, selected: Measurement | None) -> None:
    if selected is None:
        return
    st.markdown("**Selected measurement**")
    st.write(f"{selected.verification_state} · "+(f"{selected.value_si:.6g} {'m²' if selected.si_unit=='m2' else 'm'}" if selected.value_si is not None else "SI value unavailable"))
    st.caption("Measurement selection controls the drawing highlight. Clear it to return to candidate-only selection.")
    st.write(selected.geometric_convention)
    st.write(f"Scale/unit correctness: **{selected.calibration_correctness}**; structure completeness: **{selected.structure_completeness}**.")
    for warning in selected.warnings:
        st.caption(warning)
    st.json(selected.model_dump(mode="json"),expanded=False)
    blockers=approval_blockers(selected)
    if blockers:
        st.warning("Manual measurement approval blocked: "+" ".join(blockers))
    with st.form(f"measurement_review_{selected.id}"):
        reviewer=st.text_input("Measurement reviewer name")
        note=st.text_input("Measurement review note")
        approve=st.form_submit_button("Record manual measurement approval",disabled=bool(blockers) or selected.verification_state=="manually_approved")
        st.caption("This records an explicit review of this geometry and calibration revision. It grants no engineering approval.")
    if approve:
        try:
            approval=approve_measurement(result,selected.id,reviewer,note)
            st.session_state.measurement_approvals.setdefault(_scope(inspection),[]).append(approval)
            persist_active_review("measurement_review_approved",actor=reviewer,note=note)
            st.rerun()
        except (ValueError,KeyError) as exc:
            st.error(str(exc))
