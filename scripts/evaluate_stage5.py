"""Reproducible offline evaluation; generated labels never reach extraction.

Run with a new directory: python -m scripts.evaluate_stage5 --output support/artifacts/stage5/run
An optional external manifest uses the same schema as fixtures/manifest.json.
"""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import platform
from pathlib import Path
from uuid import uuid4

import pymupdf
import shapely
import pydantic
from plantrace import __version__

from plantrace.calibration import new_book, make_reference, put_reference
from plantrace.metrology_models import ReferenceEstablishment
from plantrace.candidates import extract_candidates
from plantrace.documents import inspect_document, inspect_page, render_page
from plantrace.evaluation import evaluate_page, counts, error_summary
from plantrace.measurements import measure_candidates
from plantrace.models import Point
from plantrace.overlays import render_overlay
from plantrace.topology import build_topology
from scripts.stage5_fixtures import generate
from scripts.stage5_failures import run_failures
from scripts.demo_stage5 import run as run_workflow


def calibrated_book(inspection, labels):
    book = new_book(inspection)
    for reference in labels.get("references", []):
        book = put_reference(book, make_reference(book, book.regions[0].id,
            tuple(Point(x=x,y=y) for x,y in reference["points"]), reference["distance"], reference["unit"], reference["label"],
            note="Manually associated evaluation reference; see dataset provenance and SI basis.",
            establishment=ReferenceEstablishment.model_validate(reference["establishment"]) if reference.get("establishment") else None))
    return book


def evaluate_entry(entry, root, output):
    if not re.fullmatch(r"[A-Za-z0-9_-]+",entry["id"]):
        raise ValueError("Dataset IDs must be simple unique file-safe identifiers.")
    source=Path(entry["file"])
    if not source.is_absolute(): source=root/source
    data=source.read_bytes()
    if sha256(data).hexdigest()!=entry["sha256"]:
        raise ValueError(f"Source checksum mismatch: {source}")
    doc=inspect_document(data,source.name)
    if len(doc.pages)!=len(entry["pages"]):
        raise ValueError("Manifest must explicitly label or mark unavailable every page.")
    results=[]
    for number, labels in enumerate(entry["pages"],1):
        inspection=inspect_page(data,source.name,number)
        scene=extract_candidates(inspection)
        book=calibrated_book(inspection,labels)
        measurements=measure_candidates(scene,book)
        topology=build_topology(scene)
        preview=render_page(data,source.name,number,max_edge=1000)
        overlay=render_overlay(preview,scene)
        # Every page produces inspectable source/overlay and a full evidence file.
        prefix=f"{entry['id']}_p{number}"
        (output/f"{prefix}_source.png").write_bytes(preview.png)
        (output/f"{prefix}_overlay.png").write_bytes(overlay)
        (output/f"{prefix}_evidence.json").write_text(json.dumps({"inspection":inspection.model_dump(mode="json"),
            "candidates":scene.model_dump(mode="json"),"measurements":measurements.model_dump(mode="json"),
            "topology":topology.model_dump(mode="json")},indent=2),encoding="utf-8")
        results.append({"dataset":entry["id"],"origin":entry["origin"],"sha256":doc.document_id,"page":number,
            "content_kind":inspection.content_kind,"original_rotation":inspection.page.original_rotation,
            "cropbox":inspection.page.cropbox,"preview_transform":preview.source_to_pixel,
            "metrics":evaluate_page(scene,topology,measurements,labels)})
        results[-1]["room_strict_iou_075"] = evaluate_page(scene,topology,measurements,labels,room_iou=.75)["room"]
    return results


def aggregate(pages):
    result={}
    for key in ("room","wall","opening","wall_connectivity","room_adjacency","room_boundary","opening_host"):
        available=[p["metrics"][key] for p in pages if p["metrics"][key]["available"]]
        result[key]={"labelled_pages":len(available),"unlabelled_pages":len(pages)-len(available),
            **counts(sum(v["tp"] for v in available),sum(v["predictions"] for v in available),sum(v["labels"] for v in available))}
        if key=="room":
            similarities=[m["similarity"] for v in available for m in v["matches"]]
            result[key]["matched_polygon_iou_mean"]=sum(similarities)/len(similarities) if similarities else None
        if key in {"room","wall"}:
            result[key]["si_errors"]=error_summary([e for v in available for e in v["si_errors"]["absolute_errors"]],
                sum(v["si_errors"]["labelled_objects"] for v in available),sum(v["si_errors"]["eligible_predictions"] for v in available))
            result[key]["approval_gate"]={k:sum(v["approval_gate"][k] for v in available) for k in
                ("eligible","eligible_unmatched_predictions","materially_wrong_matched_values_eligible")}
    review=[p["metrics"]["review"] for p in pages]
    result["review"]={k:sum(v[k] for v in review) for k in review[0] if not k.endswith("_rate")} if review else {}
    if review:
        result["review"]["unresolved_rate"]=result["review"]["unresolved"]/result["review"]["candidates"] if result["review"]["candidates"] else None
        result["review"]["needs_review_rate"]=result["review"]["needs_review"]/result["review"]["measurements"] if result["review"]["measurements"] else None
    strict=[p["room_strict_iou_075"] for p in pages if p["room_strict_iou_075"]["available"]]
    result["room_strict_iou_075"]=counts(sum(v["tp"] for v in strict),sum(v["predictions"] for v in strict),sum(v["labels"] for v in strict))
    calibration=[p["metrics"]["calibration"] for p in pages]
    errors=[e for c in calibration for e in c["relative_scale_errors"] or []]
    result["calibration"]={"regions":sum(c["regions"] for c in calibration),"conflicting_regions":sum(c["conflicting"] for c in calibration),
        "ground_truth_scale_regions":len(errors),"relative_error":error_summary(errors,len(errors),len(errors)),
        "residual_m":error_summary([r for c in calibration for r in c["reference_residuals_m"]],
             sum(len(c["reference_residuals_m"]) for c in calibration),sum(len(c["reference_residuals_m"]) for c in calibration))}
    hosts=[p["metrics"]["opening_host_assignments"] for p in pages]
    result["opening_host_assignments"]={k:sum(h[k] or 0 for h in hosts) for k in
        ("opening_labels","manual_assignments","correct_supported_assignments","confirmed_assignments","unresolved_opening_candidates")}
    manual_count=result["opening_host_assignments"]["manual_assignments"]
    result["opening_host_assignments"]["accuracy_on_assignments"]=result["opening_host_assignments"]["correct_supported_assignments"]/manual_count if manual_count else None
    result["opening_host_assignments"]["confirmed_host_accuracy"]=None
    return result


