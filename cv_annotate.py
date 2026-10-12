"""Anotación experta del CV en PDF: marca DENTRO del documento lo que el
diagnóstico ATS detectó.

- Sello de puntaje TALENTUM en la primera página.
- Nota adhesiva por hallazgo, en la página relevante, con qué está mal y
  cómo arreglarlo. Color por severidad: rojo = crítico, naranja = medio,
  azul = leve.
- Resaltado de los fragmentos citados como evidencia en el hallazgo.

Nunca lanza: ante cualquier fallo devuelve None.
"""

import logging
import re

log = logging.getLogger("talentum")

try:
    import pymupdf
    _HAS_PYMUPDF = True
except Exception:  # pragma: no cover
    pymupdf = None
    _HAS_PYMUPDF = False

SEV_COLORS = {
    "critico": (0.90, 0.20, 0.20),
    "medio": (0.98, 0.60, 0.10),
    "leve": (0.30, 0.52, 0.92),
}
SEV_LABEL = {"critico": "CRÍTICO", "medio": "A MEJORAR", "leve": "DETALLE"}
NAVY = (0.04, 0.12, 0.27)
GOLD = (0.79, 0.64, 0.15)

_SCORE_COLORS = [
    (75, (0.16, 0.62, 0.30)),   # verde
    (50, (0.98, 0.60, 0.10)),   # naranja
    (0, (0.90, 0.20, 0.20)),    # rojo
]


def _score_color(score):
    try:
        s = int(score)
    except (TypeError, ValueError):
        return (0.5, 0.5, 0.5)
    for floor, color in _SCORE_COLORS:
        if s >= floor:
            return color
    return (0.5, 0.5, 0.5)


def _draw_cover(doc, report):
    """Portada de resumen como primera página (no tapa el contenido del CV)."""
    W, H = 595, 842  # A4
    page = doc.new_page(pno=0, width=W, height=H)
    # Banda superior navy con línea dorada.
    page.draw_rect(pymupdf.Rect(0, 0, W, 120), color=NAVY, fill=NAVY)
    page.draw_rect(pymupdf.Rect(0, 120, W, 124), color=GOLD, fill=GOLD)
    page.insert_text(pymupdf.Point(40, 55), "TALENTUM GROUP",
                     fontsize=28, fontname="helv", color=(0.79, 0.64, 0.15))
    page.insert_text(pymupdf.Point(40, 85), "Diagnóstico ATS · Ingeniería de perfiles",
                     fontsize=13, fontname="helv", color=(0.85, 0.87, 0.92))
    # Puntaje grande con color.
    score = report.get("indice_diagnostico")
    try:
        score_txt = str(int(score))
    except (TypeError, ValueError):
        score_txt = "--"
    scolor = _score_color(score)
    page.insert_text(pymupdf.Point(60, 260), score_txt,
                     fontsize=96, fontname="helv", color=scolor)
    page.insert_text(pymupdf.Point(60 + 96 * len(score_txt) * 0.55, 260), "/100",
                     fontsize=30, fontname="helv", color=(0.35, 0.35, 0.43))
    y = 300
    resumen = str(report.get("resumen") or "").strip().split("\n")[0]
    if resumen:
        for line in _wrap(resumen, 72)[:4]:
            page.insert_text(pymupdf.Point(60, y), line,
                             fontsize=11, fontname="helv", color=(0.1, 0.1, 0.18))
            y += 18
    # Leyenda de marcas.
    y += 20
    page.insert_text(pymupdf.Point(60, y), "Cómo leer las marcas en tu CV:",
                     fontsize=13, fontname="helv", color=NAVY)
    y += 26
    for sev, desc in (("critico", "freno real: corrígelo primero"),
                      ("medio", "te resta puntos en el filtro"),
                      ("leve", "pule para competir")):
        c = SEV_COLORS[sev]
        page.draw_oval(pymupdf.Rect(60, y - 12, 76, y + 4), color=c, fill=c)
        page.insert_text(pymupdf.Point(88, y),
                         f"{SEV_LABEL[sev]} — {desc}",
                         fontsize=11, fontname="helv", color=(0.1, 0.1, 0.18))
        y += 26
    page.insert_text(pymupdf.Point(60, y + 10),
                     "Toca cada marca del documento para ver qué está mal y cómo arreglarlo.",
                     fontsize=10, fontname="helv", color=(0.35, 0.35, 0.43))
    page.insert_text(pymupdf.Point(60, H - 40),
                     "TALENTUM GROUP · ¿Quieres que lo reconstruyamos para que pase los filtros?",
                     fontsize=10, fontname="helv", color=NAVY)


