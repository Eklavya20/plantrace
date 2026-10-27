"""Stage 6 evidence policy and source-coverage regressions over real extraction."""
from hashlib import sha256
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from plantrace.calibration import assess_region,make_reference,new_book,put_reference,update_book,digest
from plantrace.candidates import extract_candidates
from plantrace.documents import inspect_page,inspect_document
from plantrace.measurements import measure_candidates,approve_measurement,approval_blockers
from plantrace.metrology_models import ReferenceEstablishment
from plantrace.models import Point
from plantrace.gates import engineering_handoff_gate
from scripts.stage5_fixtures import pdf,line,rectangle
from support_provenance import physical_record,with_physical_declarations


def references(inspection,horizontal=4,vertical=3,unit="m",physical=False):
    book=new_book(inspection)
    for index,(coords,distance) in enumerate([([(100,100),(500,100)],horizontal), ([(100,100),(100,400)],vertical)]):
        book=put_reference(book,make_reference(book,book.regions[0].id,tuple(Point(x=x,y=y) for x,y in coords),distance,unit,f"dimension {index}",
            establishment=physical_record(index) if physical else ReferenceEstablishment(kind="manual_assumption",independence_group="same-unverified-source",
                evidence_record="Manually supplied assumption",established_by="Test operator",established_at="Not known",
                unit_basis="assumed",unit_statement=f"Assumed {unit}; no independent unit check")))
    return book


@pytest.mark.parametrize("horizontal,vertical,unit,scale",[(4.4,3.3,"m",.011),(4,3,"cm",.0001)])
def test_original_consistently_wrong_inputs_are_escalated_not_magically_detected(pdf_bytes,horizontal,vertical,unit,scale):
    inspection=inspect_page(pdf_bytes,"source.pdf")
    book=references(inspection,horizontal,vertical,unit)
    values=measure_candidates(extract_candidates(inspection),book)
    assessment=values.assessments[0]
    assert assessment.status=="cross_checked" and not assessment.conflicts
    assert assessment.correctness_status=="unverified"
    assert assessment.metres_per_source_unit==pytest.approx(scale,abs=1e-15)
    assert len(assessment.unit_assumption_reference_ids)==2
    assert all(m.verification_state=="needs_review" for m in values.measurements)
    with pytest.raises(ValueError,match="Independent scale and unit"):
        approve_measurement(values,values.measurements[0].id,"Reviewer","Bad inputs must not become approved")
    assert not engineering_handoff_gate().eligible


@pytest.mark.parametrize("shared_field",["independence_group","evidence_record","evidence_sha256"])
def test_correlated_physical_declarations_do_not_establish_independence(pdf_bytes,shared_field):
    inspection=inspect_page(pdf_bytes,"source.pdf")
    book=references(inspection,physical=True)
    first,second=book.references
    correlated=second.establishment.model_copy(update={shared_field:getattr(first.establishment,shared_field)})
    book=put_reference(book,second.model_copy(update={"establishment":correlated}))
    a=assess_region(book,book.regions[0])
    assert a.status=="cross_checked"  # Distinct geometric spans still agree.
    assert a.correctness_status=="unverified" and a.correctness_reference_ids==[]


def test_annotations_from_one_drawing_are_not_independent_physical_proof(pdf_bytes):
    inspection=inspect_page(pdf_bytes,"source.pdf")
    book=references(inspection)
    for index,ref in enumerate(list(book.references)):
        evidence=ReferenceEstablishment(kind="source_annotation",independence_group=f"different-label-{index}",
            evidence_record=f"same drawing annotation {index}",established_by="Test annotator",established_at="2026-10-27",
            unit_basis="explicit_source_annotation",unit_statement="m printed in source; not a physical check")
        book=put_reference(book,ref.model_copy(update={"establishment":evidence}))
    assert assess_region(book,book.regions[0]).correctness_status=="unverified"
    m=measure_candidates(extract_candidates(inspection),book).measurements[0]
    assert approval_blockers(m)


