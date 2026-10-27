"""Run after installing .[dev]: python scripts/generate_examples.py."""

from pathlib import Path
from io import BytesIO

from PIL import Image

from plantrace.demo import raster_plan, vector_plan_pdf


def main() -> None:
    target = Path(__file__).resolve().parents[1] / "examples"
    target.mkdir(exist_ok=True)
    (target / "synthetic_plan.pdf").write_bytes(vector_plan_pdf())
    png = raster_plan()
    (target / "synthetic_plan.png").write_bytes(png)
    with Image.open(BytesIO(png)) as image:
        image.save(target / "synthetic_plan.jpg", quality=90)
    print(f"Generated PDF, PNG and JPEG examples in {target}")


if __name__ == "__main__":
    main()
