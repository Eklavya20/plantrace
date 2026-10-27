"""Launch with: python -m streamlit run app.py."""

from pathlib import Path
import json

import streamlit as st

from plantrace.documents import DocumentError, inspect_document, inspect_page, render_page
from plantrace.overlays import render_overlay
from plantrace.review_ui import candidate_controls, candidate_details, candidate_diagnostics, manual_correction_form
from plantrace.metrology_ui import calibration_controls, measurement_controls, measurement_details
from plantrace.repository import ReviewStore,PersistenceError,database_path
from plantrace.session_review import restore_page_review,activate_review,persist_active_review
from plantrace.assembly_ui import assembly_panel

ROOT = Path(__file__).resolve().parent


def main() -> None:
    st.set_page_config(page_title="PlanTrace | Source inspection", page_icon="📐", layout="wide")
    st.title("PlanTrace")
    st.caption("Stage 6 / Independent evidence, reliability gates and persistent review")
    st.info("Review wall runs, enclosed regions and possible openings derived from source graphics. Every candidate remains unverified.")
    try:
        store=ReviewStore(database_path())
        saved_sources=store.list_sources()
    except PersistenceError as exc:
        st.error(str(exc))
        return

    with st.sidebar:
        st.header("Drawing source")
        uploaded = st.file_uploader("Upload a floor plan", type=["pdf", "png", "jpg", "jpeg"])
        demo = st.checkbox("Use synthetic example", value=False, disabled=uploaded is not None)
        saved_id=None
        if saved_sources:
            saved_id=st.selectbox("Saved drawing",[None]+[source["document_id"] for source in saved_sources],
                format_func=lambda identity:"Choose a persisted drawing" if identity is None else next(f"{source['filename']} | {identity[:8]}" for source in saved_sources if source["document_id"]==identity),
                disabled=uploaded is not None or demo)
        reload_review=st.button("Reload saved review")
        if reload_review:
            st.session_state.pop("source_bytes",None)
            st.session_state.review_ui_epoch=st.session_state.get("review_ui_epoch",0)+1
            st.rerun()
        st.caption("PDF, PNG or JPEG · up to 50 MiB · source files and review decisions are stored in local SQLite")
        st.divider()
        st.markdown("**Current scope**\n\nSource inspection, provisional candidates and selectable overlays.")
        st.caption("Known dimensions support SI calculations and explicit measurement review. No engineering approval is granted.")

    if uploaded is not None:
        data, filename = uploaded.getvalue(), uploaded.name
    elif demo:
        example = ROOT / "examples" / "synthetic_plan.pdf"
        if not example.exists():
            st.error("Example missing. Run: python scripts/generate_examples.py")
            return
        data, filename = example.read_bytes(), example.name
    elif saved_id is not None:
        try:
            data,info=store.load_source(saved_id)
            filename=info.filename
        except (PersistenceError,KeyError) as exc:
            st.error(str(exc))
            return
    else:
        st.subheader("Start with a floor plan")
        st.write("Upload a drawing or enable the synthetic example in the sidebar. Vector PDFs expose drawing paths; scanned PDFs and images provide a preview.")
        return

    try:
        # Session-local storage avoids sharing private drawing bytes through a global cache.
        if st.session_state.get("source_bytes") != data or st.session_state.get("source_name") != filename:
            document = inspect_document(data, filename)
            store.save_source(data,document)
            st.session_state.update(source_bytes=data, source_name=filename, document=document, page_results={}, candidate_cache={}, manual_candidates={},
                                    calibration_books={}, measurement_approvals={}, geometry_revision={},hydrated_review_pages=set(),
                                    initial_detection_configs={},review_revisions={},active_review_context=None)
        document = st.session_state.document
        with st.sidebar:
            page_number = st.selectbox("Page", options=range(1, len(document.pages) + 1), key=f"page_{document.document_id}")
        results = st.session_state.page_results
        if page_number not in results:
            with st.spinner("Inspecting page and extracting source paths..."):
                results[page_number] = (
                    inspect_page(data, filename, page_number),
                    render_page(data, filename, page_number),
                )
            # Bound retained renders/evidence for long multi-page documents.
            while len(results) > 3:
                del results[next(iter(results))]
        inspection, preview = results[page_number]
    except (DocumentError,PersistenceError) as exc:
        st.error(str(exc))
        return

    st.subheader(document.filename)
    metrics = st.columns(4)
    metrics[0].metric("Page", f"{page_number} / {len(document.pages)}")
    metrics[1].metric("Content", inspection.content_kind.replace("_", " ").title())
    metrics[2].metric("Vector paths", len(inspection.paths))
    metrics[3].metric("Image occurrences", inspection.image_count)

    initial_config=restore_page_review(store,inspection)
    candidates, selected_candidate, visible_types = candidate_controls(inspection,initial_config)
    activate_review(store,inspection,candidates)
    calibration = calibration_controls(inspection, preview)
    measurements, selected_measurement = measurement_controls(inspection, candidates, calibration)
    if selected_measurement is not None:
        selected_candidate = candidates.select_candidate(selected_measurement.candidate_id)
        visible_types = tuple(dict.fromkeys(visible_types + (selected_candidate.candidate_type,)))
    overlay = render_overlay(preview, candidates, selected_candidate.id if selected_candidate else None, visible_types)

    drawing, evidence = st.columns([3, 2])
    with drawing:
        st.image(overlay, caption=f"Page {page_number} · source-aligned candidate overlay (unrotated)", width="stretch")
        units = "PDF points" if inspection.page.units == "pdf_point" else "pixels"
        st.caption(f"Source extent: {inspection.page.width:g} × {inspection.page.height:g} {units}. Preview: {preview.width} × {preview.height} pixels.")
    with evidence:
        measurement_details(inspection, measurements, selected_measurement)
        candidate_details(candidates, selected_candidate)
        manual_correction_form(inspection, candidates, selected_candidate)
        st.divider()
        st.markdown("**Source evidence**")
        st.caption(f"SHA-256: {document.document_id}")
        st.write(inspection.coordinate_convention)
        st.warning("Candidate interpretation and PDF visibility remain unverified. Measurement cross-checks or manual reviews do not grant engineering approval.")
        for note in inspection.warnings:
            st.caption(note)
        if inspection.paths:
            selected_id = st.selectbox(
                "Inspect a raw path", [path.id for path in inspection.paths],
                format_func=lambda value: "Path " + value.rsplit("path", 1)[-1],
                key=f"path_{document.document_id}_{page_number}",
            )
            selected = next(path for path in inspection.paths if path.id == selected_id)
            st.json(selected.model_dump(mode="json"), expanded=False)
        else:
            st.write("No extractable vector paths. The preview is available; no geometry was invented.")

    candidate_diagnostics(candidates)

    with st.expander("Path inventory and document metadata"):
        if inspection.paths:
            st.dataframe([
                {"path": p.source.path_index, "paint": p.paint_type, "commands": len(p.commands),
                 "operations": ", ".join(sorted({c.operation for c in p.commands})),
                 "closed": p.close_path, "status": p.status}
                for p in inspection.paths
            ], hide_index=True)
        st.json(document.model_dump(mode="json"), expanded=False)
        st.write("Source-to-preview affine transform [a, b, c, d, e, f]")
        st.code(str(preview.source_to_pixel), language=None)

    export = {
        "artifact_type": "candidate_measurement_evidence_not_engineering_approved",
        "document": document.model_dump(mode="json"),
        "inspection": inspection.model_dump(mode="json"),
        "preview": preview.model_dump(mode="json", exclude={"png"}),
        "candidate_review": candidates.model_dump(mode="json"),
        "measurement_review": measurements.model_dump(mode="json"),
    }
    st.download_button(
        "Download page evidence JSON", data=json.dumps(export, indent=2),
        file_name=f"{Path(document.filename).stem}_page_{page_number}_evidence.json",
        mime="application/json",
    )
    persist_active_review()
    assembly_panel(store,document.document_id,page_number)


if __name__ == "__main__":
    main()
