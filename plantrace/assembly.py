"""Deterministic, database-backed candidate tools and provisional model assembly."""

from dataclasses import dataclass
from uuid import uuid4

from plantrace.assembly_models import (
    ArchitectDecision, BuildingEntity, BuildingModel, EntityRelationship, HostAssignment,
    ReviewWorkspace, TopologyIssue, TopologySnapshot, ValidationReport,
)
from plantrace.calibration import digest
from plantrace.measurements import measure_candidates
from plantrace.repository import ReviewStore, StoredWorkspace, ReviewConflict, utc_now
from plantrace.topology import build_topology, effective_candidates, geometry_version
from plantrace.model_validation import validate_assembled_model
from plantrace.gates import engineering_handoff_gate
from plantrace.source_audit import audit_source_lines


@dataclass
class ReviewContext:
    stored: StoredWorkspace
    workspace: ReviewWorkspace
    topology: TopologySnapshot
    decisions: list[ArchitectDecision]
    assignments: list[HostAssignment]
    version: str


class ModelValidationError(ValueError):
    pass


class PlanTraceService:
    """No LLM dependency. Every mutation validates current geometry in a transaction."""

    def __init__(self,store: ReviewStore,document_id: str,page_number: int):
        self.store=store
        self.document_id=document_id
        self.page_number=page_number

    def _context(self,connection) -> ReviewContext:
        stored=self.store.read_workspace(connection,self.document_id,self.page_number)
        if stored is None:
            raise KeyError("No persisted candidate review exists for this document/page.")
        workspace=stored.workspace
        scene=workspace.candidates.model_copy(update={"source_line_audit":audit_source_lines(workspace.inspection,workspace.candidates)})
        workspace=workspace.model_copy(update={"candidates":scene})
        measurements=measure_candidates(workspace.candidates,workspace.calibration,workspace.geometry_revision,workspace.measurement_approvals)
        workspace=workspace.model_copy(update={"measurements":measurements})
        decisions=self.store.decisions(connection,self.document_id,self.page_number)
        assignments=self.store.assignments(connection,self.document_id,self.page_number)
        latest={decision.candidate_id:decision for decision in decisions}
        excluded={identity for identity,decision in latest.items() if decision.decision=="reject"}
        topology=build_topology(workspace.candidates,assignments,excluded_ids=excluded)
        version=digest({"snapshot":stored.snapshot_id,"revision":stored.revision,"measurements":measurements.model_dump(mode="json"),
                        "topology":topology.version,"decisions":[d.model_dump(mode="json") for d in decisions]})
        return ReviewContext(stored,workspace,topology,decisions,assignments,version)

    def context(self) -> ReviewContext:
        with self.store.connection() as connection:
            return self._context(connection)

    def list_candidates(self,candidate_type: str | None=None,include_inactive: bool=False):
        if candidate_type not in {None,"wall","room","opening"}:
            raise ValueError("Candidate type must be wall, room or opening.")
        context=self.context()
        active=set(context.topology.effective_candidate_ids)
        return [c for c in context.workspace.candidates.list_candidates(candidate_type) if include_inactive or c.id in active]

    def select_candidate(self,candidate_id: str):
        return self.context().workspace.candidates.select_candidate(candidate_id)

    @staticmethod
    def _check_review_version(context, expected_context_version):
        if expected_context_version is not None and context.version != expected_context_version:
            raise ReviewConflict("Review changed in another session. Reload and inspect the current geometry/decisions before submitting again.")

    def link_opening_to_wall(self,opening_id: str,wall_id: str,actor: str="Unspecified local reviewer",note: str="Manual supported host association",
                             expected_context_version: str | None=None) -> HostAssignment:
        if not actor.strip() or not note.strip():
            raise ValueError("Actor and review note are required.")
        with self.store.transaction() as connection:
            context=self._context(connection)
            self._check_review_version(context,expected_context_version)
            opening=context.workspace.candidates.select_candidate(opening_id)
            wall=context.workspace.candidates.select_candidate(wall_id)
            supported=next((r for r in context.topology.relationships if r.kind=="opening_host" and r.from_candidate_id==opening_id and r.to_candidate_id==wall_id),None)
            if opening.candidate_type!="opening" or wall.candidate_type!="wall" or supported is None:
                raise ModelValidationError("Host assignment is unsupported by the current effective opening/wall geometry.")
            assignment=HostAssignment(id=f"assignment:{uuid4().hex}",opening_id=opening_id,wall_id=wall_id,
                opening_geometry_version=geometry_version(opening),wall_geometry_version=geometry_version(wall),
                actor=actor.strip(),note=note.strip(),created_at=utc_now())
            connection.execute("INSERT INTO host_assignments(document_id,page_number,assignment_id,payload_json) VALUES(?,?,?,?)",
                               (self.document_id,self.page_number,assignment.id,assignment.model_dump_json()))
            self.store.event(connection,self.document_id,self.page_number,"opening_host_linked",actor,note,assignment.model_dump(mode="json"))
            return assignment

    def record_decision(self,candidate_id: str,decision: str,actor: str,note: str,expected_context_version: str | None=None) -> ArchitectDecision:
        if not actor.strip() or not note.strip():
            raise ValueError("Reviewer name and a decision note are required.")
        with self.store.transaction() as connection:
            context=self._context(connection)
            self._check_review_version(context,expected_context_version)
            candidate=context.workspace.candidates.select_candidate(candidate_id)
            active,_,_=effective_candidates(context.workspace.candidates)
            if candidate_id not in {c.id for c in active}:
                raise ModelValidationError("Cannot review a superseded or structurally invalid candidate as current.")
            latest={d.candidate_id:d.decision for d in context.decisions}
            latest[candidate_id]=decision
            after=build_topology(context.workspace.candidates,context.assignments,
                                 excluded_ids={identity for identity,value in latest.items() if value=="reject"})
            record=ArchitectDecision(id=f"decision:{uuid4().hex}",candidate_id=candidate_id,geometry_version=geometry_version(candidate),
                topology_version=after.version,decision=decision,actor=actor.strip(),note=note.strip(),created_at=utc_now())
            connection.execute("INSERT INTO architect_decisions(document_id,page_number,decision_id,payload_json) VALUES(?,?,?,?)",
                               (self.document_id,self.page_number,record.id,record.model_dump_json()))
            self.store.event(connection,self.document_id,self.page_number,"candidate_decision",actor,note,record.model_dump(mode="json"))
            return record

    @staticmethod
    def decision_status(context: ReviewContext,candidate) -> str:
        record=next((d for d in reversed(context.decisions) if d.candidate_id==candidate.id),None)
        if record is None:
            return "not_reviewed"
        if record.decision=="reject":
            return "rejected"
        if record.geometry_version!=geometry_version(candidate) or record.topology_version!=context.topology.version:
            return "stale_decision"
        return "accepted_provisional" if record.decision=="accept_provisional" else "needs_review"

    @staticmethod
    def _validation(context: ReviewContext,ids: set[str] | None=None) -> ValidationReport:
        issues=list(context.topology.issues)
        active=set(context.topology.effective_candidate_ids)
        if ids is not None:
            for identity in ids-active:
                issues.append(TopologyIssue(severity="error",code="inactive_candidate",message="Candidate is absent, invalid, superseded or explicitly rejected.",candidate_ids=[identity]))
        original={m.candidate_id:m for m in context.stored.workspace.measurements.measurements}
        for measurement in context.workspace.measurements.measurements:
            previous=original.get(measurement.candidate_id)
            if previous and previous.id==measurement.id and previous!=measurement:
                issues.append(TopologyIssue(severity="error",code="measurement_provenance_mismatch",message="Stored measurement value/provenance differs from deterministic recalculation at the same dependency version.",candidate_ids=[measurement.candidate_id]))
            elif previous and previous.id!=measurement.id:
                issues.append(TopologyIssue(severity="warning",code="stale_measurement_recalculated",message="Stale measurement was recalculated; obsolete approval does not transfer.",candidate_ids=[measurement.candidate_id]))
            if measurement.value_si is None or measurement.verification_state=="needs_review":
                issues.append(TopologyIssue(severity="warning",code="measurement_needs_review",message="SI value is unavailable or unverified; provisional structural commitment does not approve it.",candidate_ids=[measurement.candidate_id]))
        if ids is not None:
            issues=[issue for issue in issues if not issue.candidate_ids or set(issue.candidate_ids)&ids]
        errors={}
        for issue in issues:
            if issue.severity=="error":
                for identity in issue.candidate_ids or ["*"]:
                    errors.setdefault(identity,[]).append(issue.code)
        return ValidationReport(scope="committed_entities" if ids is not None else "candidate_repository",
            structurally_valid=not any(i.severity=="error" for i in issues),issues=issues,candidate_errors=errors)

    def validate_model(self,ids: list[str] | None=None) -> ValidationReport:
        return self._validation(self.context(),set(ids) if ids is not None else None)

    def commit_entities(self,ids: list[str],actor: str="Unspecified local reviewer",note: str="Provisional model assembly",
                        expected_context_version: str | None=None) -> BuildingModel:
        if not actor.strip() or not note.strip():
            raise ValueError("Commit actor and note are required.")
        with self.store.transaction() as connection:
            context=self._context(connection)
            self._check_review_version(context,expected_context_version)
            active={c.id:c for c in context.workspace.candidates.candidates if c.id in context.topology.effective_candidate_ids}
            previous=self.store.read_model(connection,self.document_id,self.page_number)
            requested=set(ids)
            if not requested and previous is None:
                raise ModelValidationError("Select at least one candidate for the first model version.")
            report=self._validation(context,requested)
            if not report.structurally_valid:
                raise ModelValidationError("Cannot commit structurally invalid candidates: "+", ".join(sorted({i.code for i in report.issues if i.severity=="error"})))
            retained=set()
            retired=list(previous.retired_entities) if previous else []
            for entity in previous.entities if previous else []:
                eligible=entity.candidate_id in active and self._validation(context,{entity.candidate_id}).structurally_valid
                if eligible:
                    retained.add(entity.candidate_id)
                else:
                    retired.append(entity.model_copy(update={"status":"stale","retired_reason":"Superseded, rejected or no longer structurally valid in the current review."}))
            selected=requested|retained
            # Include only actual supported boundary/opening dependencies, not the
            # entire connected wall network. No dependency geometry is synthesized.
            while True:
                expanded=set(selected)
                for relation in context.topology.relationships:
                    if relation.from_candidate_id in selected and relation.kind in {"room_boundary","room_boundary_hypothesis","opening_host"}:
                        expanded.add(relation.to_candidate_id)
                if expanded==selected:
                    break
                selected=expanded
            report=self._validation(context,selected)
            if not report.structurally_valid:
                raise ModelValidationError("Required dependencies are structurally invalid: "+", ".join(sorted({i.code for i in report.issues if i.severity=="error"})))
            measurement_by_id={m.candidate_id:m for m in context.workspace.measurements.measurements}
            entities=[]
            for identity in sorted(selected):
                candidate=active[identity]
                measurement=measurement_by_id.get(identity)
                flags=set(candidate.flags)
                if candidate.candidate_type=="opening":
                    flags.add("unresolved_opening")
                if measurement and measurement.has_inferred_boundaries:
                    flags.add("inferred_boundary")
                if measurement and measurement.value_si is None:
                    flags.add("si_unavailable")
                entities.append(BuildingEntity(id="entity:"+digest([self.document_id,self.page_number,identity]),candidate_id=identity,
                    entity_type=candidate.candidate_type,geometry=candidate.geometry,geometry_version=geometry_version(candidate),
                    candidate_snapshot=candidate,source_units=context.workspace.inspection.page.units,
                    candidate_resolution_status=candidate.status,review_status=self.decision_status(context,candidate),flags=sorted(flags),measurement=measurement))
            entity_ids={e.candidate_id:e.id for e in entities}
            relationships=[EntityRelationship(id="entity_relation:"+digest([r.id,entity_ids[r.from_candidate_id],entity_ids[r.to_candidate_id]]),
                from_entity_id=entity_ids[r.from_candidate_id],to_entity_id=entity_ids[r.to_candidate_id],candidate_relationship=r)
                for r in context.topology.relationships if r.from_candidate_id in entity_ids and r.to_candidate_id in entity_ids]
            version=previous.version+1 if previous else 1
            model=BuildingModel(model_id="model:"+digest([self.document_id,self.page_number]),version=version,
                document_id=self.document_id,page_number=self.page_number,workspace_version=context.version,created_at=utc_now(),
                actor=actor.strip(),note=note.strip(),entities=entities,
                retired_entities=list({(e.id,e.geometry_version):e for e in retired if e.id not in set(entity_ids.values())}.values()),
                relationships=relationships,topology=context.topology,review_decisions=context.decisions,host_assignments=context.assignments,
                source_review=context.workspace,validation=report)
            model=model.model_copy(update={"validation":validate_assembled_model(model)})
            if not model.validation.structurally_valid:
                raise ModelValidationError("Assembled snapshot failed entity/relationship/provenance validation.")
            connection.execute("INSERT INTO model_versions VALUES(?,?,?,?,?)",(self.document_id,self.page_number,version,model.model_dump_json(),digest(model.model_dump(mode="json"))))
            self.store.event(connection,self.document_id,self.page_number,"entities_committed",actor,note,{"version":version,"requested_ids":sorted(requested),"committed_ids":sorted(selected)})
            return model

    def export_model(self,version: int | None=None) -> dict:
        with self.store.connection() as connection:
            context=self._context(connection)
            model=self.store.read_model(connection,self.document_id,self.page_number,version)
            if model is None:
                raise KeyError("No committed model version exists for this document/page.")
            current_measurements={m.candidate_id:m for m in context.workspace.measurements.measurements}
            active={c.id:c for c in context.workspace.candidates.candidates if c.id in context.topology.effective_candidate_ids}
            stale=[]
            for entity in model.entities:
                candidate=active.get(entity.candidate_id)
                if candidate is None or geometry_version(candidate)!=entity.geometry_version or model.topology.version!=context.topology.version or entity.measurement!=current_measurements.get(entity.candidate_id) or entity.review_status!=self.decision_status(context,candidate):
                    stale.append(entity.id)
            return {"artifact_type":"versioned_provisional_building_model","schema_version":"0.4","model":model.model_dump(mode="json"),
                "engineering_handoff_gate":engineering_handoff_gate().model_dump(mode="json"),
                "current_assessment":{"matches_current_review":model.workspace_version==context.version,"stale_entity_ids":stale,
                    "historical_snapshot":model.workspace_version!=context.version,"engineering_handoff_eligible":False,
                    "current_repository_validation":self._validation(context).model_dump(mode="json"),
                    "reason":"Model versions are immutable as-of review snapshots, not engineering approval. Refresh/commit a new version after changes."}}

    @staticmethod
    def tool_definitions() -> dict:
        string={"type":"string"}
        identifiers={"type":"array","items":{"type":"string"}}
        definitions={
            "list_candidates":("List actual candidates; inactive evidence is optional.",{"candidate_type":{"type":["string","null"],"enum":[None,"wall","room","opening"]},"include_inactive":{"type":"boolean"}},[]),
            "select_candidate":("Inspect geometry and original source evidence.",{"candidate_id":string},["candidate_id"]),
            "link_opening_to_wall":("Record a geometry-supported association without resolving opening semantics. Pass validate_model's review_context_version as expected_context_version to reject stale writes.",{"opening_id":string,"wall_id":string,"actor":string,"note":string,"expected_context_version":string},["opening_id","wall_id"]),
            "validate_model":("Validate actual topology and measurement consistency, separately from engineering handoff.",{"ids":{**identifiers,"type":["array","null"]}},[]),
            "commit_entities":("Create an immutable provisional version and include supported dependencies.",{"ids":identifiers,"actor":string,"note":string,"expected_context_version":string},["ids"]),
        }
        return {name:{"description":description,"parameters":{"type":"object","properties":properties,"required":required,"additionalProperties":False}}
                for name,(description,properties,required) in definitions.items()}

    def call_tool(self,name: str,arguments: dict):
        if name not in self.tool_definitions():
            raise ValueError(f"Unknown deterministic PlanTrace tool {name!r}.")
        schema=self.tool_definitions()[name]["parameters"]
        if not isinstance(arguments,dict) or set(arguments)-set(schema["properties"]) or set(schema["required"])-set(arguments):
            raise ValueError("Tool arguments have missing or unsupported fields.")
        for key,value in arguments.items():
            allowed=schema["properties"][key]["type"]
            types=[allowed] if isinstance(allowed,str) else allowed
            valid=value is None and "null" in types or isinstance(value,str) and "string" in types or isinstance(value,bool) and "boolean" in types or isinstance(value,list) and "array" in types and all(isinstance(identity,str) for identity in value)
            if not valid:
                raise ValueError(f"Invalid type for tool argument {key!r}.")
        if name=="validate_model":
            # A response token is intentionally outside the persisted schemas:
            # adding default fields there would invalidate Stage 4 content hashes.
            with self.store.connection() as connection:
                context=self._context(connection)
                report=self._validation(context,set(arguments["ids"]) if arguments.get("ids") is not None else None)
                return {**report.model_dump(mode="json"),"review_context_version":context.version}
        result=getattr(self,name)(**arguments)
        if isinstance(result,list):
            return [value.model_dump(mode="json") for value in result]
        return result.model_dump(mode="json")
