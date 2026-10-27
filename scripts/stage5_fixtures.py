"""Independent declarative ground truth -> ReportLab PDFs, not detector output.

All generated data are synthetic. Native coordinates are top-left PDF points;
synthetic SI truth is an explicitly designed 0.01 m/point, not a printed scale.
"""
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path

import pymupdf
from reportlab.pdfgen import canvas
from shapely.geometry import LineString, Polygon, mapping


def line(a, b):
    return [list(a), list(b)]


def make_labels(walls, rooms, openings=(), scale=.01):
    labelled = {"walls": [], "rooms": [], "openings": [], "wall_connectivity": [],
                "room_boundary": [], "room_adjacency": [], "opening_host": [], "scale_m_per_unit": scale}
    for kind, items in [("wall", walls), ("room", rooms), ("opening", openings)]:
        for i, coordinates in enumerate(items):
            shape = Polygon(coordinates) if kind == "room" else LineString(coordinates)
            item = {"id": f"{kind}{i}", "geometry": mapping(shape)}
            if kind != "opening":
                item["value_si"] = (shape.area * scale ** 2 if kind == "room" else shape.length * scale) if scale else None
            labelled[kind + "s"].append(item)
    ws, rs, os = [list(map(LineString, walls)), list(map(Polygon, rooms)), list(map(LineString, openings))]
    labelled["wall_connectivity"] = [[f"wall{i}", f"wall{j}"] for i, a in enumerate(ws) for j, b in enumerate(ws) if i < j and a.intersects(b)]
    labelled["room_adjacency"] = [[f"room{i}", f"room{j}"] for i, a in enumerate(rs) for j, b in enumerate(rs) if i < j and a.boundary.intersection(b.boundary).length > .01]
    labelled["room_boundary"] = [[f"room{i}", f"wall{j}"] for i, a in enumerate(rs) for j, b in enumerate(ws) if a.boundary.intersection(b).length > .01]
    # Oracle openings explicitly denote collinear gaps in the designed walls.
    for i, gap in enumerate(os):
        for j, wall in enumerate(ws):
            if gap.distance(wall) < 1e-9 and abs(gap.union(wall).convex_hull.area) < 1e-9:
                labelled["opening_host"].append([f"opening{i}", f"wall{j}"])
    return labelled


def rectangle(x0, y0, x1, y1):
    return [(x0,y0),(x1,y0),(x1,y1),(x0,y1),(x0,y0)]


def pdf(pages, rotate=False, crop=False):
    out = BytesIO()
    c = canvas.Canvas(out, pagesize=(600,500), invariant=1)
    for spec in pages:
        for coordinates, width, dashed in spec["strokes"]:
            c.setLineWidth(width)
            c.setDash([8,4] if dashed else [])
            p = c.beginPath()
            p.moveTo(coordinates[0][0], 500-coordinates[0][1])
            for x,y in coordinates[1:]: p.lineTo(x,500-y)
            c.drawPath(p, stroke=1, fill=0)
        c.setDash([])
        for points in spec.get("curves", []):
            c.setLineWidth(1)
            p=c.beginPath(); p.moveTo(points[0][0],500-points[0][1])
            p.curveTo(*[v for x,y in points[1:] for v in (x,500-y)])
            c.drawPath(p)
        c.showPage()
    c.save()
    data=out.getvalue()
    if rotate or crop:
        with pymupdf.open(stream=data,filetype="pdf") as doc:
            for page in doc:
                if crop: page.set_cropbox(pymupdf.Rect(50,50,550,450))
                if rotate: page.set_rotation(90)
            data=doc.tobytes(garbage=4,deflate=True)
    return data


