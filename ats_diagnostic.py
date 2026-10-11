"""Capa DETERMINÍSTICA del motor híbrido de diagnóstico ATS — TALENTUM GROUP.

Analiza un CV en PDF con reglas fijas (sin LLM) y produce hallazgos
accionables, un índice 0-100 de rúbrica interna y fortalezas.

Contrato de interfaz (lo consume la capa semántica/LLM):
    Finding = {"criterio": str, "severidad": "critico"|"medio"|"leve",
               "hallazgo": str, "como_arreglarlo": str}
    diagnose(pdf_bytes, job_posting_text=None, semantic_fn=None) -> dict

diagnose() NUNCA lanza excepciones: cualquier fallo catastrófico se
devuelve como reporte con un hallazgo crítico que describe el fallo.
"""

import re
from collections import Counter

try:
    import pdfplumber
except ImportError:  # diagnose() lo captura y lo reporta de forma controlada
    pdfplumber = None

NOTA_METODOLOGIA = (
    "Índice TALENTUM: rúbrica interna 0-100. No es el puntaje de ningún ATS; "
    "cada sistema (Workday, Taleo, SuccessFactors, etc.) parsea y puntúa distinto."
)

_PDFPLUMBER_MSG = (
    "pdfplumber no está instalado. Instálalo con: pip install pdfplumber"
)

_SEVERIDADES = ("critico", "medio", "leve")
_ORDEN_SEV = {"critico": 0, "medio": 1, "leve": 2}
_PENALIZACION = {"critico": 15, "medio": 7, "leve": 3}

# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _finding(criterio, severidad, hallazgo, como_arreglarlo):
    if severidad not in _SEVERIDADES:
        severidad = "leve"
    return {
        "criterio": str(criterio),
        "severidad": severidad,
        "hallazgo": str(hallazgo),
        "como_arreglarlo": str(como_arreglarlo),
    }