def test_independent_evidence_can_expose_wrong_scale_and_units(pdf_bytes):
    inspection=inspect_page(pdf_bytes,"source.pdf")
    for horizontal,vertical,unit in [(4.4,3.3,"m"),(4,3,"cm")]:
        book=references(inspection,horizontal,vertical,unit)
        # Actual controlled extra evidence is a known 4 m synthetic span. It
        # disagrees numerically; the metadata declaration does not cause detection.
        book=put_reference(book,make_reference(book,book.regions[0].id,(Point(x=100,y=100),Point(x=500,y=100)),4,"m","External comparison",
            establishment=physical_record("external")))
        assessment=assess_region(book,book.regions[0])
        assert assessment.status=="conflicting" and assessment.correctness_status=="conflicting"
        assert assessment.conflicts


def test_loose_diagnostic_tolerance_cannot_mask_independent_reference_disagreement(pdf_bytes):
    inspection=inspect_page(pdf_bytes,"source.pdf")
    book=update_book(references(inspection,horizontal=4.4,vertical=3,physical=True),diagnostic_tolerance=.5)
    assessment=assess_region(book,book.regions[0])
    assert assessment.status=="cross_checked"  # Only the loose diagnostic agrees.
    assert assessment.correctness_status=="conflicting" and assessment.evidence_conflicts
    assert assessment.review_reference_tolerance==.02
    assert all(approval_blockers(m) for m in measure_candidates(extract_candidates(inspection),book).measurements)


def test_supported_wall_review_with_declared_evidence_never_grants_engineering_approval(pdf_bytes):
    inspection=inspect_page(pdf_bytes,"source.pdf")
    scene=extract_candidates(inspection)
    book=references(inspection,physical=True)
    measured=measure_candidates(scene,book)
    assert measured.assessments[0].correctness_status=="independently_supported"
    wall=next(m for m in measured.measurements if m.candidate_type=="wall")
    approval=approve_measurement(measured,wall.id,"Named actor","Policy contract using declared test-double evidence")
    assert not approval.engineering_approval
    reviewed=measure_candidates(scene,book,approvals=[approval])
    assert reviewed.select_measurement(wall.id).verification_state=="manually_approved"
    downgraded=put_reference(book,book.references[0].model_copy(update={"establishment":None}))
    assert measure_candidates(scene,downgraded,approvals=[approval]).approvals[0].status=="stale"


def test_wholly_absent_partition_stays_undetectable_even_with_supported_scale():
    coords=[line((100,100),(500,100)),line((500,100),(500,400)),line((500,400),(100,400)),line((100,400),(100,100))]
    data=pdf([{"strokes":[(c,4,False) for c in coords]}])
    inspection=inspect_page(data,"absent.pdf")
    scene=extract_candidates(inspection)
    assert len(scene.list_candidates("room"))==1
    assert all(s.interpretation=="represented" for s in scene.source_line_audit.lines)
    assert not any(d.code=="possible_missed_source_wall" for d in scene.diagnostics)
    measured=measure_candidates(scene,references(inspection,physical=True))
    room=next(m for m in measured.measurements if m.candidate_type=="room")
    assert room.value_si==pytest.approx(12,abs=1e-12)
    assert room.calibration_correctness=="independently_supported"
    assert room.structure_completeness=="not_established" and room.verification_state=="needs_review"
    with pytest.raises(ValueError,match="completeness"):
        approve_measurement(measured,room.id,"Reviewer","Source geometry cannot establish missing intent")


def test_partial_thin_partition_is_source_evidenced_but_furniture_stays_ambiguous():
    strokes=[(rectangle(100,100,500,400),4,False),(line((300,100),(300,220)),.7,False),
             (rectangle(150,260,210,290),.5,False)]
    inspection=inspect_page(pdf([{"strokes":strokes}]),"partial.pdf")
    scene=extract_candidates(inspection)
    audit=scene.source_line_audit
    suspected=[s for s in audit.lines if s.interpretation=="possible_missed_wall"]
    assert len(suspected)==1
    assert suspected[0].geometry.as_shapely().length==120
    assert suspected[0].unexplained_length==pytest.approx(120,abs=3e-6)
    assert suspected[0].evidence.source.document_id==inspection.document_id
    assert any(s.interpretation=="small_or_closed_graphic_ambiguous" for s in audit.lines)
    assert "unexplained_source_wall_like_evidence" in scene.list_candidates("room")[0].flags
    assert scene.list_candidates("room")[0].status=="unresolved"
    assert audit.completeness=="not_established"