def generate(output: Path):
    output.mkdir(parents=True,exist_ok=True)
    # Known noded wall runs (corners are separate runs; T-junctions split runs).
    outer=[line((100,100),(300,100)),line((300,100),(500,100)),line((500,100),(500,400)),
           line((500,400),(300,400)),line((300,400),(100,400)),line((100,400),(100,100))]
    partition=[line((300,100),(300,400))]
    rooms=[rectangle(100,100,300,400),rectangle(300,100,500,400)]
    base={"strokes":[(coordinates,4,False) for coordinates in outer+partition]}
    doorwalls=outer+[line((300,100),(300,220)),line((300,280),(300,400))]
    door={"strokes":[(coordinates,4,False) for coordinates in doorwalls],
          "curves":[[(300,280),(333,280),(360,253),(360,220)]]}
    gapwalls=[line((100,100),(299,100)),line((301,100),(500,100)),line((500,100),(500,400)),line((500,400),(100,400)),line((100,400),(100,100))]
    gap={"strokes":[(coordinates,4,False) for coordinates in gapwalls]}
    decorations=[(rectangle(140,160,210,200),.5,False), (rectangle(20,20,95,95),4,False),
                 (rectangle(120,420,180,450),4,False), (line((30,470),(250,470)),4,False)]
    specs=[("two_rooms",[base],[make_labels(outer+partition,rooms)]),
           ("door",[door],[make_labels(doorwalls,rooms,[line((300,220),(300,280))])]),
           ("decorations",[{"strokes":base["strokes"]+decorations}],[make_labels(outer+partition,rooms)]),
           ("endpoint_gap",[gap],[make_labels(gapwalls,[rectangle(100,100,500,400)])]),
           ("thin_partition",[{"strokes":[(v,4 if i<6 else .7,False) for i,v in enumerate(outer+partition)]}],[make_labels(outer+partition,rooms)]),
           ("dashed_partition",[{"strokes":[(v,4,i==6) for i,v in enumerate(outer+partition)]}],[make_labels(outer+partition,rooms)]),
           ("missing_wall",[{"strokes":base["strokes"][:-1]}],[make_labels([line((100,100),(500,100)),line((500,100),(500,400)),
                  line((500,400),(100,400)),line((100,400),(100,100))],rooms)]),
           ("invalid_graphic",[{"strokes":[([(100,100),(400,350),(100,350),(400,100),(100,100)],4,False)]}],[make_labels([],[])]),
           ("rotated_cropped",[base],[make_labels([[(x-50,y-50) for x,y in w] for w in outer+partition],
                 [[(x-50,y-50) for x,y in r] for r in rooms])]),
           ("multipage",[base,door],[make_labels(outer+partition,rooms),make_labels(doorwalls,rooms,[line((300,220),(300,280))])])]
    inventory=[]
    for name,pages,labels in specs:
        data=pdf(pages,rotate=name=="rotated_cropped",crop=name=="rotated_cropped")
        path=output/f"{name}.pdf"; path.write_bytes(data)
        for label in labels:
            # Independent dimensions are explicit synthetic design labels.
            offset=50 if name=="rotated_cropped" else 0
            label["references"]=[{"points":[[100-offset,100-offset],[500-offset,100-offset]],"distance":4,"unit":"m","label":"designed horizontal"},
                                 {"points":[[100-offset,100-offset],[100-offset,400-offset]],"distance":3,"unit":"m","label":"designed vertical"}]
        entry={"id":name,"file":path.name,"sha256":sha256(data).hexdigest(),"origin":"generated_synthetic",
               "permission":"Project-authored synthetic data for this repository's evaluation; no third-party source.","pages":labels}
        inventory.append(entry)
    # Preserve existing fixture bytes. Its reference distances remain assumptions.
    source=Path(__file__).resolve().parents[1]/"examples"/"synthetic_plan.pdf"
    original=make_labels(doorwalls,rooms,[line((300,220),(300,280))])
    original["references"]=inventory[1]["pages"][0]["references"]
    inventory.append({"id":"original","file":os.path.relpath(source,output.resolve()).replace("\\","/"),"sha256":sha256(source.read_bytes()).hexdigest(),
                      "origin":"existing_synthetic","permission":"Project-authored fixture.",
                      "si_basis":"Explicit test assumptions (4 m/400 points and 3 m/300 points); not dimensions recovered from source.",
                      "pages":[original,{"rooms":None,"walls":None,"openings":None,"references":[]}]})
    (output/"manifest.json").write_text(json.dumps({"schema_version":"stage5-evaluation-1","datasets":inventory},indent=2),encoding="utf-8")
    return inventory
