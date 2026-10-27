from copy import deepcopy
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from plantrace.candidates import extract_candidates
from plantrace.documents import inspect_page,render_page
from plantrace.evaluation import match_one_to_one,counts,error_summary,evaluate_page
from plantrace.measurements import measure_candidates,approval_blockers
from plantrace.models import Point
from plantrace.overlays import source_to_pixel
from plantrace.calibration import pixel_to_source
from plantrace.topology import build_topology
from scripts.stage5_fixtures import generate
from scripts.evaluate_stage5 import calibrated_book,evaluate_entry
from scripts.stage5_failures import run_failures


@pytest.fixture
def datasets(tmp_path):
    root=tmp_path/"fixtures"
    return root,generate(root)


def extracted(datasets,name,page=1):
    root,entries=datasets
    entry=next(e for e in entries if e["id"]==name)
    path=Path(entry["file"])
    if not path.is_absolute(): path=root/path
    data=path.read_bytes()
    inspection=inspect_page(data,path.name,page)
    scene=extract_candidates(inspection)
    book=calibrated_book(inspection,entry["pages"][page-1])
    measurements=measure_candidates(scene,book)
    return entry,inspection,scene,measurements,data


def test_matching_reassigns_competing_predictions_and_maximises_cardinality():
    assert sorted(match_one_to_one([[.9,.8],[.85,0]],.5))==[(0,1,.8),(1,0,.85)]
    assert sorted(match_one_to_one([[.8,.9],[.7,.5]],.5))==[(0,1,.9),(1,0,.7)]
    assert match_one_to_one([[],[]],.5)==[]
    assert match_one_to_one([], .5)==[]


@pytest.mark.parametrize("scores,threshold",[([[float("nan")]],.5),([[1.1]],.5),([[.8],[]],.5),([[.8]],0)])
def test_matching_rejects_invalid_inputs(scores,threshold):
    with pytest.raises(ValueError): match_one_to_one(scores,threshold)


def test_exact_metrics_empty_cases_and_p95_are_explicit():
    assert counts(2,3,4)["f1"]==pytest.approx(4/7,abs=1e-15)
    assert counts(0,0,0)["f1"] is None
    assert error_summary([],3,0)["mae"] is None
    assert error_summary([0,1,2,3,4],5,5)["p95_linear"]==pytest.approx(3.8,abs=1e-14)


@pytest.mark.parametrize("name,page,rooms",[("two_rooms",1,2),("door",1,2),("endpoint_gap",1,1),
    ("rotated_cropped",1,2),("multipage",1,2),("multipage",2,2)])
def test_source_extraction_matches_controlled_oracle(datasets,name,page,rooms):
    entry,inspection,scene,measurements,data=extracted(datasets,name,page)
    result=evaluate_page(scene,build_topology(scene),measurements,entry["pages"][page-1])
    assert result["room"]["tp"]==rooms
    assert result["room"]["fp"]==result["room"]["fn"]==0
    assert result["room"]["matched_polygon_iou_mean"]==pytest.approx(1,abs=1e-12)
    assert result["wall"]["si_errors"]["mae"]==pytest.approx(0,abs=1e-12)
    assert result["room"]["si_errors"]["mae"]==pytest.approx(0,abs=1e-12)
    if name in {"door","endpoint_gap"} or page==2:
        assert any(m.has_inferred_boundaries for m in measurements.measurements)
        assert all(approval_blockers(m) for m in measurements.measurements if m.candidate_type=="room")
    if name=="rotated_cropped":
        assert inspection.page.original_rotation==90
        assert scene.list_candidates("room")[0].geometry.as_shapely().bounds in [(50,50,250,350),(250,50,450,350)]
        for edge in (550,1100):
            preview=render_page(data,"cropped.pdf",max_edge=edge)
            for candidate in scene.candidates:
                for point in candidate.geometry.coordinates:
                    px=source_to_pixel(point,preview)
                    restored=pixel_to_source(Point(x=px[0],y=px[1]),preview)
                    assert restored.x==pytest.approx(point.x,abs=1e-10)
                    assert restored.y==pytest.approx(point.y,abs=1e-10)