def test_provenance_requires_explicit_valid_units_hash_and_date(pdf_bytes):
    inspection=inspect_page(pdf_bytes,"source.pdf")
    book=new_book(inspection)
    with pytest.raises(ValidationError):
        make_reference(book,book.regions[0].id,(Point(x=100,y=100),Point(x=500,y=100)),4,None,"No units")
    for field,value in [("evidence_sha256",None),("established_at","unknown"),("unit_basis","assumed")]:
        invalid={**physical_record(0).model_dump(),field:value}
        with pytest.raises(ValidationError):ReferenceEstablishment.model_validate(invalid)


def test_forged_assurance_field_and_room_completeness_cannot_bypass_gate(pdf_bytes):
    inspection=inspect_page(pdf_bytes,"source.pdf")
    measured=measure_candidates(extract_candidates(inspection),references(inspection))
    for m in measured.measurements:
        fake=m.model_copy(update={"calibration_correctness":"independently_supported","structure_completeness":"not_applicable","verification_state":"cross_checked"})
        assert approval_blockers(fake)


def test_malformed_evidence_instances_and_handoff_flags_cannot_bypass_policy(pdf_bytes):
    from plantrace.calibration import independent_physical_pair
    from plantrace.gates import HandoffDecision
    inspection=inspect_page(pdf_bytes,"source.pdf")
    book=references(inspection,physical=True)
    first,second=book.references
    bad=first.model_copy(update={"establishment":first.establishment.model_copy(update={"evidence_sha256":None})})
    assert not independent_physical_pair(bad,second)
    measured=measure_candidates(extract_candidates(inspection),book)
    assert approval_blockers(measured.measurements[0].model_copy(update={"scale_metres_per_source_unit":0}))
    with pytest.raises(ValidationError):HandoffDecision(eligible=True,reasons=["Manual approval is insufficient"])


def test_prepare_import_preserves_unknown_labels_and_refuses_synthetic_as_real(tmp_path):
    from scripts.prepare_independent_evaluation import prepare
    source=Path(__file__).resolve().parents[1]/"examples/synthetic_plan.pdf"
    with pytest.raises(ValueError,match="synthetic"):
        prepare(source,tmp_path/"refused","test-source","test-permission","Workflow test only",True)
    entry=prepare(source,tmp_path/"transport-test","test-source","test-permission","Workflow test, not real data",False)
    assert entry["origin"]=="unconfirmed_import" and not entry["independent_origin_attestation"]
    assert all(p["rooms"] is None and p["walls"] is None and not p["references"] for p in entry["pages"])
    assert entry["sha256"]==sha256((tmp_path/"transport-test/source.pdf").read_bytes()).hexdigest()
    assert (tmp_path/"transport-test/coordinate_frames.json").exists()


def test_full_stage6_report_separates_detection_blocking_and_declared_evidence_residual(tmp_path):
    from scripts.evaluate_stage6 import run
    report=run(tmp_path/"stage6")
    assert report["core_failure_outcomes"]["material_errors"]==12
    assert report["core_failure_outcomes"]["detected"]==9
    assert report["core_failure_outcomes"]["blocked"]==12
    assert report["core_failure_outcomes"]["escalated"]==3
    assert report["core_failure_outcomes"]["measurement_review_gate_passes"]==0
    assert report["all_material_outcomes"]["tested"]==13
    assert report["all_material_outcomes"]["measurement_review_gate_passes"]==1
    assert report["all_material_outcomes"]["engineering_handoff_gate_passes"]==0
    assert report["aggregate_all_synthetic"]["room"]["f1"]==pytest.approx(38/43,abs=1e-15)
    assert report["aggregate_all_synthetic"]["review"]["needs_review"]==102
    assert report["real_plan_coverage"]["evaluated_pages"]==0


