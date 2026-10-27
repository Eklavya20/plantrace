"""Serializable Stage 1 evidence. Drawing paths are not building entities."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Point(EvidenceModel):
    x: float
    y: float


class PageInfo(EvidenceModel):
    page_number: int = Field(ge=1)
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    units: Literal["pdf_point", "pixel"]
    original_rotation: int = 0
    cropbox: tuple[float, float, float, float] | None = None
    mediabox: tuple[float, float, float, float] | None = None


class DocumentInfo(EvidenceModel):
    document_id: str
    filename: str
    format: Literal["pdf", "png", "jpeg"]
    byte_size: int
    pages: list[PageInfo]
    warnings: list[str] = Field(default_factory=list)


class SourceReference(EvidenceModel):
    document_id: str
    page_number: int = Field(ge=1)
    path_index: int = Field(ge=0)
    sequence_number: int | None = None
    extractor: str


class DrawingCommand(EvidenceModel):
    item_index: int = Field(ge=0)
    operation: Literal["line", "cubic_bezier", "rectangle", "quad"]
    points: list[Point]
    orientation: int | None = None


class VectorPath(EvidenceModel):
    id: str
    source: SourceReference
    commands: list[DrawingCommand]
    paint_type: str
    bounds: tuple[float, float, float, float]
    stroke_color: tuple[float, ...] | None = None
    fill_color: tuple[float, ...] | None = None
    stroke_width: float | None = None
    stroke_opacity: float | None = None
    fill_opacity: float | None = None
    close_path: bool = False
    even_odd: bool | None = None
    dashes: str | None = None
    line_cap: tuple[int, ...] | None = None
    line_join: float | None = None
    layer: str | None = None
    status: Literal["extracted_unverified"] = "extracted_unverified"


class PageInspection(EvidenceModel):
    document_id: str
    page: PageInfo
    content_kind: Literal["vector", "raster", "mixed", "text_only", "empty"]
    paths: list[VectorPath]
    image_count: int = Field(ge=0)
    text_character_count: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)
    coordinate_convention: str
    schema_version: str = "0.1"
    calibration_status: Literal["not_calibrated"] = "not_calibrated"


class Preview(EvidenceModel):
    png: bytes
    width: int
    height: int
    # Affine [a,b,c,d,e,f]: pixel_x = a*x + c*y + e; pixel_y = b*x + d*y + f.
    source_to_pixel: tuple[float, float, float, float, float, float]
    rotation: Literal[0] = 0