@pytest.mark.parametrize("name",["thin_partition","dashed_partition"])
def test_ignored_possible_partition_cannot_cross_check_aggregate_area(datasets,name):
    _,_,scene,measurements,_=extracted(datasets,name)
    assert len(scene.list_candidates("room"))==1
    room=scene.list_candidates("room")[0]
    assert "ignored_stroke_possible_partition" in room.flags and room.status=="unresolved"
    measured=next(m for m in measurements.measurements if m.candidate_type=="room")
    assert measured.value_si==pytest.approx(12,abs=1e-12)
    assert measured.verification_state=="needs_review" and approval_blockers(measured)


def test_thick_decoration_stays_visible_but_its_walls_are_not_approval_eligible(datasets):
    entry,_,scene,measurements,_=extracted(datasets,"decorations")
    labelled_bounds=[Polygon(r["geometry"]["coordinates"][0]).bounds for r in entry["pages"][0]["rooms"]]
    false_rooms=[c for c in scene.list_candidates("room") if c.geometry.as_shapely().bounds not in labelled_bounds]
    assert len(false_rooms)==1  # Residual false detection remains in F1.
    ambiguous=[c for c in scene.list_candidates("wall") if "isolated_closed_graphic_ambiguous" in c.flags]
    assert len(ambiguous)==8 and all(c.status=="unresolved" for c in ambiguous)
    assert all(approval_blockers(m) for m in measurements.measurements if m.candidate_id in {c.id for c in ambiguous})


def test_missing_source_partition_is_an_undetectable_semantic_failure(datasets):
    entry,_,scene,measurements,_=extracted(datasets,"missing_wall")
    report=evaluate_page(scene,build_topology(scene),measurements,entry["pages"][0])
    assert report["room"]["tp"]==1 and report["room"]["fn"]==1
    assert report["room"]["si_errors"]["mae"]==pytest.approx(6,abs=1e-12)
    assert report["room"]["approval_gate"]["materially_wrong_matched_values_eligible"]==0
    assert any(m.structure_completeness=="not_established" for m in measurements.measurements if m.candidate_type=="room")


def test_invalid_source_and_unlabelled_raster_do_not_gain_invented_labels(datasets):
    _,_,scene,_,_=extracted(datasets,"invalid_graphic")
    assert scene.candidates==[] and any(d.code=="invalid_source_path" for d in scene.diagnostics)
    entry,_,scene,measurements,_=extracted(datasets,"original",2)
    result=evaluate_page(scene,build_topology(scene),measurements,entry["pages"][1])
    assert not result["room"]["available"] and result["room"]["predictions"]==0
    assert result["calibration"]["relative_scale_errors"] is None


def test_evaluation_rejects_bad_or_mutated_provenance(datasets,tmp_path):
    root,_=datasets
    entry,_,scene,measurements,_=extracted(datasets,"two_rooms")
    labels=deepcopy(entry["pages"][0]);labels["rooms"][1]["id"]=labels["rooms"][0]["id"]
    with pytest.raises(ValueError,match="unique"):
        evaluate_page(scene,build_topology(scene),measurements,labels)
    labels=deepcopy(entry["pages"][0]);labels["room_boundary"].append(["room0","unknown"])
    with pytest.raises(ValueError,match="Relationship"):
        evaluate_page(scene,build_topology(scene),measurements,labels)
    with pytest.raises(ValueError,match="checksum"):
        evaluate_entry({**entry,"sha256":"0"*64},root,tmp_path)
    with pytest.raises(ValueError,match="file-safe"):
        evaluate_entry({**entry,"id":"../escape"},root,tmp_path)


def test_injected_errors_have_recorded_outcomes_and_honest_residual_risk(tmp_path):
    report=run_failures(tmp_path/"failures")
    scenarios={s["scenario"]:s for s in report["scenarios"]}
    for name in ["wrong_uniform_scale_10_percent","incorrect_units_consistently_entered_cm","missing_source_partition"]:
        assert scenarios[name]["outcome"]=="escalated"
        assert not scenarios[name]["detected"]
        assert scenarios[name]["blocked"]
    for name in ["conflicting_independent_references","missing_calibration","wrong_opening_host","invalid_bowtie_polygon",
                 "missing_wall_retained_room","hallucinated_unsupported_room","stale_measurements_after_wall_correction",
                 "concurrent_workspace_change","concurrent_architect_decision"]:
        assert scenarios[name]["outcome"]=="blocked"
    assert scenarios["missing_wall_retained_room"]["evidence"]["withheld_room_values"]==1
    assert scenarios["session_restart"]["evidence"]["architect_decisions"]==1
    assert report["unsafe_acceptance"]["numerator"]==0 and report["unsafe_acceptance"]["denominator"]==12