def test_legacy_snapshots_keep_hash_integrity_and_old_reviews_become_stale(tmp_path,pdf_bytes):
    from test_assembly import service
    from plantrace.repository import ReviewStore,PersistenceError
    from plantrace.assembly import PlanTraceService
    api,state=service(tmp_path,pdf_bytes)
    m=state.measurements.measurements[0]
    approval=approve_measurement(state.measurements,m.id,"Legacy reviewer","Positive declaration contract before legacy serialization")
    reviewed=state.model_copy(update={"measurement_approvals":[approval],"measurements":measure_candidates(state.candidates,state.calibration,approvals=[approval])})
    saved=api.store.save_workspace(reviewed,expected_revision=api.context().stored.revision)
    model=api.commit_entities([c.id for c in state.candidates.candidates],"Legacy reviewer","Historical provisional version")
    new_fields={"establishment","correctness_status","correctness_reference_ids","independent_evidence_groups","unit_assumption_reference_ids",
                "calibration_correctness","structure_completeness","review_policy_version","source_line_audit","comparison_span_distinct",
                "evidence_independent_of_primary","evidence_conflicts","review_reference_tolerance"}
    def legacy(value):
        if isinstance(value,dict):return {k:legacy(v) for k,v in value.items() if k not in new_fields}
        if isinstance(value,list):return [legacy(v) for v in value]
        return value
    raw=legacy(reviewed.model_dump(mode="json"));identity=digest(raw)
    old_model=legacy(model.model_dump(mode="json"))
    with api.store.transaction() as connection:
        connection.execute("INSERT INTO workspace_snapshots VALUES(?,?,?,?,?)",(identity,state.document.document_id,1,json.dumps(raw),"legacy-test"))
        connection.execute("UPDATE workspaces SET snapshot_id=?,revision=?",(identity,saved.revision+1))
        connection.execute("UPDATE model_versions SET payload_json=?,payload_hash=?",(json.dumps(old_model),digest(old_model)))
    reopened=PlanTraceService(ReviewStore(tmp_path/"review.sqlite3"),state.document.document_id,1)
    restored=reopened.context()
    assert restored.workspace.calibration.references[0].establishment is None
    assert restored.workspace.measurements.approvals[0].status=="stale"
    assert all(approval_blockers(m) for m in restored.workspace.measurements.measurements)
    export=reopened.export_model(1)
    assert export["current_assessment"]["historical_snapshot"]
    assert not export["engineering_handoff_gate"]["eligible"]
    with api.store.transaction() as connection:
        corrupted={**raw,"geometry_revision":99}
        connection.execute("UPDATE workspace_snapshots SET payload_json=? WHERE snapshot_id=?",(json.dumps(corrupted),identity))
    with pytest.raises(PersistenceError,match="integrity"):
        reopened.context()


def test_audit_rebuilds_after_wall_correction_even_if_caller_keeps_old_audit(tmp_path,pdf_bytes):
    from test_assembly import service
    from plantrace.corrections import correct_candidate
    api,state=service(tmp_path,pdf_bytes)
    old_audit=state.candidates.source_line_audit
    wall=next(c for c in state.candidates.list_candidates("wall") if c.geometry.as_shapely().bounds==(100,100,100,400))
    correction=correct_candidate(wall,[[110,100],[110,400]],state.inspection.page)
    scene=state.candidates.model_copy(update={"candidates":state.candidates.candidates+[correction]})
    api.store.save_workspace(state.model_copy(update={"candidates":scene,"geometry_revision":1}),expected_revision=1)
    context=api.context()
    current=context.workspace.candidates.source_line_audit
    assert current.wall_geometry_version!=old_audit.wall_geometry_version
    assert any(s.unexplained_length>100 for s in current.lines)
    assert any(m.value_si is None and m.candidate_type=="room" for m in context.workspace.measurements.measurements)
