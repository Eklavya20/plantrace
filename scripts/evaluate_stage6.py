"""Stage 6 evaluation reuses every Stage 5 fixture, metric and failure scenario."""
import argparse
from collections import Counter
from datetime import datetime,timezone
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from plantrace.calibration import new_book,make_reference,put_reference
from plantrace.metrology_models import ReferenceEstablishment
from plantrace.candidates import extract_candidates
from plantrace.documents import inspect_page
from plantrace.measurements import measure_candidates,approval_blockers,approve_measurement
from plantrace.models import Point
from plantrace.gates import engineering_handoff_gate
from scripts.evaluate_stage5 import run as base_run,evaluate_entry,aggregate
from scripts.stage5_fixtures import pdf,make_labels,rectangle,line


def declaration_probe(source):
    """Adversarial DECLARATIONS, not real physical ground truth or a bypass.

    This intentionally demonstrates that the application cannot authenticate
    external evidence. Hash-looking strings are not an authenticated survey.
    """
    inspection=inspect_page(source.read_bytes(),source.name)
    scene=extract_candidates(inspection)
    book=new_book(inspection)
    for index,(points,distance) in enumerate([([(100,100),(500,100)],4.4), ([(100,100),(100,400)],3.3)]):
        evidence=ReferenceEstablishment(kind="independently_verified_physical",independence_group=f"FAKE-CLAIM-independent-domain-{index}",
            evidence_record=f"FAKE-CLAIM-unavailable-record-{index}",evidence_sha256=sha256(f"FAKE-CLAIM-{index}".encode()).hexdigest(),
            established_by="Adversarial synthetic harness; no real survey",established_at="2026-10-27",
            unit_basis="explicit_physical_reference",unit_statement="Intentionally false unit/evidence declaration")
        book=put_reference(book,make_reference(book,book.regions[0].id,tuple(Point(x=x,y=y) for x,y in points),distance,"m",f"Probe {index}",establishment=evidence))
    values=measure_candidates(scene,book)
    wall=next(m for m in values.measurements if m.candidate_type=="wall" and not approval_blockers(m))
    approval=approve_measurement(values,wall.id,"Adversarial harness","Deliberately false external evidence declarations; not real independent validation")
    return {"scenario":"false_independent_physical_evidence_declarations","material_error":True,"detected":False,
        "blocked":False,"escalated":False,"measurement_review_gate_pass":True,
        "engineering_handoff_gate_pass":engineering_handoff_gate().eligible,"calibration_correctness":values.assessments[0].correctness_status,
        "evidence_trust_limit":"Provenance is manually declared; referenced external artifacts are not acquired or authenticated. A dishonest/incorrect claim can still pass measurement review.",
        "approval_snapshot":approval.model_dump(mode="json")}


def run(output,external_manifest=None):
    if output.exists() and any(output.iterdir()):
        raise ValueError("Choose a new/empty Stage 6 output directory.")
    base=base_run(output/"evaluation",external_manifest)
    extension=output/"additional-fixtures";extension.mkdir()
    walls=[line((100,100),(500,100)),line((500,100),(500,400)),line((500,400),(100,400)),line((100,400),(100,100)),line((300,100),(300,220))]
    data=pdf([{"strokes":[(v,4 if i<4 else .7,False) for i,v in enumerate(walls)]}])
    (extension/"partial_thin_partition.pdf").write_bytes(data)
    label=make_labels(walls,[rectangle(100,100,300,400),rectangle(300,100,500,400)])
    label["references"]=json.loads((output/"evaluation/fixtures/manifest.json").read_text())["datasets"][0]["pages"][0]["references"]
    entry={"id":"partial_thin_partition","file":"partial_thin_partition.pdf","sha256":sha256(data).hexdigest(),
        "origin":"generated_synthetic","permission":"Project-authored controlled source geometry; no third-party material.","pages":[label]}
    (extension/"manifest.json").write_text(json.dumps({"datasets":[entry]},indent=2),encoding="utf-8")
    additional=evaluate_entry(entry,extension,output)
    summaries=[]
    for page in base["pages"]+additional:
        root=output if page["dataset"]==entry["id"] else output/"evaluation"
        evidence=json.loads((root/f"{page['dataset']}_p{page['page']}_evidence.json").read_text())
        audit=evidence["candidates"]["source_line_audit"]
        summaries.append({"dataset":page["dataset"],"page":page["page"],"status":audit["status"],
            "completeness":audit["completeness"],"source_edges":len(audit["lines"]),
            "interpretation_counts":dict(Counter(line["interpretation"] for line in audit["lines"])),
            "unexplained_source_length":sum(line["unexplained_length"] for line in audit["lines"]),
            "unassessed_curves":audit["unassessed_curve_commands"]})
    source=Path(__file__).resolve().parents[1]/"examples/synthetic_plan.pdf"
    probe=declaration_probe(source)
    core=base["failures"]["gate_outcomes"]
    project=Path(__file__).resolve().parents[1]
    fingerprints={name:sha256((project/name).read_bytes()).hexdigest() for name in
        ("plantrace/calibration.py","plantrace/metrology_models.py","plantrace/measurements.py","plantrace/source_audit.py",
         "plantrace/gates.py","plantrace/candidates.py","plantrace/candidate_models.py","plantrace/repository.py","plantrace/model_validation.py",
         "plantrace/metrology_ui.py","plantrace/assembly.py","plantrace/review_ui.py","plantrace/session_review.py",
         "scripts/evaluate_stage6.py","scripts/prepare_independent_evaluation.py")}
    report={"schema_version":"stage6-evaluation-1","created_at":datetime.now(timezone.utc).isoformat(),"implementation_sha256":fingerprints,
        "regression_evaluation":base,"additional_pages":additional,"source_line_audits":summaries,
        "aggregate_all_synthetic":aggregate([p for p in base["pages"]+additional if p["origin"]!="independent_real"]),
        "core_failure_outcomes":core,"adversarial_provenance_probe":probe,
        "all_material_outcomes":{"tested":core["material_errors"]+1,"detected":core["detected"]+int(probe["detected"]),"blocked":core["blocked"]+int(probe["blocked"]),
            "escalated":core["escalated"]+int(probe["escalated"]),"measurement_review_gate_passes":core["measurement_review_gate_passes"]+int(probe["measurement_review_gate_pass"]),
            "engineering_handoff_gate_passes":core["engineering_handoff_gate_passes"]+int(probe["engineering_handoff_gate_pass"]),"definition":"12 original material scenarios plus one deliberately false independent-evidence declaration probe. Escalation and blocking overlap; neither proves detection."},
        "real_plan_coverage":{**base["real_plan_coverage"],"implementation_session_access":"New CC BY-SA architectural PDF download: HTTP 429. LOC floor-plan resource: HTTP 403. Metadata was accessible; no original drawing obtained."},
        "browser_qa":{"browser_test_executed":False,"implementation_session_inventory":{"apps":0,"browsers":0},
            "basis":"Enabled browser/app inventory was empty in the implementation session; AppTest, Node handlers and affine/overlay tests used."}}
    (output/"report.json").write_text(json.dumps(report,indent=2,allow_nan=False),encoding="utf-8")
    return report


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output",type=Path,default=Path("support/artifacts/stage6")/f"run-{uuid4().hex[:8]}")
    p.add_argument("--external-manifest",type=Path)
    a=p.parse_args();r=run(a.output,a.external_manifest)
    print(json.dumps({"output":str(a.output),"core":r["core_failure_outcomes"],"all_material":r["all_material_outcomes"],"real_plan_coverage":r["real_plan_coverage"]},indent=2))
