"""Independent synthetic fixture: ReportLab vector page + Pillow raster page."""

from io import BytesIO

from PIL import Image, ImageDraw
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


def raster_plan() -> bytes:
    image = Image.new("RGB", (900, 600), "white")
    draw = ImageDraw.Draw(image)
    draw.text((40, 25), "SYNTHETIC RASTER PLAN - preview only", fill="#172B3A", font_size=22)
    draw.rectangle((100, 120, 800, 500), outline="black", width=8)
    draw.line((450, 120, 450, 300), fill="black", width=8)
    draw.line((450, 380, 450, 500), fill="black", width=8)
    draw.text((200, 260), "A", fill="black", font_size=30)
    draw.text((600, 260), "B", fill="black", font_size=30)
    stream = BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def vector_plan_pdf() -> bytes:
    stream = BytesIO()
    drawing = canvas.Canvas(stream, pagesize=(600, 500), invariant=1)
    drawing.setTitle("PlanTrace synthetic inspection fixture")
    drawing.setFont("Helvetica-Bold", 18)
    drawing.drawString(40, 455, "PlanTrace / synthetic vector plan")
    drawing.setFont("Helvetica", 10)
    drawing.drawString(40, 435, "Known source geometry. No building scale or engineering approval.")
    # In unrotated PyMuPDF coordinates: rectangle (100,100)-(500,400).
    drawing.setLineWidth(4)
    drawing.rect(100, 100, 400, 300)
    drawing.line(300, 100, 300, 220)
    drawing.line(300, 280, 300, 400)
    # Door-like arc is only a cubic graphic, not a detected opening.
    drawing.setLineWidth(1)
    curve = drawing.beginPath()
    curve.moveTo(300, 220)
    curve.curveTo(333, 220, 360, 247, 360, 280)
    drawing.drawPath(curve)
    drawing.setFont("Helvetica", 14)
    drawing.drawString(175, 245, "A")
    drawing.drawString(395, 245, "B")
    # Deliberate furniture-like rectangle for Stage 2 false-positive evaluation.
    drawing.setLineWidth(0.5)
    drawing.rect(150, 150, 60, 40)
    drawing.setFont("Helvetica", 9)
    drawing.drawString(40, 40, "400 x 300 PDF points; the small rectangle is furniture-like test geometry.")
    drawing.showPage()
    drawing.setFont("Helvetica-Bold", 18)
    drawing.drawString(40, 455, "PlanTrace / synthetic raster page")
    drawing.drawImage(ImageReader(BytesIO(raster_plan())), 40, 100, width=520, height=347)
    drawing.setFont("Helvetica", 10)
    drawing.drawString(40, 50, "No vector paths should be inferred from this embedded image.")
    drawing.save()
    return stream.getvalue()
