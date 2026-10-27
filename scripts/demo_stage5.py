"""Reproduce the real-extraction Stage 4 workflow plus a persisted scale conflict.

python -m scripts.demo_stage5 --output support/artifacts/stage5/demo
This is a synthetic demo. The injected dimension is deliberately 10% wrong.
"""
import argparse
import json
import math
from pathlib import Path
from uuid import uuid4

from plantrace.assembly import PlanTraceService
from plantrace.calibration import make_reference,put_reference,update_book
from plantrace.measurements import measure_candidates,approval_blockers
from plantrace.models import Point
from plantrace.repository import ReviewStore
from scripts.evaluate_stage4 import run as stage4_workflow


def run(output: Path):
    report=stage4_workflow(output)
    store=ReviewStore(output/"review.sqlite3")
    api=PlanTraceService(store,report["source_sha256"],1)
    state=api.context().stored
    book=state.workspace.calibration
    # Pick endpoints from stored reference coordinates, not fixture detections.
    horizontal,vertical=book.references
    start=horizontal.points[0]
    end=Point(x=horizontal.points[1].x,y=vertical.points[1].y)
    injected=make_reference(book,book.regions[0].id,(start,end),
        math.hypot(horizontal.known_distance,vertical.known_distance)*1.1,"m","Deliberately wrong synthetic diagonal",
        note="Failure injection only: 10% wrong known dimension, manually associated; not a recovered printed dimension.")
    conflict=put_reference(book,injected)
    values=measure_candidates(state.workspace.candidates,conflict,state.workspace.geometry_revision,state.workspace.measurement_approvals)
    assert values.assessments[0].status=="conflicting"
    assert all(approval_blockers(m) for m in values.measurements)
    changed=state.workspace.model_copy(update={"calibration":conflict,"measurements":values})
    saved=store.save_workspace(changed,expected_revision=state.revision,action="demo_scale_conflict",actor="Synthetic failure injection")
    fourth=api.commit_entities([],"Synthetic demo","Persist deliberately conflicting calibration; every SI value remains unverified")
    exported=api.export_model(fourth.version)
    (output/"model_v4_conflict.json").write_text(json.dumps(exported,indent=2),encoding="utf-8")
    resolved=update_book(conflict,references=[r for r in conflict.references if r.id!=injected.id])
    values_after=measure_candidates(changed.candidates,resolved,changed.geometry_revision,changed.measurement_approvals)
    restored=changed.model_copy(update={"calibration":resolved,"measurements":values_after})
    store.save_workspace(restored,expected_revision=saved.revision,action="demo_conflict_removed",actor="Synthetic failure injection")
    fifth=api.commit_entities([],"Synthetic demo","Remove injected reference; preserve wall correction history and room review gates")
    (output/"model_v5.json").write_text(json.dumps(api.export_model(fifth.version),indent=2),encoding="utf-8")
    report["scale_conflict_demo"]={"version":fourth.version,"reference_count":3,"calibration_status":"conflicting",
        "approval_eligible_measurements":sum(not approval_blockers(m) for m in fourth.source_review.measurements.measurements),
        "active_entities":len(fourth.entities),"retired_entities":len(fourth.retired_entities),
        "conflict_removed_version":fifth.version,"restored_calibration_status":values_after.assessments[0].status}
    report["model_versions"]=store.list_versions(report["source_sha256"],1)
    (output/"report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    return report


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=Path("support/artifacts/stage5")/f"demo-{uuid4().hex[:8]}")
    print(json.dumps(run(parser.parse_args().output),indent=2))
