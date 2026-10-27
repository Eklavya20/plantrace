from hashlib import sha256
from io import BytesIO
import json

from PIL import Image
import pymupdf
import pytest

from plantrace.documents import DocumentError, _command, inspect_document, inspect_page, render_page
from plantrace.models import PageInspection


def test_inventory_and_document_identity(pdf_bytes):
    info = inspect_document(pdf_bytes, "test.pdf")
    assert info.document_id == sha256(pdf_bytes).hexdigest()
    assert info.format == "pdf"
    assert len(info.pages) == 2
    assert info.pages[0].width == 600
    assert info.pages[0].height == 500
    assert info.pages[1].page_number == 2
    assert info.pages[0].units == "pdf_point"


def test_extracts_known_geometry_without_semantic_claims(pdf_bytes):
    result = inspect_page(pdf_bytes, "test.pdf")
    assert result.content_kind == "vector"
    assert len(result.paths) == 5
    outer = result.paths[0]
    assert outer.commands[0].operation == "rectangle"
    assert [(p.x, p.y) for p in outer.commands[0].points] == [(100, 100), (500, 100), (500, 400), (100, 400)]
    assert outer.bounds == (100, 100, 500, 400)
    assert outer.stroke_width == 4
    assert result.paths[1].commands[0].operation == "line"
    assert [(p.x, p.y) for p in result.paths[1].commands[0].points] == [(300, 400), (300, 280)]
    assert result.paths[3].commands[0].operation == "cubic_bezier"
    assert [(p.x, p.y) for p in result.paths[3].commands[0].points] == [(300, 280), (333, 280), (360, 253), (360, 220)]
    assert all(p.status == "extracted_unverified" for p in result.paths)
    assert result.calibration_status == "not_calibrated"
    assert "not building metres" in result.coordinate_convention


def test_stable_path_ids_and_page_evidence(pdf_bytes):
    first = inspect_page(pdf_bytes, "one.pdf")
    second = inspect_page(pdf_bytes, "renamed.pdf")
    assert [p.id for p in first.paths] == [p.id for p in second.paths]
    assert len({p.id for p in first.paths}) == len(first.paths)
    for index, path in enumerate(first.paths):
        assert path.source.document_id == sha256(pdf_bytes).hexdigest()
        assert path.source.page_number == 1
        assert path.source.path_index == index
        assert "PyMuPDF" in path.source.extractor


def test_raster_pdf_has_no_fabricated_paths(pdf_bytes):
    result = inspect_page(pdf_bytes, "test.pdf", 2)
    assert result.content_kind == "raster"
    assert result.image_count == 1
    assert result.paths == []
    assert result.text_character_count > 0
    assert any("No vector paths" in warning for warning in result.warnings)


def test_mixed_page(png_bytes):
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=500)
        page.insert_image(pymupdf.Rect(100, 100, 400, 300), stream=png_bytes)
        page.draw_line((20, 20), (500, 20))
        data = doc.tobytes()
    result = inspect_page(data, "mixed.pdf")
    assert result.content_kind == "mixed"
    assert result.image_count == 1
    assert len(result.paths) == 1


@pytest.mark.parametrize("with_text,expected", [(False, "empty"), (True, "text_only")])
def test_non_graphic_pages(with_text, expected):
    with pymupdf.open() as doc:
        page = doc.new_page()
        if with_text:
            page.insert_text((50, 50), "Text is not a wall")
        data = doc.tobytes()
    result = inspect_page(data, "plain.pdf")
    assert result.content_kind == expected
    assert result.paths == []


def test_render_has_bounded_dimensions_and_real_geometry(pdf_bytes):
    preview = render_page(pdf_bytes, "test.pdf", max_edge=1200)
    assert (preview.width, preview.height) == (1200, 1000)
    assert preview.source_to_pixel == (2, 0, 0, 2, 0, 0)
    with Image.open(BytesIO(preview.png)) as image:
        assert image.format == "PNG"
        assert image.getpixel((200, 400)) == (0, 0, 0)  # Known outer vertical wall stroke.
        assert image.getpixel((100, 400)) == (255, 255, 255)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_crop_and_rotation_align_with_unrotated_preview(rotation):
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=500)
        page.draw_line((150, 150), (350, 150), width=3)
        page.set_cropbox(pymupdf.Rect(50, 50, 550, 450))
        page.set_rotation(rotation)
        data = doc.tobytes()
    result = inspect_page(data, "crop.pdf")
    points = result.paths[0].commands[0].points
    assert [(p.x, p.y) for p in points] == [(100, 100), (300, 100)]
    assert result.page.original_rotation == rotation
    assert result.page.cropbox == (50, 50, 550, 450)
    preview = render_page(data, "crop.pdf", max_edge=1000)
    assert (preview.width, preview.height) == (1000, 800)
    assert preview.rotation == 0
    with Image.open(BytesIO(preview.png)) as image:
        assert image.getpixel((400, 200)) == (0, 0, 0)
    with pymupdf.open(stream=data, filetype="pdf") as original:
        assert original[0].rotation == rotation  # Original bytes never rewritten.


@pytest.mark.parametrize("image_format,filename", [("PNG", "plan.png"), ("JPEG", "plan.jpg"), ("JPEG", "plan.jpeg")])
def test_raster_uploads(image_format, filename, png_bytes):
    with Image.open(BytesIO(png_bytes)) as image:
        stream = BytesIO()
        image.save(stream, format=image_format)
    data = stream.getvalue()
    result = inspect_page(data, filename)
    assert result.page.units == "pixel"
    assert result.content_kind == "raster"
    assert result.paths == []
    preview = render_page(data, filename, max_edge=450)
    assert (preview.width, preview.height) == (450, 300)
    assert preview.source_to_pixel == (0.5, 0, 0, 0.5, 0, 0)


