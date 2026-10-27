"""Independent consistency checks for an immutable assembled model snapshot."""

import math

from plantrace.assembly_models import BuildingModel,TopologyIssue,ValidationReport
from plantrace.topology import geometry_version,validate_topology
from plantrace.measurements import approval_blockers


def validate_assembled_model(model: BuildingModel) -> ValidationReport:
    selected={entity.candidate_id for entity in model.entities}
    latest={decision.candidate_id:decision for decision in model.review_decisions}
    excluded={identity for identity,decision in latest.items() if decision.decision=="reject"}
    issues=[issue for issue in validate_topology(model.source_review.candidates,model.topology,model.host_assignments,excluded)
            if not issue.candidate_ids or set(issue.candidate_ids)&selected]
    issues.extend(issue for issue in model.validation.issues if issue not in issues)

    def error(code,message,identities=()):
        issues.append(TopologyIssue(severity="error",code=code,message=message,candidate_ids=list(identities)))

    entity_ids={entity.id:entity for entity in model.entities}
    by_candidate={entity.candidate_id:entity for entity in model.entities}
    source={candidate.id:candidate for candidate in model.source_review.candidates.candidates}
    if len(entity_ids)!=len(model.entities) or len(by_candidate)!=len(model.entities):
        error("duplicate_entity_identity","Assembled entity and candidate identities must be unique.")
    source_measurements={measurement.candidate_id:measurement for measurement in model.source_review.measurements.measurements}
    for entity in model.entities:
        candidate=source.get(entity.candidate_id)
        if candidate is None or entity.candidate_id not in model.topology.effective_candidate_ids:
            error("entity_without_effective_candidate","Entity lacks an effective source candidate.",[entity.candidate_id])
            continue
        if entity.geometry!=candidate.geometry or entity.geometry_version!=geometry_version(candidate) or entity.candidate_snapshot!=candidate or entity.entity_type!=candidate.candidate_type:
            error("entity_geometry_mismatch","Entity geometry/type/version differs from its source snapshot.",[candidate.id])
        if entity.status!="provisional" or entity.review_status=="rejected":
            error("invalid_active_entity_status","Rejected/stale entities belong in history, not the active entity set.",[candidate.id])
        if entity.candidate_resolution_status!=candidate.status or candidate.candidate_type=="opening" and "unresolved_opening" not in entity.flags:
            error("lost_unresolved_status","Assembly must preserve unresolved opening and candidate status.",[candidate.id])
        measurement=entity.measurement
        if measurement!=source_measurements.get(candidate.id):
            error("entity_measurement_mismatch","Entity measurement differs from the auditable source review snapshot.",[candidate.id])
        if measurement:
            if measurement.verification_state=="manually_approved" and approval_blockers(measurement):
                error("unsupported_measurement_approval","Manual approval lacks current independent evidence or room completeness support.",[candidate.id])
            if measurement.geometry_version!=geometry_version(candidate) or measurement.source_geometry!=entity.geometry:
                error("stale_entity_measurement","Measurement belongs to another geometry revision.",[candidate.id])
            if measurement.value_si is not None:
                power=2 if candidate.candidate_type=="room" else 1
                shape=candidate.geometry.as_shapely()
                raw=shape.area if power==2 else shape.length
                try:
                    expected=raw*measurement.scale_metres_per_source_unit**power
                    valid=math.isclose(measurement.value_si,expected,rel_tol=1e-12,abs_tol=1e-12)
                except (TypeError,OverflowError):
                    valid=False
                if not valid or measurement.boundary_support=="stale":
                    error("invalid_si_provenance","SI value is stale or inconsistent with the stored geometry and scale.",[candidate.id])
            if measurement.has_inferred_boundaries and measurement.verification_state in {"cross_checked","manually_approved"}:
                error("unsafe_inferred_verification","Inferred geometry cannot inherit measurement verification/approval.",[candidate.id])
    expected={relation.id:relation for relation in model.topology.relationships if relation.from_candidate_id in by_candidate and relation.to_candidate_id in by_candidate}
    provided={}
    for relation in model.relationships:
        first=entity_ids.get(relation.from_entity_id)
        second=entity_ids.get(relation.to_entity_id)
        candidate_relation=relation.candidate_relationship
        if first is None or second is None or first.candidate_id!=candidate_relation.from_candidate_id or second.candidate_id!=candidate_relation.to_candidate_id:
            error("dangling_entity_relationship","Entity relationship endpoints do not match committed entity identities.")
        if candidate_relation.id in provided or expected.get(candidate_relation.id)!=candidate_relation:
            error("unsupported_entity_relationship","Entity relationship is duplicated or unsupported by source topology.")
        provided[candidate_relation.id]=candidate_relation
    if set(provided)!=set(expected):
        error("incomplete_entity_relationships","Supported relationships between committed entities are missing or have unexpected IDs.")
    if any(entity.status!="stale" for entity in model.retired_entities):
        error("invalid_retired_status","Retired entity snapshots must be explicitly stale.")
    errors={}
    for issue in issues:
        if issue.severity=="error":
            for identity in issue.candidate_ids or ["*"]:
                errors.setdefault(identity,[]).append(issue.code)
    return ValidationReport(scope="committed_entities",structurally_valid=not any(issue.severity=="error" for issue in issues),issues=issues,candidate_errors=errors)