def _norm_line(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


# ---------------------------------------------------------------------------
# parse_pdf
# ---------------------------------------------------------------------------

def _band_lines(page):
    """Líneas de texto en las bandas superior/inferior (8% de la altura)."""
    h = page.height
    top_words, bottom_words = [], []
    for w in page.extract_words() or []:
        if w["top"] < 0.08 * h:
            top_words.append(w)
        if w["bottom"] > 0.92 * h:
            bottom_words.append(w)

    def _group(words):
        words = sorted(words, key=lambda w: (w["top"], w["x0"]))
        lines, cur, cur_top = [], [], None
        for w in words:
            if cur_top is None or abs(w["top"] - cur_top) <= 3:
                cur.append(w)
                cur_top = w["top"] if cur_top is None else cur_top
            else:
                lines.append(" ".join(x["text"] for x in cur))
                cur, cur_top = [w], w["top"]
        if cur:
            lines.append(" ".join(x["text"] for x in cur))
        return [_norm_line(l) for l in lines if _norm_line(l)]

    return _group(top_words), _group(bottom_words)


def _page_is_multicolumn(page):
    """Dos columnas si el centro está vacío y ambos lados tienen texto.

    Heurística: histograma de 20 bines en X; los 2 bines centrales deben
    tener <8% de los caracteres y cada mitad >25%.
    """
    chars = page.chars or []
    if len(chars) < 200:
        return False
    w = page.width
    nb = 20
    bins = [0] * nb
    for c in chars:
        x = (c["x0"] + c["x1"]) / 2.0
        i = min(nb - 1, max(0, int(x / w * nb)))
        bins[i] += 1
    total = sum(bins)
    if total == 0:
        return False
    mid = sum(bins[nb // 2 - 1: nb // 2 + 1])
    left = sum(bins[: nb // 2 - 1])
    right = sum(bins[nb // 2 + 1:])
    return mid < 0.08 * total and left > 0.25 * total and right > 0.25 * total


def _count_tables(page):
    """Tablas con al menos 2 columnas (artefactos de 1 columna se descartan)."""
    try:
        tables = page.extract_tables() or []
    except Exception:
        return 0
    n = 0
    for t in tables:
        if t and any(len([c for c in row if c]) >= 2 for row in t):
            n += 1
    return n


def parse_pdf(pdf_bytes):
    """Extrae texto y señales de maquetación de un PDF.

    Lanza ImportError controlado si pdfplumber no está instalado.
    """
    if pdfplumber is None:
        raise ImportError(_PDFPLUMBER_MSG)
    if not pdf_bytes:
        raise ValueError("pdf_bytes vacío: no se recibió ningún documento.")

    pages_text, chars_per_page, tables_pp = [], [], []
    multicol_pages, image_only_pages = [], []
    band_counter = Counter()
    n_images = 0

    with pdfplumber.open(__import__("io").BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            pages_text.append(text)
            chars_per_page.append(len(text))
            tables_pp.append(_count_tables(page))
            n_images += len(page.images or [])
            if _page_is_multicolumn(page):
                multicol_pages.append(i)
            if (page.images or []) and len(text) < 50:
                image_only_pages.append(i)
            try:
                top_lines, bottom_lines = _band_lines(page)
            except Exception:
                top_lines, bottom_lines = [], []
            for line in set(top_lines + bottom_lines):
                band_counter[line] += 1

    full_text = "\n".join(pages_text)
    words = len(full_text.split())
    repeated = sorted([l for l, c in band_counter.items() if c >= 2])

    return {
        "text": full_text,
        "pages": len(pages_text),
        "words": words,
        "chars": len(full_text),
        "chars_per_page": chars_per_page,
        "tables": sum(tables_pp),
        "images": n_images,
        "layout": {
            "is_multicolumn": bool(multicol_pages),
            "multicolumn_pages": multicol_pages,
            "image_only_pages": image_only_pages,
            "repeated_band_lines": repeated,
            "tables_per_page": tables_pp,
        },
    }


# ---------------------------------------------------------------------------
# deterministic_checks
# ---------------------------------------------------------------------------

_SECCIONES = {
    "experiencia": ["experiencia laboral", "experiencia profesional", "experiencia",
                    "trayectoria profesional", "trayectoria", "experience",
                    "work experience", "employment history"],
    "educacion": ["educación", "educacion", "formación académica", "formacion academica",
                  "formación", "formacion", "estudios", "education",
                  "academic background"],
    "habilidades": ["habilidades técnicas", "habilidades", "competencias",
                    "aptitudes", "destrezas", "skills", "technical skills",
                    "core competencies"],
    "contacto": ["información de contacto", "informacion de contacto",
                 "datos personales", "datos de contacto", "contacto", "contact"],
    "perfil": ["perfil profesional", "perfil", "resumen ejecutivo", "resumen",
               "extracto", "objetivo profesional", "objetivo", "profile",
               "summary", "professional summary"],
    "certificaciones": ["certificaciones", "certificación", "certificados",
                        "certifications", "certification"],
    "idiomas": ["idiomas", "languages", "language"],
}
_SECCIONES_NUCLEO = ("experiencia", "educacion", "habilidades", "contacto")

_RE_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_RE_PHONE = re.compile(
    r"\+593[\s\-.()]*\d[\d\s\-.()]{7,}\d"      # +593 98 470 5397, +593-9-8470-5397
    r"|\b09\d{8}\b"                             # 0984705397
    r"|\b0[2-7]\d{7}\b"                         # fijos Ecuador 022xxx...
    r"|\+\d{1,3}[\s\-.]\d[\d\s\-.]{6,}\d"       # internacionales
)
_RE_LINKEDIN = re.compile(r"linkedin\.com/in/[\w\-/]+", re.IGNORECASE)
_RE_LOCATION = re.compile(
    r"\b(Quito|Guayaquil|Cuenca|Manta|Ambato|Loja|Riobamba|Ibarra|Machala|"
    r"Esmeraldas|Portoviejo|Santo Domingo|El Coca|Francisco de Orellana|"
    r"Orellana|Sucumb[ií]os|Napo|Pastaza|Morona Santiago|Zamora Chinchipe|"
    r"Bogot[aá]|Lima|Medell[ií]n|Madrid|Ecuador|Colombia|Per[uú]|Chile|"
    r"Estados Unidos|USA|Espa[ñn]a)\b",
    re.IGNORECASE,
)
_RE_BULLET = re.compile(
    r"^(?:•|·|▪|‣|\*|-|–|—|\(cid:\d+\)|\d{1,2}[.)])\s*"
)
_RE_QUANT = re.compile(
    r"\d|%|\$|\bUSD\b|años?|meses?|d[ií]as?|years?|months?|millones?|miles?",
    re.IGNORECASE,
)
# Rango de fechas: 2020–2024, Jul 2025 – Ago 2026, 2019 - presente, etc.
_RE_RANGE = re.compile(
    r"\b((?:19|20)\d{2})\s*[–—\-]\s*"
    r"(?:([A-Za-záéíóúñ]{3,}\.?)\s*)?"
    r"((?:19|20)\d{2}|presente|actualidad|actual|hoy|current|now)\b",
    re.IGNORECASE,
)
# Fecha suelta: 07/2024, Jul 2025, (2024)
_RE_DATE_ANY = re.compile(
    r"\b\d{1,2}[/\-.]\d{1,2}[/\-.](?:19|20)\d{2}\b"
    r"|\b(?:ene|feb|mar|abr|may|jun|jul|ago|sep|oct|nov|dic|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?:19|20)\d{2}\b"
    r"|\b(?:19|20)\d{2}\b",
    re.IGNORECASE,
)
_RE_MES_ANIO = re.compile(
    r"(?:ene|feb|mar|abr|may|jun|jul|ago|sep|oct|nov|dic|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?:19|20)\d{2}",
    re.IGNORECASE,
)
_RE_ANIO_SOLO = re.compile(r"\(\s*(?:19|20)\d{2}\s*\)")

_MESES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
          "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12,
          "jan": 1, "apr": 4, "aug": 8, "dec": 12}

_STOPWORDS = frozenset("""
de la el en y a los del se las por un para con no una su al lo como mas pero sus
le ya o este si porque esta entre cuando muy sin sobre tambien me hasta hay
donde quien desde todo nos durante todos uno les ni contra otros ese eso ante
ellos esto mi antes algunos que unos yo otro otras otra el tanto esa estos
mucho quienes nada muchos cual poco ella estar estas algo mis tu te ti tus
ellas nosotros nuestros vuestra vuestros esta estas estan este estos eso aquel
aquella aquello cuyo cuya cuyos cuyas donde cuando cuanto cuanta cuantos
cuantas hay ser son fue fueron era eran esto aquello ello este esta estos
the and of to in for with on at from by as an are was were be been has have
had will would can
""".split())


def _es_encabezado_seccion(linea):
    """True si la línea es un título de sección (p. ej. '— EDUCACIÓN').

    Evita contar los encabezados como viñetas: muchos CV usan '— ' como
    ornamento antes del título de sección.
    """
    s = re.sub(r"^(?:•|·|▪|‣|\*|-|–|—|\(cid:\d+\)|\d{1,2}[.)])\s*",
               "", linea.strip()).strip().lower()
    if not s or len(s) > 45:
        return False
    for sinonimos in _SECCIONES.values():
        for syn in sinonimos:
            if s == syn or (s.startswith(syn) and len(s) - len(syn) <= 12):
                return True
    return False


def _bullet_lines(text):
    """Líneas con viñeta, excluyendo encabezados de sección."""
    out = []
    for ln in text.split("\n"):
        s = ln.strip()
        if s and _RE_BULLET.match(s) and not _es_encabezado_seccion(s):
            out.append(s)
    return out


def _detectar_secciones(text):
    """Devuelve {seccion: True/False}. 'contacto' vale si hay encabezado
    o si email+teléfono están presentes (bloque de contacto sin título)."""
    low = text.lower()
    found = {}
    for sec, sinonimos in _SECCIONES.items():
        hit = any(s in low for s in sinonimos)
        found[sec] = hit
    email = bool(_RE_EMAIL.search(text))
    phone = bool(_RE_PHONE.search(text))
    if email and phone:
        found["contacto"] = True
    return found, email, phone


def deterministic_checks(parsed, job_posting_text=None):
    """Aplica las reglas determinísticas y devuelve [Finding]."""
    findings = []
    text = parsed.get("text") or ""
    n_chars = parsed.get("chars", 0)
    layout = parsed.get("layout", {}) or {}
    pages = parsed.get("pages", 0)
    words = parsed.get("words", 0)

    # 1. Extracción de texto ------------------------------------------------
    image_only = layout.get("image_only_pages", []) or []
    if n_chars < 100 or image_only:
        findings.append(_finding(
            "Texto extraíble",
            "critico",
            "PDF sin texto extraíble (posible documento escaneado). "
            f"Se recuperaron {n_chars} caracteres en {pages} página(s).",
            "Generar el CV desde un editor (Word/Google Docs) y exportarlo "
            "como PDF con texto seleccionable; si es un escaneo, pasarlo por "
            "OCR antes de postular.",
        ))
        # Sin texto no hay nada más que analizar con honestidad.
        return findings

    # 2. Tablas --------------------------------------------------------------
    n_tables = parsed.get("tables", 0) or 0
    if n_tables > 0:
        findings.append(_finding(
            "Tablas en el documento",
            "medio",
            f"Se detectaron {n_tables} estructura(s) tabular(es) con 2+ columnas. "
            "Los ATS suelen leer las tablas por filas o por columnas y mezclan "
            "el contenido.",
            "Reemplazar las tablas por texto lineal con encabezados y viñetas; "
            "reservar tablas solo para el diseño visual final, no para el PDF "
            "que se sube a portales.",
        ))

    # 3. Varias columnas ------------------------------------------------------
    multicol_pages = layout.get("multicolumn_pages", []) or []
    if multicol_pages:
        findings.append(_finding(
            "Diseño en varias columnas",
            "medio",
            f"Se detectó maquetación en dos columnas en "
            f"{len(multicol_pages)} página(s) "
            f"({', '.join(str(p + 1) for p in multicol_pages)}). Los ATS leen "
            "de izquierda a derecha y pueden intercalar el contenido de ambas "
            "columnas.",
            "Usar una sola columna en el PDF para portales de empleo; dejar el "
            "diseño de dos columnas solo para envíos directos por correo.",
        ))

    # 4. Encabezado/pie repetido con datos clave ------------------------------
    repeated = layout.get("repeated_band_lines", []) or []
    key_tokens = ("@", "linkedin", "+593", "593", "09", "tel", "phone",
                  "correo", "email", "www.", "http")
    flagged = [l for l in repeated
               if any(t in l for t in key_tokens)
               or _RE_EMAIL.search(l) or _RE_PHONE.search(l)
               or _RE_LINKEDIN.search(l)]
    if flagged:
        findings.append(_finding(
            "Datos clave en encabezado/pie repetido",
            "leve",
            "Encabezado o pie de página repetido que contiene datos de "
            "contacto. Algunos ATS lo leen en cada página y duplican la "
            "información o la asocian mal.",
            "Mantener nombre y datos de contacto solo en la primera página, "
            "en el cuerpo del documento, no en el encabezado/pie del editor.",
        ))

    # 5. Secciones -------------------------------------------------------------
    secciones, email_ok, phone_ok = _detectar_secciones(text)
    for sec in _SECCIONES_NUCLEO:
        if not secciones.get(sec):
            findings.append(_finding(
                f"Sección faltante: {sec}",
                "medio",
                f"No se detectó encabezado de '{sec}'. Los ATS y reclutadores "
                "buscan estas secciones estándar para clasificar el CV.",
                f"Agregar una sección titulada '{sec.capitalize()}' con el "
                "contenido correspondiente.",
            ))

    # 6. Datos de contacto ------------------------------------------------------
    if not email_ok:
        findings.append(_finding(
            "Correo electrónico ausente",
            "critico",
            "No se detectó ninguna dirección de correo en el documento.",
            "Incluir un correo profesional vigente en el bloque de contacto, "
            "en texto plano (no dentro de una imagen).",
        ))
    if not phone_ok:
        findings.append(_finding(
            "Teléfono ausente",
            "medio",
            "No se detectó número de teléfono en el documento.",
            "Incluir un número de contacto en texto plano, con código de país "
            "si se postula fuera de Ecuador (p. ej. +593 ...).",
        ))
    if not _RE_LINKEDIN.search(text):
        findings.append(_finding(
            "LinkedIn ausente",
            "leve",
            "No se detectó URL de LinkedIn en el documento.",
            "Agregar la URL del perfil de LinkedIn (linkedin.com/in/usuario) "
            "en el bloque de contacto.",
        ))

    # 7. Cuantificación de logros -------------------------------------------------
    bullet_lines = _bullet_lines(text)
    n_bullets = len(bullet_lines)
    if n_bullets == 0:
        findings.append(_finding(
            "Sin viñetas detectadas",
            "leve",
            "No se detectaron listas con viñetas. Las viñetas ayudan al "
            "parseo por secciones.",
            "Estructurar la experiencia con viñetas estándar (• o -), una por "
            "logro o responsabilidad.",
        ))
        quant_ratio = None
    else:
        n_quant = sum(1 for l in bullet_lines if _RE_QUANT.search(l))
        quant_ratio = n_quant / n_bullets
        if quant_ratio < 0.3:
            findings.append(_finding(
                "Logros sin cuantificar",
                "medio",
                f"Solo {n_quant} de {n_bullets} viñetas incluyen cifras "
                f"({quant_ratio:.0%}). Los logros sin números pesan menos "
                "tanto para el ATS como para el reclutador.",
                "Añadir cifras a cada logro: %, montos, plazos, tamaños de "
                "equipo o volúmenes (p. ej. 'reduje costos 12%').",
            ))

    # 8. Longitud -----------------------------------------------------------------
    if pages > 3:
        findings.append(_finding(
            "Documento extenso",
            "medio",
            f"El CV tiene {pages} páginas. Documentos largos se parsean peor "
            "y se leen menos.",
            "Condensar a 2 páginas máximo: conservar lo relevante de los "
            "últimos 10 años y mover el resto a LinkedIn.",
        ))
    if words < 150:
        findings.append(_finding(
            "Contenido muy breve",
            "medio",
            f"Solo {words} palabras detectadas. Un CV tan breve deja pocas "
            "señales para el parseo y la clasificación.",
            "Desarrollar experiencia, habilidades y logros hasta un mínimo de "
            "~300 palabras de contenido real.",
        ))
    elif words > 1200:
        findings.append(_finding(
            "Contenido muy denso",
            "leve",
            f"{words} palabras detectadas. El exceso de texto diluye las "
            "palabras clave.",
            "Priorizar: recortar descripciones genéricas y dejar una línea "
            "por logro relevante.",
        ))

    # 9. Fechas --------------------------------------------------------------------
    ranges = list(_RE_RANGE.finditer(text))
    any_date = bool(ranges) or bool(_RE_DATE_ANY.search(text))
    if not any_date:
        findings.append(_finding(
            "Fechas no detectadas",
            "leve",
            "No se detectaron fechas claras de experiencia o educación. Sin "
            "fechas, el ATS no puede ordenar la trayectoria.",
            "Indicar mes y año de inicio/fin en cada experiencia y año en "
            "educación (p. ej. 'Ene 2022 – Dic 2024').",
        ))
    else:
        invertidos = []
        for m in ranges:
            y1, mes_txt, y2 = m.group(1), m.group(2), m.group(3)
            if y2.isdigit() and int(y2) < int(y1):
                invertidos.append(m.group(0).strip())
        if invertidos:
            findings.append(_finding(
                "Rangos de fecha invertidos",
                "leve",
                "Se detectaron rangos donde el año final es anterior al "
                f"inicial: {', '.join(invertidos[:3])}. Revisar: puede ser un "
                "error de digitación.",
                "Corregir los rangos o confirmar que las fechas son correctas.",
            ))
        # Solapamientos entre rangos año–año
        spans = []
        for m in ranges:
            y1, y2 = m.group(1), m.group(3)
            if y2.isdigit():
                spans.append((int(y1), int(y2), m.group(0).strip()))
        solapados = []
        for i in range(len(spans)):
            for j in range(i + 1, len(spans)):
                a1, a2 = spans[i][0], spans[i][1]
                b1, b2 = spans[j][0], spans[j][1]
                if max(a1, b1) < min(a2, b2):
                    solapados.append(f"{spans[i][2]} / {spans[j][2]}")
        if solapados:
            findings.append(_finding(
                "Rangos de fecha solapados",
                "leve",
                "Se detectaron rangos de fechas que se solapan: "
                f"{'; '.join(solapados[:2])}. Puede ser empleo simultáneo real "
                "o un error.",
                "Si fue empleo simultáneo, aclararlo en el texto; si no, "
                "corregir las fechas.",
            ))
        # Formatos inconsistentes: "Mes Año" vs "(Año)" suelto
        if _RE_MES_ANIO.search(text) and _RE_ANIO_SOLO.search(text):
            findings.append(_finding(
                "Formatos de fecha inconsistentes",
                "leve",
                "Se mezclan formatos de fecha ('Ene 2022 – Dic 2024' junto a "
                "'(2024)'). Los parseadores trabajan mejor con un formato único.",
                "Unificar: usar 'Mes Año – Mes Año' en experiencia y 'Año' "
                "solo en educación, de forma consistente.",
            ))

    # 10. Coincidencia de términos con la oferta ------------------------------------
    if job_posting_text and job_posting_text.strip():
        tokens = re.findall(r"[a-záéíóúñü]{4,}", job_posting_text.lower())
        freq = Counter(t for t in tokens if t not in _STOPWORDS)
        top = [t for t, _ in freq.most_common(40)]
        cv_tokens = set(re.findall(r"[a-záéíóúñü]{4,}", text.lower()))
        presentes = [t for t in top if t in cv_tokens]
        faltantes = [t for t in top if t not in cv_tokens][:8]
        detalle_falt = (f" Faltantes frecuentes: {', '.join(faltantes)}."
                        if faltantes else " Sin faltantes entre los 40 principales.")
        findings.append(_finding(
            "Coincidencia de términos con la oferta (no es puntaje ATS)",
            "medio",
            f"{len(presentes)}/{len(top)} términos frecuentes de la oferta "
            f"aparecen en el CV.{detalle_falt} Esto mide solapamiento de "
            "vocabulario, no es el puntaje de ningún ATS.",
            "Incorporar con honestidad los términos de la oferta que sí "
            "describan la experiencia real; no agregar palabras clave sin "
            "sustento.",
        ))

    # Señales de calidad de extracción (no punitivas en exceso) ----------------------
    n_cid = len(re.findall(r"\(cid:\d+\)", text))
    if n_cid:
        findings.append(_finding(
            "Símbolos no mapeados en la extracción",
            "leve",
            f"{n_cid} símbolo(s) del PDF no pudieron convertirse a texto "
            "(aparecen como códigos CID). Suelen ser viñetas o separadores "
            "con una fuente sin mapa Unicode; algunos ATS los leerán como "
            "basura.",
            "Reemplazar viñetas y separadores decorativos por caracteres "
            "estándar (•, -, |) con fuentes comunes.",
        ))

    n_images = parsed.get("images", 0) or 0
    if n_images and not image_only:
        findings.append(_finding(
            "Imágenes incrustadas",
            "leve",
            f"El documento contiene {n_images} imagen(es) incrustada(s); no se "
            "pudo determinar su contenido. Si son fotos o elementos "
            "decorativos, algunos ATS las procesan mal.",
            "Retirar fotos y gráficos decorativos del PDF que se sube a "
            "portales; el texto debe transmitirlo todo.",
        ))

    return findings


# ---------------------------------------------------------------------------
# Puntuación, fortalezas, acciones
# ---------------------------------------------------------------------------

def _score_findings(findings):
    """100 - 15 por crítico, -7 por medio, -3 por leve. Mínimo 0."""
    score = 100
    for f in findings:
        score -= _PENALIZACION.get(f.get("severidad"), 0)
    return max(0, score)


def _derive_strengths(parsed, findings, quant_ratio=None):
    fortalezas = []
    text = parsed.get("text") or ""

    if quant_ratio is not None and quant_ratio >= 0.6:
        n_b = len(_bullet_lines(text))
        fortalezas.append(
            f"Logros cuantificados: {quant_ratio:.0%} de las viñetas "
            f"({n_b} detectadas) incluyen cifras, plazos o porcentajes."
        )

    secciones, email_ok, phone_ok = _detectar_secciones(text)
    if all(secciones.get(s) for s in _SECCIONES_NUCLEO):
        fortalezas.append(
            "Estructura completa: secciones de experiencia, educación, "
            "habilidades y contacto presentes y detectables."
        )

    if email_ok and phone_ok and _RE_LINKEDIN.search(text) and _RE_LOCATION.search(text):
        fortalezas.append(
            "Bloque de contacto completo: correo, teléfono, LinkedIn y "
            "ubicación en texto plano."
        )

    words = parsed.get("words", 0)
    pages = parsed.get("pages", 0)
    if 300 <= words <= 1200 and 1 <= pages <= 2:
        fortalezas.append(
            f"Longitud adecuada para parseo ATS: {words} palabras en "
            f"{pages} página(s)."
        )

    if not any(f.get("severidad") == "critico" for f in findings):
        fortalezas.append("Texto extraíble en todas las páginas: ninguna "
                          "página depende de imágenes para su contenido.")

    return fortalezas


def _derive_actions(findings, limite=5):
    ordenados = sorted(findings, key=lambda f: _ORDEN_SEV.get(f.get("severidad"), 9))
    acciones, vistos = [], set()
    for f in ordenados:
        fix = (f.get("como_arreglarlo") or "").strip()
        if fix and fix not in vistos:
            acciones.append(fix)
            vistos.add(fix)
        if len(acciones) >= limite:
            break
    return acciones


def _quant_ratio_of(text):
    bullets = _bullet_lines(text)
    if not bullets:
        return None
    n_q = sum(1 for l in bullets if _RE_QUANT.search(l))
    return n_q / len(bullets)


def _build_resumen(indice, findings, semantica):
    n = len(findings)
    c = sum(1 for f in findings if f.get("severidad") == "critico")
    m = sum(1 for f in findings if f.get("severidad") == "medio")
    l = sum(1 for f in findings if f.get("severidad") == "leve")
    linea1 = (f"Índice TALENTUM: {indice}/100 — {n} hallazgo(s): "
              f"{c} crítico(s), {m} medio(s), {l} leve(s).")
    if findings:
        top = sorted(findings, key=lambda f: _ORDEN_SEV.get(f.get("severidad"), 9))[0]
        linea2 = f"Prioridad: {top.get('como_arreglarlo', '')}"
    else:
        linea2 = "Sin hallazgos: el documento supera la capa determinística."
    linea3 = ("Incluye revisión semántica contra la oferta."
              if semantica else
              "Solo capa determinística; sin revisión semántica contra oferta.")
    return "\n".join([linea1, linea2, linea3])


# ---------------------------------------------------------------------------
# diagnose
# ---------------------------------------------------------------------------

def _coerce_semantic_hallazgos(raw):
    """Normaliza hallazgos semánticos a forma Finding sin lanzar."""
    out = []
    if not isinstance(raw, list):
        return out
    for h in raw:
        if not isinstance(h, dict):
            continue
        out.append(_finding(
            h.get("criterio", "Hallazgo semántico"),
            h.get("severidad", "leve"),
            h.get("hallazgo", ""),
            h.get("como_arreglarlo", ""),
        ))
    return out


def _dedupe(seq):
    vistos, out = set(), []
    for x in seq:
        if x not in vistos:
            out.append(x)
            vistos.add(x)
    return out


def diagnose(pdf_bytes, job_posting_text=None, semantic_fn=None):
    """Diagnóstico ATS determinístico (+ semántico si se provee semantic_fn).

    NUNCA lanza: ante fallo catastrófico devuelve un reporte con un
    hallazgo crítico que describe el fallo.
    """
    try:
        parsed = parse_pdf(pdf_bytes)
        findings = deterministic_checks(parsed, job_posting_text)
        det_index = _score_findings(findings)
        quant_ratio = _quant_ratio_of(parsed.get("text") or "")
        fortalezas = _derive_strengths(parsed, findings, quant_ratio)
        acciones = _derive_actions(findings)

        revision_semantica = False
        if semantic_fn is not None:
            try:
                sem = semantic_fn(parsed.get("text") or "", job_posting_text)
            except Exception:
                sem = None
            if isinstance(sem, dict):
                sem_hallazgos = _coerce_semantic_hallazgos(sem.get("hallazgos"))
                sem_index = _score_findings(sem_hallazgos)
                findings = findings + sem_hallazgos
                fortalezas = _dedupe(fortalezas + [str(x) for x in
                                                   (sem.get("fortalezas") or [])
                                                   if str(x).strip()])
                acciones = _dedupe(acciones + [str(x) for x in
                                               (sem.get("acciones") or [])
                                               if str(x).strip()])
                det_index = round(0.6 * det_index + 0.4 * sem_index)
                revision_semantica = True

        return {
            "resumen": _build_resumen(det_index, findings, revision_semantica),
            "criterios": findings,
            "indice_diagnostico": int(det_index),
            "fortalezas": fortalezas,
            "acciones": acciones,
            "revision_semantica_disponible": revision_semantica,
            "nota_metodologia": NOTA_METODOLOGIA,
        }
    except Exception as exc:  # noqa: BLE001 — contrato: nunca lanzar
        fallo = _finding(
            "Fallo del diagnóstico",
            "critico",
            f"No se pudo completar el diagnóstico determinístico: {exc}. "
            "No se pudo determinar el estado del documento.",
            "Verificar que el archivo sea un PDF válido y no esté corrupto o "
            "protegido con contraseña, e reintentar.",
        )
        return {
            "resumen": ("No se pudo completar el diagnóstico: fallo interno "
                        "controlado.\nPrioridad: validar el archivo PDF e "
                        "reintentar.\nSolo capa determinística; sin revisión "
                        "semántica."),
            "criterios": [fallo],
            "indice_diagnostico": 0,
            "fortalezas": [],
            "acciones": [fallo["como_arreglarlo"]],
            "revision_semantica_disponible": False,
            "nota_metodologia": NOTA_METODOLOGIA,
        }
