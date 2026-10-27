from io import BytesIO

from PIL import Image, ImageChops
import pymupdf
import pytest

from plantrace.candidates import extract_candidates
from plantrace.documents import inspect_page, render_page
from plantrace.models import Point
from plantrace.overlays import SELECTED, WALL, render_overlay, source_to_pixel


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("max_edge", [500, 1000, 2000])
def test_overlay_alignment_matches_actual_cropped_rotated_source_pixels(rotation, max_edge):
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=500)
        page.draw_line((150, 150), (350, 150), width=4)
        page.set_cropbox(pymupdf.Rect(50, 50, 550, 450))
        page.set_rotation(rotation)
        data = doc.tobytes()
    inspection = inspect_page(data, "crop.pdf")
    result = extract_candidates(inspection)
    preview = render_page(data, "crop.pdf", max_edge=max_edge)
    x, y = source_to_pixel(Point(x=200, y=100), preview)
    center = (round(x), round(y))
    with Image.open(BytesIO(preview.png)) as original:
        assert original.getpixel(center) == (0, 0, 0)
    with Image.open(BytesIO(render_overlay(preview, result))) as overlay:
        assert overlay.size == (preview.width, preview.height)
        assert overlay.getpixel(center) == WALL[:3]
        assert overlay.getpixel((center[0], center[1] + 12)) == (255, 255, 255)
    selected = render_overlay(preview, result, result.candidates[0].id)
    with Image.open(BytesIO(selected)) as overlay:
        assert overlay.getpixel(center) == SELECTED[:3]


def test_room_selection_and_dashed_opening_highlighting(pdf_bytes):
    inspection = inspect_page(pdf_bytes, "plan.pdf")
    result = extract_candidates(inspection)
    preview = render_page(pdf_bytes, "plan.pdf", max_edge=600)
    selected_room = result.list_candidates("room")[0]
    with Image.open(BytesIO(render_overlay(preview, result, selected_room.id))) as overlay:
        # Interior changes without shifting source boundaries; white paper remains outside.
        assert overlay.getpixel((200, 180)) != (255, 255, 255)
        assert overlay.getpixel((90, 180)) == (255, 255, 255)
        assert overlay.getpixel((100, 180)) == SELECTED[:3]
    opening = result.list_candidates("opening")[0]
    with Image.open(BytesIO(render_overlay(preview, result, opening.id))) as overlay:
        assert overlay.getpixel((300, 222)) == SELECTED[:3]
        assert overlay.getpixel((300, 231)) == (255, 255, 255)  # Gap in the dashed hypothesis.


def test_hiding_all_overlays_preserves_preview_pixels(pdf_bytes):
    result = extract_candidates(inspect_page(pdf_bytes, "plan.pdf"))
    preview = render_page(pdf_bytes, "plan.pdf")
    with Image.open(BytesIO(preview.png)) as original, Image.open(BytesIO(render_overlay(preview, result, visible_types=()))) as overlay:
        assert ImageChops.difference(original.convert("RGB"), overlay).getbbox() is None


def test_affine_mapping_uses_all_six_coefficients(pdf_bytes):
    preview = render_page(pdf_bytes, "plan.pdf").model_copy(update={"source_to_pixel": (2, 1, 0.5, 3, -7, 11)})
    assert source_to_pixel(Point(x=10, y=20), preview) == (23, 81)


def test_stale_selection_is_explicit(pdf_bytes):
    result = extract_candidates(inspect_page(pdf_bytes, "plan.pdf"))
    with pytest.raises(KeyError):
        render_overlay(render_page(pdf_bytes, "plan.pdf"), result, "stale")
