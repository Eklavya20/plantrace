"""Provisional entity assembly and relationship review using the deterministic API."""

import json
import streamlit as st

from plantrace.assembly import PlanTraceService,ModelValidationError
from plantrace.repository import PersistenceError


def assembly_panel(store,document_id: str,page_number: int):
    service=PlanTraceService(store,document_id,page_number)
    context=service.context()
    version_key=f"assembly_view_version_{document_id}_{page_number}"
    viewed_version=st.session_state.get(version_key,context.version)
    report=service.validate_model()
    st.markdown("**Topology and provisional building model**")
    st.caption("Structural consistency, architect review and engineering handoff are separate. Committing a provisional entity does not verify its interpretation or approve engineering use.")
    with st.expander("Current topology and validation"):
        st.write(f"Structural validation: {'passes' if report.structurally_valid else 'needs correction'}")
        if report.issues:
            st.dataframe([{"severity":i.severity,"code":i.code,"detail":i.message,"candidate IDs":", ".join(i.candidate_ids)} for i in report.issues],hide_index=True)
        if context.topology.relationships:
            st.dataframe([{"kind":r.kind,"from candidate":r.from_candidate_id,"to candidate":r.to_candidate_id,
                           "status":r.status,"support":r.support_kind,"assignment":r.assignment_origin} for r in context.topology.relationships],hide_index=True)
        st.json({"topology":context.topology.model_dump(mode="json"),"validation":report.model_dump(mode="json")},expanded=False)
    actor=st.text_input("Architect/reviewer name",key=f"assembly_actor_{document_id}_{page_number}",help="Self-reported review identity, not an authenticated engineering sign-off.")
    candidates=context.workspace.candidates.candidates
    labels={c.id:f"{c.candidate_type.title()} {index+1} | {c.origin} | {service.decision_status(context,c)}" for index,c in enumerate(candidates)}
    with st.expander("Architect decisions and opening host assignments"):
        identity=st.selectbox("Candidate for architect decision",[None]+[c.id for c in candidates],format_func=lambda value:"Select a candidate" if value is None else labels[value],
                              key=f"architect_candidate_{document_id}_{page_number}")
        with st.form(f"architect_decision_{document_id}_{page_number}"):
            decision=st.selectbox("Architect decision",["needs_review","accept_provisional","reject"],key=f"architect_action_{document_id}_{page_number}")
            note=st.text_input("Architect decision note",key=f"architect_note_{document_id}_{page_number}")
            save=st.form_submit_button("Save architect decision",disabled=identity is None)
        if save:
            try:
                service.record_decision(identity,decision,actor,note,expected_context_version=viewed_version)
                st.rerun()
            except (ValueError,KeyError,PersistenceError) as exc:
                st.error(str(exc))
        st.caption("Rejected candidates stay in source history but are excluded from model topology. A rejected boundary can invalidate dependent rooms. Acceptance remains provisional.")
        opening_ids=[c.id for c in service.list_candidates("opening")]
        opening=st.selectbox("Opening for host review",[None]+opening_ids,format_func=lambda value:"Select an opening" if value is None else labels[value],key=f"host_opening_{document_id}_{page_number}")
        supported=[r for r in context.topology.relationships if r.kind=="opening_host" and r.from_candidate_id==opening]
        host_key=f"host_wall_{document_id}_{page_number}_{opening}"
        supported_ids=[r.to_candidate_id for r in supported]
        if st.session_state.get(host_key) not in [None]+supported_ids:
            st.session_state[host_key]=None
        wall=st.selectbox("Supported host wall or flank",[None]+supported_ids,format_func=lambda value:"Select supported geometry" if value is None else labels[value],key=host_key)
        with st.form(f"host_assignment_{document_id}_{page_number}_{opening}"):
            host_note=st.text_input("Host assignment note")
            link=st.form_submit_button("Record supported host assignment",disabled=wall is None or opening is None)
        if link:
            try:
                service.link_opening_to_wall(opening,wall,actor,host_note,expected_context_version=viewed_version)
                st.rerun()
            except (ValueError,KeyError,PersistenceError) as exc:
                st.error(str(exc))
        st.caption("Flanking gap support is not a continuous host wall. Assignments preserve unresolved opening status and do not establish door/window semantics.")
    with st.form(f"commit_model_{document_id}_{page_number}"):
        selected=st.multiselect("Candidates to commit as provisional",context.topology.effective_candidate_ids,
                                format_func=lambda value:labels[value])
        version_note=st.text_input("Model version note",value="Provisional assembly; engineering approval remains pending.")
        commit=st.form_submit_button("Commit provisional entities")
    if commit:
        try:
            if not actor.strip():
                raise ValueError("Enter the architect/reviewer name before committing a version.")
            model=service.commit_entities(selected,actor,version_note,expected_context_version=viewed_version)
            st.session_state[f"latest_committed_version_{document_id}_{page_number}"]=model.version
            st.rerun()
        except (ValueError,KeyError,PersistenceError) as exc:
            st.error(str(exc))
    versions=store.list_versions(document_id,page_number)
    exported=None
    if versions:
        version=st.selectbox("Committed model version",versions,index=len(versions)-1,key=f"model_version_{document_id}_{page_number}_{len(versions)}")
        exported=service.export_model(version)
        model=exported["model"]
        st.write(f"Model version {version}: {len(model['entities'])} provisional entities, {len(model['relationships'])} entity relationships.")
        if not exported["current_assessment"]["matches_current_review"]:
            st.warning("This immutable model version is historical: the current review has changed. Commit a refreshed version before treating it as current.")
        st.info("Engineering handoff: not eligible. Manual measurement approval and provisional assembly do not prove engineering safety.")
        st.dataframe([{"entity":e["id"],"type":e["entity_type"],"assembly":e["status"],"resolution":e["candidate_resolution_status"],
                       "review":e["review_status"],"SI value":e["measurement"]["value_si"] if e["measurement"] else None,
                       "SI unit":e["measurement"]["si_unit"] if e["measurement"] else "","flags":", ".join(e["flags"])} for e in model["entities"]],hide_index=True)
        with st.expander("Assembled entity relationships and auditable JSON"):
            if model["relationships"]:
                st.dataframe([{"kind":r["candidate_relationship"]["kind"],"from entity":r["from_entity_id"],"to entity":r["to_entity_id"],
                               "status":r["candidate_relationship"]["status"],"support":r["candidate_relationship"]["support_kind"]} for r in model["relationships"]],hide_index=True)
            st.json(exported,expanded=False)
        st.download_button("Download versioned building-model JSON",data=json.dumps(exported,indent=2),
                           file_name=f"plantrace_page_{page_number}_model_v{version}.json",mime="application/json")
    else:
        st.caption("No committed building-model entities yet. Select candidates and commit; supported boundary dependencies are included automatically.")
    with st.expander("Persistent review history"):
        history=store.history(document_id,page_number)
        st.dataframe([{"event":row["sequence"],"time UTC":row["created_at"],"action":row["action"],"actor":row["actor"],"note":row["note"]} for row in history],hide_index=True)
        st.json({"architect_decisions":[d.model_dump(mode="json") for d in context.decisions],
                 "host_assignments":[a.model_dump(mode="json") for a in context.assignments]},expanded=False)
    st.session_state.current_topology=context.topology
    st.session_state[version_key]=context.version
    st.session_state.current_building_model=exported
    return exported
