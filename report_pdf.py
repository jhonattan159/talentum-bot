"""Reporte de diagnóstico ATS diseñado (PDF con marca TALENTUM).

Portada con banda navy/dorado, puntaje con color por nivel, hallazgos
agrupados por severidad con insignias de color, fortalezas y acciones
priorizadas. Nunca lanza: ante fallo devuelve None.
"""

import io
import logging
from datetime import datetime

log = logging.getLogger("talentum")

try:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (HRFlowable, Paragraph, SimpleDocTemplate,
                                    Spacer, Table, TableStyle)
    _HAS_RL = True
except Exception:  # pragma: no cover
    _HAS_RL = False

NAVY = HexColor("#0A1F44")
GOLD = HexColor("#C9A227")
WHITE = HexColor("#FFFFFF")
DARK = HexColor("#1A1A2E")
GRAY = HexColor("#5A5A6E")
LIGHT_BG = HexColor("#F4F5FA")
RED = HexColor("#E63946")
ORANGE = HexColor("#F4A259")
BLUE = HexColor("#4361EE")
GREEN = HexColor("#2BA84A")

SEV_STYLE = {
    "critico": ("CRÍTICO", RED),
    "medio": ("A MEJORAR", ORANGE),
    "leve": ("DETALLE", BLUE),
}


def _score_color_hex(score):
    try:
        s = int(score)
    except (TypeError, ValueError):
        return GRAY
    if s >= 75:
        return GREEN
    if s >= 50:
        return ORANGE
    return RED


def _verdict(score):
    try:
        s = int(score)
    except (TypeError, ValueError):
        return "Puntaje no disponible en esta revisión."
    if s >= 75:
        return "Tu CV está bien encaminado: con ajustes puntuales puede competir."
    if s >= 50:
        return "Tu CV necesita trabajo: hay frenos reales que corregir."
    return "Tu CV hoy no pasa los filtros ATS: requiere reconstrucción."