def run(output, external_manifest=None):
    if output.exists() and any(output.iterdir()):
        raise ValueError("Choose a new/empty directory; previous evaluation evidence is immutable.")
    output.mkdir(parents=True,exist_ok=True)
    fixtures=output/"fixtures"
    entries=generate(fixtures)
    pages=[p for e in entries for p in evaluate_entry(e,fixtures,output)]
    if external_manifest:
        external=json.loads(external_manifest.read_text(encoding="utf-8"))
        known_ids={e["id"] for e in entries}
        for entry in external["datasets"]:
            if entry["origin"]!="independent_real" or not all(entry.get(k) for k in ("source_url","permission","permission_url")):
                raise ValueError("External real inputs require source and permission provenance.")
            if entry["id"] in known_ids:
                raise ValueError("Dataset identifiers must be unique across manifests.")
            known_ids.add(entry["id"])
            if any(any(t.get("value_si") is not None for t in (p.get("rooms") or [])+(p.get("walls") or [])) or p.get("scale_m_per_unit")
                   for p in entry["pages"]) and not entry.get("si_label_provenance"):
                raise ValueError("Real SI labels require explicit verified dimension/calibration provenance.")
            pages.extend(evaluate_entry(entry,external_manifest.parent,output))
    failures=run_failures(output/"failures")
    e2e=run_workflow(output/"workflow")
    project=Path(__file__).resolve().parents[1]
    fingerprints={name:sha256((project/name).read_bytes()).hexdigest() for name in
        ("plantrace/candidates.py","plantrace/measurements.py","plantrace/assembly.py","plantrace/evaluation.py",
         "scripts/stage5_fixtures.py","scripts/stage5_failures.py","scripts/evaluate_stage5.py","scripts/demo_stage5.py")}
    report={"schema_version":"stage5-evaluation-1","created_at":datetime.now(timezone.utc).isoformat(),
            "implementation_sha256":fingerprints,"dataset_manifest":{"file":str(fixtures/"manifest.json"),
               "sha256":sha256((fixtures/"manifest.json").read_bytes()).hexdigest()},
            "runtime":{"plantrace":__version__,"python":platform.python_version(),"pymupdf":pymupdf.VersionBind,
                       "shapely":shapely.__version__,"geos":shapely.geos_version_string,"pydantic":pydantic.__version__},
            "matching":{"room_iou_threshold":.5,"line_hausdorff_tolerance_pdf_points":.01,
             "assignment":"maximum cardinality, then maximum similarity; one-to-one", "p95":"linear interpolation at (n-1)*0.95"},
            "pages":pages,"aggregate_synthetic":aggregate([p for p in pages if p["origin"]!="independent_real"]),
            "aggregate_real":aggregate([p for p in pages if p["origin"]=="independent_real"]) if any(p["origin"]=="independent_real" for p in pages) else None,
            "real_plan_coverage":{"evaluated_pages":sum(p["origin"]=="independent_real" for p in pages),
              "reason_if_missing":"No permitted independent architectural drawing downloaded: Wikimedia HTTP 429; LOC interactive challenge. No real detections or SI truth claimed." if not external_manifest else None},
            "failures":failures,"end_to_end":e2e}
    (output/"report.json").write_text(json.dumps(report,indent=2,allow_nan=False),encoding="utf-8")
    return report


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=Path("support/artifacts/stage5")/f"run-{uuid4().hex[:8]}")
    parser.add_argument("--external-manifest",type=Path)
    args=parser.parse_args()
    report=run(args.output,args.external_manifest)
    print(json.dumps({"output":str(args.output),"aggregate_synthetic":report["aggregate_synthetic"],
                      "failures":report["failures"],"real_plan_coverage":report["real_plan_coverage"]},indent=2))
