"""Local input validation, inspection and rendering without semantic detection."""

from contextlib import contextmanager
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Iterator
import warnings

from PIL import Image, UnidentifiedImageError
import pymupdf

from plantrace.models import (
    DocumentInfo, DrawingCommand, PageInfo, PageInspection, Point, Preview,
    SourceReference, VectorPath,
)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_PAGES = 500
MAX_IMAGE_PIXELS = 40_000_000
MAX_PATHS = 25_000
MAX_COMMANDS = 150_000
COORDINATES = (
    "PyMuPDF unrotated page coordinates, relative to the visible CropBox; "
    "origin top-left, x right, y down; PDF points (1/72 inch on paper), not building metres. "
    "Preview is rendered without page rotation."
)


class DocumentError(ValueError):
    """An input cannot be processed reliably within the prototype's limits."""


def _format(data: bytes, filename: str) -> str:
    if not data:
        raise DocumentError("The file is empty. Choose a PDF, PNG or JPEG.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise DocumentError("The file exceeds the 50 MiB upload limit.")
    suffix = Path(filename).suffix.lower()
    expected = {".pdf": "pdf", ".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}
    if suffix not in expected:
        raise DocumentError("Unsupported file extension. Use PDF, PNG, JPG or JPEG.")
    actual = (
        "pdf" if data[:1024].lstrip().startswith(b"%PDF-") else
        "png" if data.startswith(b"\x89PNG\r\n\x1a\n") else
        "jpeg" if data.startswith(b"\xff\xd8\xff") else None
    )
    if actual != expected[suffix]:
        raise DocumentError("The file contents do not match its extension, or its header is damaged.")
    return actual


@contextmanager
def _pdf(data: bytes) -> Iterator[pymupdf.Document]:
    doc = None
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
        if doc.needs_pass:
            raise DocumentError("Password-protected PDFs are unsupported. Upload a decrypted copy.")
        if not 0 < len(doc) <= MAX_PAGES:
            raise DocumentError(f"PDF must have between 1 and {MAX_PAGES} pages.")
        yield doc
    except DocumentError:
        raise
    except (RuntimeError, ValueError, OSError) as exc:
        raise DocumentError(f"Cannot process this PDF: {exc}") from exc
    finally:
        if doc is not None:
            doc.close()


def _image(data: bytes) -> Image.Image:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise DocumentError("Image exceeds the 40 megapixel decode limit.")
                image.load()
                # Keep encoded pixel coordinates; do not silently apply EXIF rotation.
                return image.copy()
    except DocumentError:
        raise
    except (UnidentifiedImageError, OSError, ValueError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise DocumentError(f"Cannot decode this image: {exc}") from exc


def _page_info(page: pymupdf.Page) -> PageInfo:
    return PageInfo(
        page_number=page.number + 1,
        width=page.rect.height if page.rotation in (90, 270) else page.rect.width,
        height=page.rect.width if page.rotation in (90, 270) else page.rect.height,
        units="pdf_point",
        original_rotation=page.rotation,
        cropbox=tuple(page.cropbox),
        mediabox=tuple(page.mediabox),
    )


def inspect_document(data: bytes, filename: str) -> DocumentInfo:
    """Read page inventory; expensive graphics extraction is selected-page only."""
    kind = _format(data, filename)
    notes: list[str] = []
    if kind == "pdf":
        with _pdf(data) as doc:
            pages = [_page_info(page) for page in doc]
            if doc.is_repaired:
                notes.append("MuPDF repaired this PDF while opening it; verify against the original.")
    else:
        with _image(data) as image:
            pages = [PageInfo(page_number=1, width=image.width, height=image.height, units="pixel")]
            if image.getexif().get(274, 1) != 1:
                notes.append("EXIF rotation is not applied; coordinates refer to encoded image pixels.")
        notes.append("Raster preview only. Automatic vector extraction and OCR are unavailable.")
    return DocumentInfo(
        document_id=sha256(data).hexdigest(), filename=Path(filename).name,
        format=kind, byte_size=len(data), pages=pages, warnings=notes,
    )


def _point(value: pymupdf.Point) -> Point:
    return Point(x=value.x, y=value.y)


def _command(item: tuple, index: int) -> DrawingCommand:
    code = item[0]
    orientation = None
    if code == "l":
        operation, points = "line", [item[1], item[2]]
    elif code == "c":
        operation, points = "cubic_bezier", list(item[1:5])
    elif code == "re":
        rect = item[1]
        operation, points = "rectangle", [rect.tl, rect.tr, rect.br, rect.bl]
        orientation = item[2]
    elif code == "qu":
        quad = item[1]
        operation, points = "quad", [quad.ul, quad.ur, quad.lr, quad.ll]
    else:
        raise DocumentError(f"Unsupported PDF drawing command {code!r}; extraction stopped without substitution.")
    return DrawingCommand(
        item_index=index, operation=operation, points=[_point(p) for p in points],
        orientation=orientation,
    )


def _paths(page: pymupdf.Page, document_id: str) -> list[VectorPath]:
    drawings = page.get_drawings()
    if len(drawings) > MAX_PATHS or sum(len(d["items"]) for d in drawings) > MAX_COMMANDS:
        raise DocumentError("Page exceeds the prototype's vector complexity limit. Choose a simpler page.")
    paths = []
    for index, drawing in enumerate(drawings):
        paths.append(VectorPath(
            id=f"{document_id}:p{page.number + 1}:path{index}",
            source=SourceReference(
                document_id=document_id, page_number=page.number + 1,
                path_index=index, sequence_number=drawing.get("seqno"),
                extractor=f"PyMuPDF {pymupdf.VersionBind} Page.get_drawings",
            ),
            commands=[_command(item, i) for i, item in enumerate(drawing["items"])],
            paint_type=drawing["type"], bounds=tuple(drawing["rect"]),
            stroke_color=drawing.get("color"), fill_color=drawing.get("fill"),
            stroke_width=drawing.get("width"), stroke_opacity=drawing.get("stroke_opacity"),
            fill_opacity=drawing.get("fill_opacity"), close_path=bool(drawing.get("closePath")),
            even_odd=drawing.get("even_odd"), dashes=drawing.get("dashes"),
            line_cap=drawing.get("lineCap"), line_join=drawing.get("lineJoin"),
            layer=drawing.get("layer"),
        ))
    return paths


def inspect_page(data: bytes, filename: str, page_number: int = 1) -> PageInspection:
    """Extract evidence using one-based page numbers; no geometric guesses."""
    document = inspect_document(data, filename)
    if not 1 <= page_number <= len(document.pages):
        raise DocumentError(f"Page {page_number} does not exist; document has {len(document.pages)} page(s).")
    info = document.pages[page_number - 1]
    notes = list(document.warnings)
    if document.format != "pdf":
        return PageInspection(
            document_id=document.document_id, page=info, content_kind="raster", paths=[],
            image_count=1, text_character_count=0, warnings=notes,
            coordinate_convention="Encoded image pixels, origin top-left, x right, y down. No SI calibration.",
        )
    with _pdf(data) as doc:
        page = doc[page_number - 1]
        paths = _paths(page, document.document_id)
        images = len(page.get_image_info())  # Includes inline image occurrences, not unused resources.
        text_count = len(page.get_text().strip())
    kind = "mixed" if paths and images else "vector" if paths else "raster" if images else "text_only" if text_count else "empty"
    notes.append("Raw drawing commands are unverified; furniture, dimensions and symbols may be present.")
    notes.append("Clipping, occlusion, optional-content visibility and off-page paths are not resolved.")
    if not paths:
        notes.append("No vector paths found. No room, wall or opening detection has been attempted.")
    if info.original_rotation:
        notes.append(f"Original page rotation is {info.original_rotation} degrees; preview is unrotated to match source coordinates.")
    return PageInspection(
        document_id=document.document_id, page=info, content_kind=kind, paths=paths,
        image_count=images, text_character_count=text_count, warnings=notes,
        coordinate_convention=COORDINATES,
    )


def render_page(data: bytes, filename: str, page_number: int = 1, max_edge: int = 2000) -> Preview:
    """Render a bounded preview and record the source-to-preview transform."""
    if not 128 <= max_edge <= 4096:
        raise DocumentError("Preview max_edge must be between 128 and 4096 pixels.")
    document = inspect_document(data, filename)
    if not 1 <= page_number <= len(document.pages):
        raise DocumentError(f"Page {page_number} does not exist.")
    if document.format == "pdf":
        with _pdf(data) as doc:
            page = doc[page_number - 1]
            page.set_rotation(0)
            zoom = min(2.0, max_edge / max(page.rect.width, page.rect.height))
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csRGB, alpha=False)
            return Preview(
                png=pix.tobytes("png"), width=pix.width, height=pix.height,
                source_to_pixel=(zoom, 0, 0, zoom, -pix.x, -pix.y),
            )
    with _image(data) as image:
        original_width, original_height = image.size
        # Composite transparency on white rather than displaying black transparent pixels.
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        background.alpha_composite(rgba)
        preview = background.convert("RGB")
        preview.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        stream = BytesIO()
        preview.save(stream, format="PNG")
        return Preview(
            png=stream.getvalue(), width=preview.width, height=preview.height,
            source_to_pixel=(preview.width / original_width, 0, 0, preview.height / original_height, 0, 0),
        )
