"""Rasterize derived geometry with the exact Stage 1 source-to-preview affine."""

from io import BytesIO
import math

from PIL import Image, ImageDraw

from plantrace.candidate_models import CandidateSet
from plantrace.models import Point, Preview

WALL = (20, 125, 115, 255)
ROOM = (51, 112, 214, 255)
OPENING = (214, 117, 10, 255)
SELECTED = (215, 36, 111, 255)
MANUAL = (139, 57, 199, 255)


def source_to_pixel(point: Point, preview: Preview) -> tuple[float, float]:
    a, b, c, d, e, f = preview.source_to_pixel
    return a * point.x + c * point.y + e, b * point.x + d * point.y + f


def _dashed(draw: ImageDraw.ImageDraw, points: list[tuple[float, float]], color, width: int = 3) -> None:
    for a, b in zip(points, points[1:]):
        length = math.dist(a, b)
        if length == 0:
            continue
        ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
        for offset in range(0, math.ceil(length), 14):
            end = min(offset + 8, length)
            draw.line([(a[0] + offset * ux, a[1] + offset * uy), (a[0] + end * ux, a[1] + end * uy)], fill=color, width=width)


def render_overlay(
    preview: Preview, result: CandidateSet, selected_id: str | None = None,
    visible_types: tuple[str, ...] = ("wall", "room", "opening"),
) -> bytes:
    selected_candidate = result.select_candidate(selected_id) if selected_id is not None else None
    with Image.open(BytesIO(preview.png)) as image:
        base = image.convert("RGBA")
    if base.size != (preview.width, preview.height):
        raise ValueError("Preview metadata does not match the preview image dimensions.")

    candidates = [candidate for candidate in result.candidates if candidate.candidate_type in visible_types]
    # Rooms first, observed strokes next, selection last. Polygon holes stay transparent.
    for candidate in sorted(candidates, key=lambda c: (c.id == selected_id, c.candidate_type != "room")):
        selected = candidate.id == selected_id
        color = SELECTED if selected else MANUAL if candidate.origin == "manual_correction" else {"wall": WALL, "room": ROOM, "opening": OPENING}[candidate.candidate_type]
        points = [source_to_pixel(point, preview) for point in candidate.geometry.coordinates]
        layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        if candidate.candidate_type == "room":
            draw.polygon(points, fill=color[:3] + ((75 if selected else 25),))
            for ring in candidate.geometry.holes:
                draw.polygon([source_to_pixel(point, preview) for point in ring], fill=(0, 0, 0, 0))
            # Solid edges correspond to inferred face boundaries; bridge dashes are redrawn below.
            draw.line(points, fill=color, width=5 if selected else 2)
            for ring in candidate.geometry.holes:
                draw.line([source_to_pixel(point, preview) for point in ring], fill=color, width=2)
        elif candidate.candidate_type == "opening":
            _dashed(draw, points, color, 6 if selected else 3)
        else:
            draw.line(points, fill=color, width=7 if selected else 3)
        base = Image.alpha_composite(base, layer)
    draw = ImageDraw.Draw(base)
    for bridge in result.bridges:
        # Every missing-ink closure is visibly dashed; never a solid added wall.
        bridge_selected = selected_candidate is not None and (
            bridge.id in selected_candidate.boundary_bridge_ids or
            selected_candidate.candidate_type == "opening" and selected_id in bridge.supporting_candidate_ids
        )
        if "room" in visible_types or "opening" in visible_types:
            points = [source_to_pixel(point, preview) for point in bridge.geometry.coordinates]
            # Erase the polygon boundary drawn over missing ink, preserving the source underneath.
            # Restore from the actual preview, then draw only the hypothesis dashes.
            mask = Image.new("L", base.size, 0)
            ImageDraw.Draw(mask).line(points, fill=255, width=9)
            with Image.open(BytesIO(preview.png)) as source:
                base.paste(source.convert("RGBA"), (0, 0), mask)
            draw = ImageDraw.Draw(base)
            _dashed(draw, points, SELECTED if bridge_selected else OPENING, 5 if bridge_selected else 3)
            if math.dist(points[0],points[-1]) < 14:
                # A sub-dash gap would otherwise look like a short solid wall.
                # Screen-space ring is a review marker, never source geometry.
                x=(points[0][0]+points[-1][0])/2
                y=(points[0][1]+points[-1][1])/2
                draw.ellipse((x-6,y-6,x+6,y+6),outline=SELECTED if bridge_selected else OPENING,width=2)
    stream = BytesIO()
    base.convert("RGB").save(stream, format="PNG")
    return stream.getvalue()
