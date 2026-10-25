"""One-page grading report / certificate (PDF, US Letter, FR + EN labels).

Contains no client personal information: the certificate travels with the card.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import Color, HexColor, white
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfgen import canvas

from .. import __version__
from ..data.models import GradingSession
from ..vision import measure_centering
from .certificates import qr_payload

PAGE_W, PAGE_H = letter
MARGIN = 40
INK = HexColor("#16181d")
MUTED = HexColor("#5d6472")
ACCENT = HexColor("#e2b21f")
LINE = HexColor("#d9dde3")
GREEN = (60, 180, 80)  # BGR, centering overlay

# Usual hobby descriptors for each whole grade.
GRADE_NAMES = {
    10: "GEM MINT", 9: "MINT", 8: "NM-MT", 7: "NEAR MINT", 6: "EX-MT",
    5: "EXCELLENT", 4: "VG-EX", 3: "VERY GOOD", 2: "GOOD", 1: "POOR",
}
CRITERIA = [
    ("centering", "Centrage / Centering"),
    ("corners", "Coins / Corners"),
    ("edges", "Bordures / Edges"),
    ("surface", "Surface / Surface"),
]
SOURCES = {"auto": "mesure machine", "operator": "opérateur", "model": "modèle IA"}
PX_PER_MM = 10.0  # canonical rectified images
DEFECT_COLOURS = {"minor": (0, 200, 255), "moderate": (0, 140, 255), "major": (0, 0, 230)}  # BGR
DEFECT_KINDS = {"scratch": "rayure", "mark": "marque / creux"}
SEVERITIES = {"minor": "mineur", "moderate": "moyen", "major": "majeur"}
MAX_LISTED_DEFECTS = 8
WEAR_KINDS = {"whitening": "blanchiment", "missing": "matière manquante", "nick": "entaille"}
LOCATIONS = {"tl": "haut gauche", "tr": "haut droit", "br": "bas droit", "bl": "bas gauche",
             "top": "du haut", "right": "droite", "bottom": "du bas", "left": "gauche"}
DISCLAIMER = (
    "Note propre à OpGrade, établie selon les règles et la version indiquées ; elle ne constitue pas une "
    "prédiction de note PSA, BGS, CGC ou d'un autre organisme. / OpGrade's own grade, produced under the "
    "rules and version shown; not a prediction of a PSA, BGS, CGC or other grade."
)


def _txt(value) -> str:
    """Standard PDF fonts cover Latin-1/cp1252 only: other characters become '?'."""
    return str(value if value is not None else "").encode("cp1252", "replace").decode("cp1252")


def _annotated(path: Path, defects: list = ()) -> tuple[ImageReader | None, object]:
    """Rectified photo with measured borders (green) and numbered surface defects (orange/red)."""
    image = cv2.imread(str(path))
    if image is None:
        return None, None
    result = measure_centering(image)
    h, w = image.shape[:2]
    out = image.copy()
    x0, x1 = int(result.left_px), int(w - result.right_px)
    y0, y1 = int(result.top_px), int(h - result.bottom_px)
    for p1, p2 in (((x0, 0), (x0, h)), ((x1, 0), (x1, h)), ((0, y0), (w, y0)), ((0, y1), (w, y1))):
        cv2.line(out, p1, p2, GREEN, 4, cv2.LINE_AA)  # visible once the photo is scaled down
    for n, d in enumerate([d for d in defects if d.criterion == "surface"], 1):
        colour = DEFECT_COLOURS.get(d.severity, DEFECT_COLOURS["minor"])
        a = (int(d.x_mm * PX_PER_MM) - 6, int(d.y_mm * PX_PER_MM) - 6)
        b = (int((d.x_mm + d.w_mm) * PX_PER_MM) + 6, int((d.y_mm + d.h_mm) * PX_PER_MM) + 6)
        cv2.rectangle(out, a, b, colour, 4)
        cv2.putText(out, str(n), (a[0], max(a[1] - 8, 24)), cv2.FONT_HERSHEY_SIMPLEX, 1.0, colour, 3)
    for d in defects:  # worn corners: box around the corner zone
        if d.criterion == "corners":
            colour = DEFECT_COLOURS.get(d.severity, DEFECT_COLOURS["minor"])
            a = (int(d.x_mm * PX_PER_MM) + 3, int(d.y_mm * PX_PER_MM) + 3)
            b = (int((d.x_mm + d.w_mm) * PX_PER_MM) - 3, int((d.y_mm + d.h_mm) * PX_PER_MM) - 3)
            cv2.rectangle(out, a, b, colour, 4)
    # JPEG keeps the PDF small (a few hundred KB); measurements use the lossless originals.
    ok, jpeg = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return ImageReader(io.BytesIO(jpeg.tobytes())), result


def _fmt_date(dt: datetime | None) -> str:
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def build_report(session: GradingSession, data_dir: Path, verify_url: str | None = None) -> bytes:
    if session.state != "completed" or session.final_grade is None or not session.certificate_number:
        raise ValueError("report requires a completed grading with a certificate number")
    card = session.card
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter, pageCompression=1)
    c.setTitle(f"OpGrade {session.certificate_number}")
    c.setAuthor("OpGrade X")
    c.setSubject(_txt(f"Rapport de gradation - {card.name}"))

    # -- header band -------------------------------------------------------------------------
    band_h = 92
    c.setFillColor(INK)
    c.rect(0, PAGE_H - band_h, PAGE_W, band_h, stroke=0, fill=1)
    c.setFillColor(ACCENT)
    c.setFont("Helvetica-Bold", 24)
    c.drawString(MARGIN, PAGE_H - 44, "OpGrade")
    c.setFillColor(white)
    c.setFont("Helvetica", 11)
    c.drawString(MARGIN, PAGE_H - 64, "Rapport de gradation  /  Grading report")
    c.setFont("Helvetica-Bold", 15)
    c.drawRightString(PAGE_W - MARGIN - 86, PAGE_H - 44, session.certificate_number)
    c.setFont("Helvetica", 8)
    c.drawRightString(PAGE_W - MARGIN - 86, PAGE_H - 58, "N° de certificat / Certificate no.")

    qr = QrCodeWidget(qr_payload(session.certificate_number, verify_url), barLevel="M")
    x1, y1, x2, y2 = qr.getBounds()
    size = 76
    drawing = Drawing(size, size, transform=[size / (x2 - x1), 0, 0, size / (y2 - y1), 0, 0])
    drawing.add(qr)
    c.setFillColor(white)
    c.rect(PAGE_W - MARGIN - size, PAGE_H - band_h + 8, size, size, stroke=0, fill=1)
    renderPDF.draw(drawing, c, PAGE_W - MARGIN - size, PAGE_H - band_h + 8)

    # -- card identity -----------------------------------------------------------------------
    y = PAGE_H - band_h - 34
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 20)
    for line in simpleSplit(_txt(card.name), "Helvetica-Bold", 20, 330)[:2]:
        c.drawString(MARGIN, y, line)
        y -= 24
    c.setFont("Helvetica", 11)
    c.setFillColor(MUTED)
    details = " · ".join(_txt(v) for v in (card.set_name, card.card_number, card.year) if v)
    if details:
        c.drawString(MARGIN, y, details)
        y -= 15
    c.drawString(MARGIN, y, _txt(f"{card.card_type.name}" + (f" · {card.rarity}" if card.rarity else "")
                                 + (f" · {card.language.upper()}" if card.language else "")))

    # -- grade box ---------------------------------------------------------------------------
    box_w, box_h = 170, 128
    bx, by = PAGE_W - MARGIN - box_w, PAGE_H - band_h - 24 - box_h
    c.setFillColor(INK)
    c.roundRect(bx, by, box_w, box_h, 10, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("Helvetica", 9)
    c.drawCentredString(bx + box_w / 2, by + box_h - 18, "NOTE FINALE / FINAL GRADE")
    c.setFillColor(ACCENT)
    c.setFont("Helvetica-Bold", 60)
    c.drawCentredString(bx + box_w / 2, by + 38, str(session.final_grade))
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(bx + box_w / 2, by + 16, GRADE_NAMES.get(session.final_grade, ""))

    # -- subgrades ---------------------------------------------------------------------------
    y = by - 30
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(MARGIN, y, "Sous-notes / Subgrades")
    y -= 8
    subgrades = {s.criterion: s for s in session.subgrades}
    col_w = (PAGE_W - 2 * MARGIN) / 4
    for i, (key, label) in enumerate(CRITERIA):
        sx = MARGIN + i * col_w
        sub = subgrades.get(key)
        c.setStrokeColor(LINE)
        c.roundRect(sx + 2, y - 58, col_w - 4, 54, 6, stroke=1, fill=0)
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 8)
        c.drawCentredString(sx + col_w / 2, y - 16, label)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 18)
        c.drawCentredString(sx + col_w / 2, y - 38, f"{sub.value:g}" if sub else "-")
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 7)
        c.drawCentredString(sx + col_w / 2, y - 50, SOURCES.get(sub.source, sub.source) if sub else "")
    y -= 76

    # -- photos ------------------------------------------------------------------------------
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(MARGIN, y, "Images (bordures mesurées en vert) / Images (measured borders in green)")
    y -= 10
    photo_h, gap = 228, 14
    photo_w = photo_h * 63 / 88
    captures = {cap.side: cap for cap in session.captures if cap.lighting == "diffuse"}
    defects = {side: [d for d in session.defects if d.side == side] for side in ("front", "back")}
    centering = {}
    for i, (side, label) in enumerate((("front", "Recto / Front"), ("back", "Verso / Back"))):
        px = MARGIN + i * (photo_w + gap)
        cap = captures.get(side)
        reader, result = _annotated(data_dir / cap.warped_path, defects[side]) if cap else (None, None)
        centering[side] = result
        if reader:
            c.drawImage(reader, px, y - photo_h, photo_w, photo_h, preserveAspectRatio=True, anchor="c")
        c.setStrokeColor(LINE)
        c.rect(px, y - photo_h, photo_w, photo_h, stroke=1, fill=0)
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 8)
        c.drawString(px, y - photo_h - 11, label)

    # -- measurements and explanation (right column) -----------------------------------------
    rx = MARGIN + 2 * (photo_w + gap) + 6
    width = PAGE_W - MARGIN - rx

    def write(text: str, ry: float, font: str = "Helvetica", size: float = 8.5, color=INK, indent: float = 0,
              leading: float = 11) -> float:
        c.setFont(font, size)
        c.setFillColor(color)
        for line in simpleSplit(_txt(text), font, size, width - indent):
            c.drawString(rx + indent, ry, line)
            ry -= leading
        return ry

    ry = write("Centrage mesuré / Measured centering", y - 4, "Helvetica-Bold", 10, leading=13) - 2
    for side, label in (("front", "Recto"), ("back", "Verso")):
        res = centering.get(side)
        if res is None:
            continue
        lr, tb = res.left_share, res.top_share
        ry = write(f"{label} : G/D {lr:.0f}/{100 - lr:.0f} · H/B {tb:.0f}/{100 - tb:.0f}", ry, size=9)
        ry = write(f"bordures (mm) : G {res.left_px / 10:.1f} · D {res.right_px / 10:.1f} · "
                   f"H {res.top_px / 10:.1f} · B {res.bottom_px / 10:.1f}", ry, size=8, color=MUTED, indent=8) - 4
    if session.confidence is not None:
        ry = write(f"Confiance de la mesure : {session.confidence * 100:.0f} %", ry, size=9) - 10

    ry = write("Calcul de la note / How the grade was set", ry, "Helvetica-Bold", 10, leading=13) - 2
    for item in session.explanation or []:
        ry = write(f"- {item}", ry) - 3

    # -- defects found by the machine (surface, corners, edges) -------------------------------
    ty = y - photo_h - 34
    analysed = any(m.name in ("surface_defect_score", "corners_wear_score") for m in session.measurements)
    if analysed:
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 9)
        counts = {k: sum(1 for d in session.defects if d.criterion == k) for k in ("corners", "edges", "surface")}
        c.drawString(MARGIN, ty, f"Défauts détectés / Detected defects : coins {counts['corners']} · "
                                 f"bordures {counts['edges']} · surface {counts['surface']} (éclairage rasant)")
        ty -= 12
        c.setFont("Helvetica", 8)
        lines = []
        for criterion, label in (("corners", "Coin"), ("edges", "Bordure"), ("surface", "Surface")):
            for side in ("front", "back"):
                face = "recto" if side == "front" else "verso"
                for n, d in enumerate([d for d in defects[side] if d.criterion == criterion], 1):
                    sev = SEVERITIES.get(d.severity, d.severity)
                    if criterion == "surface":
                        size = f"{d.length_mm:.1f} mm" if d.kind == "scratch" else f"{d.area_mm2:.1f} mm²"
                        lines.append(f"Surface {face} n° {n} : {DEFECT_KINDS.get(d.kind, d.kind)}, {size}, {sev}")
                    else:
                        size = f"{d.length_mm:.1f} mm" if criterion == "edges" and d.kind == "whitening" else f"{d.area_mm2:.2f} mm²"
                        lines.append(f"{label} {LOCATIONS.get(d.location, d.location)} ({face}) : "
                                     f"{WEAR_KINDS.get(d.kind, d.kind)}, {size}, {sev}")
        for line in lines[:MAX_LISTED_DEFECTS]:
            c.setFillColor(INK)
            c.drawString(MARGIN + 6, ty, _txt(line))
            ty -= 10
        if not lines:
            c.setFillColor(MUTED)
            c.drawString(MARGIN + 6, ty, "Aucun défaut détecté.")
            ty -= 10
        elif len(lines) > MAX_LISTED_DEFECTS:
            c.setFillColor(MUTED)
            c.drawString(MARGIN + 6, ty, f"... et {len(lines) - MAX_LISTED_DEFECTS} autre(s).")
            ty -= 10
        ty -= 12

    # -- traceability ------------------------------------------------------------------------
    c.setStrokeColor(LINE)
    c.line(MARGIN, ty + 14, PAGE_W - MARGIN, ty + 14)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(MARGIN, ty, "Traçabilité / Traceability")
    ty -= 13
    order = card.order.number if card.order else "-"
    facts = [
        ("Date", _fmt_date(session.completed_at)),
        ("Machine", session.machine_id),
        ("Opérateur / Operator", session.operator or "-"),
        ("Règles / Rules", session.rules_version),
        ("Modèle IA / AI model", session.model_version or "aucun (V1)"),
        ("Calibration", f"n° {session.calibration_id}" if session.calibration_id else "aucune (non calibrée)"),
        ("Commande / Order", order),
        ("Validation", "sous-notes validées par l'opérateur" if not session.needs_review else "à réviser"),
        ("Logiciel / Software", f"OpGrade X {__version__}"),
    ]
    c.setFont("Helvetica", 8)
    half = (PAGE_W - 2 * MARGIN) / 2
    for i, (k, v) in enumerate(facts):
        fx = MARGIN + (i % 2) * half
        fy = ty - (i // 2) * 12
        c.setFillColor(MUTED)
        c.drawString(fx, fy, f"{k} :")
        c.setFillColor(INK)
        c.drawString(fx + 100, fy, _txt(v))
    ty -= (len(facts) + 1) // 2 * 12 + 8
    c.setFont("Helvetica", 6.5)
    c.setFillColor(MUTED)
    c.drawString(MARGIN, ty, _txt(f"Analyse {session.uuid}"))
    ty -= 12
    for line in simpleSplit(_txt(DISCLAIMER), "Helvetica", 7, PAGE_W - 2 * MARGIN):
        c.drawString(MARGIN, ty, line)
        ty -= 9

    # -- simulated: impossible to mistake for a real certificate -----------------------------
    if session.simulated:
        c.saveState()
        c.setFillColor(Color(0.85, 0.15, 0.15, alpha=0.22))
        c.setFont("Helvetica-Bold", 64)
        c.translate(PAGE_W / 2, PAGE_H / 2)
        c.rotate(35)
        c.drawCentredString(0, 20, "SIMULATION")
        c.setFont("Helvetica-Bold", 28)
        c.drawCentredString(0, -20, "NON VALIDE / NOT VALID")
        c.restoreState()

    c.showPage()
    c.save()
    return buf.getvalue()