def build_report_pdf(report, client_name=""):
    """Genera el PDF del reporte. Devuelve bytes o None."""
    if not _HAS_RL or not isinstance(report, dict):
        return None
    try:
        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf, pagesize=A4,
            leftMargin=18 * mm, rightMargin=18 * mm,
            topMargin=14 * mm, bottomMargin=14 * mm,
            title="Diagnóstico ATS — TALENTUM GROUP",
        )
        W = A4[0] - 36 * mm

        s_title = ParagraphStyle("title", fontName="Helvetica-Bold",
                                 fontSize=22, textColor=WHITE, leading=26)
        s_sub = ParagraphStyle("sub", fontName="Helvetica",
                               fontSize=10, textColor=HexColor("#D9DCE8"),
                               leading=13)
        s_h2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=13,
                              textColor=NAVY, leading=16, spaceBefore=10,
                              spaceAfter=6)
        s_body = ParagraphStyle("body", fontName="Helvetica", fontSize=10,
                                 textColor=DARK, leading=14)
        s_small = ParagraphStyle("small", fontName="Helvetica", fontSize=9,
                                  textColor=GRAY, leading=12)
        s_badge = ParagraphStyle("badge", fontName="Helvetica-Bold",
                                  fontSize=8, textColor=WHITE, leading=10,
                                  alignment=1)
        s_score = ParagraphStyle("score", fontName="Helvetica-Bold",
                                  fontSize=44, leading=44, alignment=1)
        s_fix = ParagraphStyle("fix", fontName="Helvetica", fontSize=10,
                                textColor=DARK, leading=14,
                                backColor=HexColor("#FFF8E6"),
                                borderPadding=(6, 6, 6))

        story = []

        # ---- Cabecera ----
        header_data = [[
            Paragraph("<font color=\"#C9A227\">TALENTUM</font> GROUP", s_title),
            Paragraph("Diagnóstico ATS<br/>Ingeniería de perfiles", s_sub),
        ]]
        ht = Table(header_data, colWidths=[W * 0.62, W * 0.38])
        ht.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), NAVY),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 14),
            ("RIGHTPADDING", (0, 0), (-1, -1), 14),
            ("TOPPADDING", (0, 0), (-1, -1), 14),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
            ("LINEBELOW", (0, 0), (-1, 0), 3, GOLD),
        ]))
        story.append(ht)
        story.append(Spacer(1, 6 * mm))

        fecha = datetime.now().strftime("%d/%m/%Y")
        who = f"Cliente: <b>{client_name}</b> &nbsp;|&nbsp; " if client_name else ""
        story.append(Paragraph(f"{who}Fecha: {fecha}", s_small))
        story.append(Spacer(1, 4 * mm))

        # ---- Puntaje ----
        score = report.get("indice_diagnostico")
        scolor = _score_color_hex(score)
        try:
            score_txt = str(int(score))
        except (TypeError, ValueError):
            score_txt = "—"
        score_box = [[
            Paragraph(f"<font color=\"#{scolor.hexval()[2:]}\">{score_txt}</font>"
                       f"<font color=\"#5A5A6E\" size=\"16\">/100</font>", s_score),
            Paragraph(f"<b>{_verdict(score)}</b><br/><br/>{report.get('resumen','')}",
                       s_body),
        ]]
        st = Table(score_box, colWidths=[W * 0.30, W * 0.70])
        st.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
            ("BOX", (0, 0), (-1, -1), 1, HexColor("#E2E4F0")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ]))
        story.append(st)

        # ---- Hallazgos por severidad ----
        findings = [f for f in (report.get("criterios") or []) if isinstance(f, dict)]
        if findings:
            story.append(Paragraph("Hallazgos del análisis", s_h2))
            order = {"critico": 0, "medio": 1, "leve": 2}
            findings.sort(key=lambda f: order.get(str(f.get("severidad")).lower(), 9))
            for f in findings:
                sev = str(f.get("severidad") or "leve").lower()
                label, color = SEV_STYLE.get(sev, ("DETALLE", BLUE))
                criterio = str(f.get("criterio") or "").strip()
                hallazgo = str(f.get("hallazgo") or "").strip()
                fix = str(f.get("como_arreglarlo") or "").strip()
                badge = Table([[Paragraph(label, s_badge)]], colWidths=[26 * mm])
                badge.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, -1), color),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("ROUNDEDCORNERS", [4, 4, 4, 4]),
                ]))
                story.append(badge)
                story.append(Spacer(1, 2 * mm))
                story.append(Paragraph(f"<b>{criterio}</b>", s_body))
                if hallazgo:
                    story.append(Paragraph(hallazgo, s_body))
                if fix:
                    story.append(Spacer(1, 2 * mm))
                    story.append(Paragraph(f"<b>Cómo arreglarlo:</b> {fix}", s_fix))
                story.append(HRFlowable(width="100%", thickness=0.5,
                                        color=HexColor("#E2E4F0"),
                                        spaceBefore=4, spaceAfter=4))

        # ---- Fortalezas ----
        forts = [x for x in (report.get("fortalezas") or [])
                 if isinstance(x, str) and x.strip()][:5]
        if forts:
            story.append(Paragraph("Fortalezas detectadas", s_h2))
            for x in forts:
                story.append(Paragraph(
                    f"<font color=\"#2BA84A\">●</font> &nbsp;{x.strip()}", s_body))
                story.append(Spacer(1, 1.5 * mm))

        # ---- Acciones priorizadas ----
        accs = [a for a in (report.get("acciones") or [])][:5]
        if accs:
            story.append(Paragraph("Acciones priorizadas", s_h2))
            for i, a in enumerate(accs, 1):
                txt = a if isinstance(a, str) else str(a)
                story.append(Paragraph(f"<b>{i}.</b> &nbsp;{txt.strip()}", s_body))
                story.append(Spacer(1, 1.5 * mm))

        # ---- Pie ----
        story.append(Spacer(1, 8 * mm))
        story.append(HRFlowable(width="100%", thickness=2, color=GOLD,
                                spaceBefore=4, spaceAfter=6))
        story.append(Paragraph(
            "TALENTUM GROUP · Ingeniería de perfiles · "
            "¿Quieres que lo reconstruyamos para que pase los filtros?",
            ParagraphStyle("foot", parent=s_small, alignment=1,
                           textColor=NAVY, fontName="Helvetica-Bold"),
        ))

        doc.build(story)
        return buf.getvalue()
    except Exception:
        log.exception("build_report_pdf: fallo generando el reporte")
        return None