def test_transparent_image_composites_on_white():
    stream = BytesIO()
    Image.new("RGBA", (200, 200), (0, 0, 0, 0)).save(stream, format="PNG")
    preview = render_page(stream.getvalue(), "transparent.png")
    with Image.open(BytesIO(preview.png)) as image:
        assert image.getpixel((0, 0)) == (255, 255, 255)


def test_password_pdf_is_explicitly_rejected():
    with pymupdf.open() as doc:
        doc.new_page()
        data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="secret")
    with pytest.raises(DocumentError, match="Password-protected"):
        inspect_document(data, "locked.pdf")


@pytest.mark.parametrize("data,filename,message", [
    (b"", "empty.pdf", "empty"),
    (b"not a pdf", "bad.pdf", "contents do not match"),
    (b"%PDF-1.7\ncorrupt", "bad.pdf", "Cannot process this PDF"),
    (b"\x89PNG\r\n\x1a\ncorrupt", "bad.png", "Cannot decode"),
    (b"hello", "plan.svg", "Unsupported file extension"),
])
def test_invalid_inputs_are_actionable(data, filename, message):
    with pytest.raises(DocumentError, match=message):
        inspect_document(data, filename)


def test_mislabeled_image_is_rejected(png_bytes):
    with pytest.raises(DocumentError, match="contents do not match"):
        inspect_document(png_bytes, "image.pdf")


@pytest.mark.parametrize("page", [0, -1, 3])
def test_bad_page_number(pdf_bytes, page):
    with pytest.raises(DocumentError, match="does not exist"):
        inspect_page(pdf_bytes, "test.pdf", page)
    with pytest.raises(DocumentError, match="does not exist"):
        render_page(pdf_bytes, "test.pdf", page)


def test_limits_fail_explicitly(pdf_bytes, png_bytes, monkeypatch):
    import plantrace.documents as documents

    monkeypatch.setattr(documents, "MAX_UPLOAD_BYTES", 10)
    with pytest.raises(DocumentError, match="upload limit"):
        inspect_document(pdf_bytes, "large.pdf")
    monkeypatch.setattr(documents, "MAX_UPLOAD_BYTES", len(pdf_bytes) + len(png_bytes))
    monkeypatch.setattr(documents, "MAX_PATHS", 1)
    with pytest.raises(DocumentError, match="complexity limit"):
        inspect_page(pdf_bytes, "complex.pdf")
    monkeypatch.setattr(documents, "MAX_IMAGE_PIXELS", 10)
    with pytest.raises(DocumentError, match="megapixel"):
        inspect_document(png_bytes, "large.png")
    monkeypatch.setattr(documents, "MAX_PAGES", 1)
    with pytest.raises(DocumentError, match="between 1 and 1"):
        inspect_document(pdf_bytes, "long.pdf")


def test_unsupported_drawing_is_never_replaced():
    with pytest.raises(DocumentError, match="without substitution"):
        _command(("unknown",), 0)


def test_quad_retains_perimeter_order():
    quad = pymupdf.Quad((10, 10), (40, 10), (10, 30), (40, 30))
    command = _command(("qu", quad), 3)
    assert command.item_index == 3
    assert command.operation == "quad"
    assert [(p.x, p.y) for p in command.points] == [(10, 10), (40, 10), (40, 30), (10, 30)]


def test_evidence_json_roundtrip(pdf_bytes):
    result = inspect_page(pdf_bytes, "test.pdf")
    exported = result.model_dump_json()
    assert PageInspection.model_validate_json(exported) == result
    assert json.loads(exported)["paths"][0]["source"]["page_number"] == 1


def test_pdf_user_unit_is_reflected_in_geometry_extent_and_preview():
    with pymupdf.open() as doc:
        page = doc.new_page(width=300, height=200)
        page.draw_line((50, 50), (250, 50), width=2)
        doc.xref_set_key(page.xref, "UserUnit", "2")
        data = doc.tobytes()
    result = inspect_page(data, "userunit.pdf")
    assert (result.page.width, result.page.height) == (600, 400)
    assert [(p.x, p.y) for p in result.paths[0].commands[0].points] == [(100, 100), (500, 100)]
    preview = render_page(data, "userunit.pdf", max_edge=600)
    with Image.open(BytesIO(preview.png)) as image:
        assert image.getpixel((300, 100)) == (0, 0, 0)


def test_filled_closed_path_paint_evidence_is_preserved():
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.draw_polyline([(10, 10), (50, 10), (30, 40)], closePath=True,
                           color=(1, 0, 0), fill=(0, 0, 1), fill_opacity=0.5, stroke_opacity=0.75)
        data = doc.tobytes()
    path = inspect_page(data, "paint.pdf").paths[0]
    # MuPDF expands this PDF close operation into an explicit final line.
    assert path.commands[-1].points[-1] == path.commands[0].points[0]
    assert path.stroke_color == (1, 0, 0)
    assert path.fill_color == (0, 0, 1)
    assert path.fill_opacity == pytest.approx(0.5)
    assert path.stroke_opacity == pytest.approx(0.75)


@pytest.mark.parametrize("edge", [0, 10000])
def test_unbounded_preview_request_rejected(pdf_bytes, edge):
    with pytest.raises(DocumentError, match="max_edge"):
        render_page(pdf_bytes, "plan.pdf", max_edge=edge)