def _wrap(text, width):
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines


def _pages_from_text(hallazgo, n_pages):
    """Páginas mencionadas en el hallazgo ("página(s) (1, 2)", "página 3")."""
    pages = set()
    try:
        for m in re.finditer(r"p[aá]gina\(s\)\s*\(([\d,\s]+)\)", hallazgo or "", re.I):
            for num in re.findall(r"\d+", m.group(1)):
                p = int(num) - 1
                if 0 <= p < n_pages:
                    pages.add(p)
        for m in re.finditer(r"p[aá]gina\s+(\d+)", hallazgo or "", re.I):
            p = int(m.group(1)) - 1
            if 0 <= p < n_pages:
                pages.add(p)
    except Exception:
        pass
    return sorted(pages) or [0]


def _quoted_spans(hallazgo):
    """Fragmentos entre comillas citados como evidencia."""
    try:
        return re.findall(r"[\"“”]([^\"“”]{4,80})[\"“”]", hallazgo or "")
    except Exception:
        return []


def annotate_cv(pdf_bytes, report):
    """Devuelve el PDF con las marcas del diagnóstico, o None si falla."""
    if not _HAS_PYMUPDF or not pdf_bytes or not isinstance(report, dict):
        return None
    try:
        doc = pymupdf.open(stream=bytes(pdf_bytes), filetype="pdf")
    except Exception:
        log.exception("annotate_cv: no se pudo abrir el PDF")
        return None
    try:
        n = len(doc)
        if n == 0:
            return None
        findings = [f for f in (report.get("criterios") or []) if isinstance(f, dict)]

        # 1. Portada de resumen (primera página): no tapa el contenido del CV.
        #    Las páginas originales se desplazan +1.
        _draw_cover(doc, report)
        n = len(doc)
        OFF = 1

        # 2. Nota adhesiva por hallazgo + resaltado de evidencia citada.
        for f in findings:
            sev = str(f.get("severidad") or "leve").lower()
            color = SEV_COLORS.get(sev, SEV_COLORS["leve"])
            tag = SEV_LABEL.get(sev, "DETALLE")
            criterio = str(f.get("criterio") or "Hallazgo").strip()
            hallazgo = str(f.get("hallazgo") or "").strip()
            fix = str(f.get("como_arreglarlo") or "").strip()
            body = hallazgo
            if fix:
                body += "\n\n✓ Cómo arreglarlo: " + fix
            title = f"TALENTUM · {tag} · {criterio}"
            pages = _pages_from_text(hallazgo, n - OFF)
            for pno in pages[:2]:
                page = doc[pno + OFF]
                pr = page.rect
                # Icono de nota en la esquina superior derecha.
                point = pymupdf.Point(pr.x1 - 28, pr.y0 + 44)
                annot = page.add_text_annot(point, body[:1200])
                try:
                    annot.set_info(title=title[:120])
                    annot.set_colors(stroke=color)
                    annot.update()
                except Exception:
                    pass
                # Resaltar fragmentos citados como evidencia.
                for quote in _quoted_spans(hallazgo):
                    try:
                        for inst in page.search_for(quote):
                            hl = page.add_highlight_annot(inst)
                            hl.set_colors(stroke=color)
                            hl.update()
                    except Exception:
                        continue
        out = doc.tobytes(deflate=True, garbage=4)
        try:
            doc.close()
        except Exception:
            pass
        return bytes(out)
    except Exception:
        log.exception("annotate_cv: fallo anotando el PDF")
        try:
            doc.close()
        except Exception:
            pass
        return None
