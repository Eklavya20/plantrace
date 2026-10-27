"""Auditable synthetic workflow, actual extraction, new SQLite file per run.

Run: python scripts/evaluate_stage4.py --output support/artifacts/stage4/e2e
No candidates or measurement results are hardcoded. The 4 m / 3 m values are
explicit synthetic calibration assumptions, not dimensions recovered from text.
"""

import argparse
from collections import Counter
import json
from pathlib import Path
from uuid import uuid4

from shapely.ops import unary_union

from plantrace.assembly import PlanTraceService
from plantrace.assembly_models import CorrectionContext,ReviewWorkspace
from plantrace.calibration import new_book,make_reference,put_reference
from plantrace.candidates import extract_candidates
from plantrace.corrections import correct_candidate
from plantrace.documents import inspect_document,inspect_page
from plantrace.measurements import measure_candidates
from plantrace.models import Point
from plantrace.repository import ReviewStore


def run(output: Path) -> dict:
    if output.exists() and any(output.iterdir()):
        raise ValueError("Choose a new/empty output directory; existing reviews will not be overwritten.")
    output.mkdir(parents=True,exist_ok=True)
    source=Path(__file__).resolve().parents[1]/"examples"/"synthetic_plan.pdf"
    data=source.read_bytes()
    document=inspect_document(data,source.name)
    inspection=inspect_page(data,source.name)
    candidates=extract_candidates(inspection)
    bounds=unary_union([c.geometry.as_shapely() for c in candidates.list_candidates("wall")]).bounds
    x0,y0,x1,y1=bounds
    book=new_book(inspection)
    for points,distance,label in [([(x0,y0),(x1,y0)],4,"Synthetic assumed horizontal dimension"),
                                  ([(x0,y0),(x0,y1)],3,"Synthetic assumed independent vertical dimension")]:
        ref=make_reference(book,book.regions[0].id,tuple(Point(x=x,y=y) for x,y in points),distance,"m",label,
                           note="Explicit synthetic evaluation assumption; not a recovered real-world dimension.")
        book=put_reference(book,ref)
    measurements=measure_candidates(candidates,book)
    state=ReviewWorkspace(document=document,inspection=inspection,candidates=candidates,calibration=book,measurements=measurements)
    store=ReviewStore(output/"review.sqlite3")
    store.save_source(data,document)
    saved=store.save_workspace(state,expected_revision=0)
    api=PlanTraceService(store,document.document_id,1)
    support=next(r for r in api.context().topology.relationships if r.kind=="opening_host")
    actor="Synthetic workflow evaluation (not authenticated engineering review)"
    api.link_opening_to_wall(support.from_candidate_id,support.to_candidate_id,actor,"Record observed flank; continuous host and opening semantics remain unresolved.")
    api.record_decision(support.from_candidate_id,"needs_review",actor,"Preserve opening uncertainty in the provisional model.")
    first=api.commit_entities([c.id for c in api.list_candidates()],actor,"Initial provisional synthetic assembly")
    restarted=PlanTraceService(ReviewStore(output/"review.sqlite3"),document.document_id,1)
    checkpoint=restarted.context()
    source_after_restart,_=restarted.store.load_source(document.document_id)
    assert source_after_restart==data
    assert len(checkpoint.decisions)==1 and len(checkpoint.assignments)==1
    assert checkpoint.workspace.calibration==book
    second=restarted.commit_entities([],actor,"Refresh into a second immutable version after reopening SQLite")
    assert restarted.export_model(1)["model"]==first.model_dump(mode="json")
    left=next(c for c in restarted.list_candidates("wall") if c.geometry.as_shapely().bounds==(x0,y0,x0,y1))
    correction=correct_candidate(left,[[x0+10,y0],[x0+10,y1]],inspection.page)
    edited=candidates.model_copy(update={"candidates":candidates.candidates+[correction]})
    changed=state.model_copy(update={"candidates":edited,"geometry_revision":1,
        "corrections":[CorrectionContext(config=candidates.config,candidates=[correction])],
        "measurements":measure_candidates(edited,book,1)})
    restarted.store.save_workspace(changed,expected_revision=saved.revision,action="geometry_corrected",actor=actor,note="Synthetic edit: shift an actual extracted wall by 10 source units.")
    assert not restarted.export_model(2)["current_assessment"]["matches_current_review"]
    after_edit=restarted.context()
    assert any(issue.code=="unsupported_room_boundary" for issue in after_edit.topology.issues)
    stale_rooms=[m for m in after_edit.workspace.measurements.measurements if m.candidate_type=="room" and m.boundary_support=="stale"]
    assert stale_rooms and all(m.value_si is None for m in stale_rooms)
    third=restarted.commit_entities([correction.id],actor,"Retire stale wall/room and assemble only structurally supported current entities")
    report={
        "source_sha256":document.document_id,"calibration_assumptions":"4 m horizontal and 3 m vertical over the extracted outer wall extent; not recovered building dimensions",
        "initial_candidate_counts":dict(Counter(c.candidate_type for c in candidates.candidates)),
        "initial_entity_counts":dict(Counter(e.entity_type for e in first.entities)),
        "initial_relationship_counts":dict(Counter(r.candidate_relationship.kind for r in first.relationships)),
        "wall_metres_counts":dict(Counter(m.value_si for m in measurements.measurements if m.candidate_type=="wall")),
        "room_square_metres":[m.value_si for m in measurements.measurements if m.candidate_type=="room"],
        "room_measurement_states":[m.verification_state for m in measurements.measurements if m.candidate_type=="room"],
        "restart":{"source_bytes_match":True,"references":len(checkpoint.workspace.calibration.references),"architect_decisions":len(checkpoint.decisions),"host_assignments":len(checkpoint.assignments)},
        "model_versions":restarted.store.list_versions(document.document_id,1),"initial_entity_count":len(first.entities),
        "after_edit":{"withheld_stale_room_values":len(stale_rooms),"active_entities":len(third.entities),"retired_entities":len(third.retired_entities)},
        "engineering_handoff_eligible":False,"database":str(output/"review.sqlite3"),
    }
    for version in report["model_versions"]:
        (output/f"model_v{version}.json").write_text(json.dumps(restarted.export_model(version),indent=2),encoding="utf-8")
    (output/"report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    return report


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=Path("support/artifacts")/"stage4"/f"e2e-{uuid4().hex[:10]}")
    arguments=parser.parse_args()
    print(json.dumps(run(arguments.output),indent=2))
