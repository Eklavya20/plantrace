"""Prepare original source, coordinate previews and EMPTY manual annotation forms.

python -m scripts.prepare_independent_evaluation --input DRAWING --output NEW_DIR
  --source-url URL --permission-url URL --permission TEXT --attest-independent
No candidate output is copied into ground truth. Independent status is a user
declaration, not an authenticated fact; known project synthetic files are refused.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
from datetime import datetime,timezone

from plantrace.documents import inspect_document,inspect_page,render_page


def prepare(source,output,source_url,permission_url,permission,attest_independent=False):
    if not all(v.strip() for v in (source_url,permission_url,permission)):
        raise ValueError("Original source and specific permission records are required.")
    data=source.read_bytes()
    document=inspect_document(data,source.name)
    known=Path(__file__).resolve().parents[1]/"support/evaluation/fixtures/manifest.json"
    known_hashes={e["sha256"] for e in json.loads(known.read_text(encoding="utf-8"))["datasets"]}
    if attest_independent and document.document_id in known_hashes:
        raise ValueError("Known synthetic fixture cannot be labelled independent_real.")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Choose a new/empty import directory; existing annotations will not be overwritten.")
    output.mkdir(parents=True,exist_ok=True)
    filename="source"+source.suffix.lower()
    (output/filename).write_bytes(data)
    labels=[]
    coordinates=[]
    for number in range(1,len(document.pages)+1):
        inspection=inspect_page(data,source.name,number)
        preview=render_page(data,source.name,number,max_edge=1400)
        (output/f"page_{number}.png").write_bytes(preview.png)
        (output/f"page_{number}_source.json").write_text(inspection.model_dump_json(indent=2),encoding="utf-8")
        coordinates.append({"page":number,"width":inspection.page.width,"height":inspection.page.height,
            "units":inspection.page.units,"original_rotation":inspection.page.original_rotation,
            "cropbox":inspection.page.cropbox,"source_to_preview":preview.source_to_pixel})
        labels.append({"rooms":None,"walls":None,"openings":None,"wall_connectivity":None,"room_boundary":None,
            "room_adjacency":None,"opening_host":None,"scale_m_per_unit":None,"references":[]})
    entry={"id":"independent_"+document.document_id[:12],"file":filename,"sha256":document.document_id,
        "origin":"independent_real" if attest_independent else "unconfirmed_import",
        "source_url":source_url,"permission_url":permission_url,"permission":permission,
        "acquired_at":datetime.now(timezone.utc).isoformat(),"independent_origin_attestation":attest_independent,
        "annotation_provenance":"Unlabelled. An annotator must record source-coordinate geometry independently of PlanTrace candidates.",
        "si_label_provenance":None,"independent_label_review":None,"pages":labels}
    (output/"manifest.json").write_text(json.dumps({"schema_version":"stage5-evaluation-1","datasets":[entry]},indent=2),encoding="utf-8")
    (output/"coordinate_frames.json").write_text(json.dumps(coordinates,indent=2),encoding="utf-8")
    (output/"ANNOTATION.md").write_text("# Independent annotation\n\nOriginal bytes and source/permission records are retained in manifest.json.\n"
        "Annotate the source preview independently of detector output. Record annotator, method, scope and second-review evidence.\n"
        "Use unrotated CropBox-local PDF coordinates (image pixels for raster); coordinate_frames.json contains exact preview affine transforms.\n"
        "Set null for unavailable labels; [] only for reviewed absence. Object labels need unique IDs and GeoJSON Polygon/LineString geometry.\n"
        "Record polygon holes and label-run segmentation. Relationship pairs reference these IDs.\n"
        "Do not supply SI values, scale or dimension references without independently established dimensions, explicit units and provenance.\n"
        "Do not copy the candidate/model export into ground truth. Source annotations from one drawing remain correlated evidence.\n"
        "The origin attestation and evidence metadata are manual declarations; they do not authenticate the source or engineering correctness.\n",encoding="utf-8")
    return entry


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input",type=Path,required=True);p.add_argument("--output",type=Path,required=True)
    p.add_argument("--source-url",required=True);p.add_argument("--permission-url",required=True);p.add_argument("--permission",required=True)
    p.add_argument("--attest-independent",action="store_true")
    a=p.parse_args()
    print(json.dumps(prepare(a.input,a.output,a.source_url,a.permission_url,a.permission,a.attest_independent),indent=2))