@pytest.mark.parametrize("operation",["decision","host","commit"])
def test_review_mutations_reject_stale_context_tokens(tmp_path,pdf_bytes,operation):
    from test_assembly import service
    from plantrace.repository import ReviewConflict
    api,state=service(tmp_path,pdf_bytes)
    token=api.call_tool("validate_model",{})["review_context_version"]
    wall=state.candidates.list_candidates("wall")[0]
    api.record_decision(wall.id,"needs_review","Writer A","Observed scene",expected_context_version=token)
    with pytest.raises(ReviewConflict,match="another session"):
        if operation=="decision":
            api.record_decision(wall.id,"accept_provisional","Writer B","Obsolete scene",expected_context_version=token)
        elif operation=="commit":
            api.call_tool("commit_entities",{"ids":[wall.id],"expected_context_version":token})
        else:
            relation=next(r for r in api.context().topology.relationships if r.kind=="opening_host")
            api.call_tool("link_opening_to_wall",{"opening_id":relation.from_candidate_id,"wall_id":relation.to_candidate_id,"expected_context_version":token})
    assert len(api.context().decisions)==1
    assert api.store.list_versions(state.document.document_id,1)==[]


def test_simultaneous_sqlite_writers_preserve_winner_and_reject_lost_update(tmp_path,pdf_bytes):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from test_assembly import service
    from plantrace.calibration import update_book
    from plantrace.repository import ReviewStore,ReviewConflict
    api,state=service(tmp_path,pdf_bytes)
    revision=api.context().stored.revision
    barrier=Barrier(2)
    def writer(tolerance):
        store=ReviewStore(tmp_path/"review.sqlite3")
        book=update_book(state.calibration,diagnostic_tolerance=tolerance)
        updated=state.model_copy(update={"calibration":book,"measurements":measure_candidates(state.candidates,book)})
        barrier.wait(timeout=10)
        try:
            store.save_workspace(updated,expected_revision=revision,actor=str(tolerance))
            return "saved",tolerance
        except ReviewConflict:
            return "conflict",tolerance
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(writer,[.01,.03]))
    assert sorted(status for status,_ in results)==["conflict","saved"]
    winner=next(t for status,t in results if status=="saved")
    restored=api.store.load_workspace(state.document.document_id,1)
    assert restored.revision==revision+1 and restored.workspace.calibration.diagnostic_tolerance==winner


def test_complete_report_and_versioned_demo_preserve_uncertainty(tmp_path):
    import json
    from scripts.evaluate_stage5 import run
    from plantrace.assembly_models import BuildingModel
    from plantrace.model_validation import validate_assembled_model
    report=run(tmp_path/"evaluation")
    assert len(report["pages"])==13
    rooms=report["aggregate_synthetic"]["room"]
    assert (rooms["tp"],rooms["fp"],rooms["fn"],rooms["labels"])==(18,1,3,21)
    assert rooms["f1"]==pytest.approx(.9,abs=1e-15)
    assert report["aggregate_real"] is None and report["real_plan_coverage"]["evaluated_pages"]==0
    assert report["end_to_end"]["model_versions"]==[1,2,3,4,5]
    assert report["end_to_end"]["scale_conflict_demo"]["approval_eligible_measurements"]==0
    exported=json.loads((tmp_path/"evaluation/workflow/model_v4_conflict.json").read_text())
    model=BuildingModel.model_validate(exported["model"])
    assert validate_assembled_model(model).structurally_valid
    assert len(model.source_review.calibration.references)==3
    assert model.source_review.corrections
    assert all(e.measurement is None or e.measurement.verification_state=="needs_review" for e in model.entities)
    assert all(r.candidate_relationship.status=="unresolved" for r in model.relationships if r.candidate_relationship.kind=="opening_host")
    assert any(e.measurement and e.measurement.has_inferred_boundaries for e in model.entities)
