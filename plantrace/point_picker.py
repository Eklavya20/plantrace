"""Local Streamlit component: clickable preview coordinates with backend validation."""

import base64
from pathlib import Path

from streamlit.components.v1 import declare_component

from plantrace.calibration import digest, pixel_to_source
from plantrace.metrology_models import CalibrationAssessment, ScaleRegion
from plantrace.models import PageInspection, Point, Preview

def picker_frame(inspection: PageInspection, preview: Preview) -> str:
    return digest({"document": inspection.document_id, "page": inspection.page.model_dump(),
                   "width": preview.width, "height": preview.height, "transform": preview.source_to_pixel})


def decode_selection(payload: dict | None, frame: str, preview: Preview) -> tuple[Point, Point] | None:
    if payload is None:
        return None
    if not isinstance(payload, dict) or payload.get("frame") != frame:
        raise ValueError("Point selection belongs to a different preview. Clear the selection and try again.")
    points = payload.get("points")
    if not isinstance(points, list) or len(points) not in (0, 1, 2):
        raise ValueError("Invalid preview point-selection payload.")
    if len(points) != 2:
        return None
    return tuple(pixel_to_source(Point.model_validate(point), preview) for point in points)


def pick_reference_points(inspection: PageInspection, preview: Preview, region: ScaleRegion,
                          assessments: list[CalibrationAssessment], reset: int = 0) -> tuple[Point, Point] | None:
    frame = picker_frame(inspection, preview)
    guides = []
    for assessment in assessments:
        for reference in assessment.references:
            guides.append({"points": [p.model_dump() for p in reference.points], "label": reference.dimension_label,
                           "conflicting": any(reference.id in conflict.reference_ids for conflict in assessment.conflicts)})
    component = declare_component("plantrace_point_picker", path=Path(__file__).with_name("point_picker_frontend"))
    payload = component(
        image=base64.b64encode(preview.png).decode("ascii"), width=preview.width, height=preview.height,
        frame=frame, reset=reset, region=region.model_dump(mode="json"), guides=guides,
        transform=preview.source_to_pixel, key=f"picker_{frame}", default=None,
    )
    if payload is not None and (not isinstance(payload, dict) or payload.get("reset") != reset or payload.get("region_id") != region.id):
        return None
    return decode_selection(payload, frame, preview)
