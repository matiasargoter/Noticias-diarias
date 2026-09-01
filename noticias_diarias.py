#!/usr/bin/env python3
"""
Noticias Diarias Chile — Briefing Económico
Corre de lunes a viernes a las 18:30 CLT vía GitHub Actions.

Flujo:
  1. Dólar de cierre (Emol portada, fallback mindicador.cl)   → item #1 ("Top 5")
  2. Candidatos de la sección Economía de Emol del día (Nacional solo como respaldo)
  3. Bonus: "+ Comentado en Economía" (API interna de comentarios de Emol)
  4. Análisis IA (Google Gemini, free tier; Anthropic opcional) — selecciona y ordena
     las 4 noticias de Economía más relevantes y redacta, por noticia, un resumen de
     2-3 párrafos estilo periodista experto que integra la lectura económica y política
     para Chile, más "Lectura del día". Sin GEMINI_API_KEY/ANTHROPIC_API_KEY → modo básico.
  5. HTML (diseño cálido y claro) → se envía por Gmail SMTP como cuerpo del correo y
     como archivo .html adjunto, y se publica en GitHub Pages (PUBLIC_BASE_URL) con
     link "Ver en el navegador".
"""

import os
import re
import sys
import time
import json
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

CHILE_TZ = ZoneInfo("America/Santiago")


def now_chile() -> datetime:
    return datetime.now(CHILE_TZ)


# ─────────────────────────────────────────────────────────
# CONFIGURACIÓN  ← env vars (GitHub Actions) o fallback local
# ─────────────────────────────────────────────────────────
GMAIL_USER         = os.environ.get("GMAIL_USER",         "matiasargoter@gmail.com")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENT_EMAIL    = os.environ.get("RECIPIENT_EMAIL",    "matiasargoter@gmail.com")
OUTPUT_DIR         = os.environ.get("OUTPUT_DIR",         "/Users/matiasargote/Desktop/Noticias diarias")
GEMINI_API_KEY     = os.environ.get("GEMINI_API_KEY",     "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY",  "")
AI_MODEL           = os.environ.get("AI_MODEL",           "claude-opus-5")
DRY_RUN            = os.environ.get("DRY_RUN", "") not in ("", "0", "false", "False")
# URL pública del briefing publicado (GitHub Pages). Vacío = sin link.
PUBLIC_BASE_URL    = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
# ─────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

DAYS_ES = {
    "Monday": "Lunes", "Tuesday": "Martes", "Wednesday": "Miércoles",
    "Thursday": "Jueves", "Friday": "Viernes", "Saturday": "Sábado", "Sunday": "Domingo"
}
MONTHS_ES = {
    "January": "enero", "February": "febrero", "March": "marzo", "April": "abril",
    "May": "mayo", "June": "junio", "July": "julio", "August": "agosto",
    "September": "septiembre", "October": "octubre", "November": "noviembre", "December": "diciembre"
}

MAX_CANDIDATES = 12   # cuántos artículos del día se le pasan a la IA para que elija
TOP_N          = 4    # noticias de Economía en el cuerpo (el dólar ocupa el #1 → "Top 5")
MIN_ECO_POOL   = 7    # bajo este umbral se agregan candidatos de Nacional como respaldo


# ─────────────────────────────────────────────────────────
# UTILIDADES
# ─────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(f"[{now_chile().strftime('%H:%M:%S')}] {msg}", flush=True)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def first_sentences(text: str, max_chars: int = 650) -> str:
    """Primeras frases de un texto, cortadas de forma limpia."""
    text = _clean(text)
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    last = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return (text[:last + 1] if last > 120 else cut.rstrip() + "…")


def _extract_json(raw: str) -> dict:
    """Extrae el primer objeto JSON de una respuesta (tolera fences ```json)."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("sin objeto JSON en la respuesta")
    return json.loads(raw[start:end + 1])


# ─────────────────────────────────────────────────────────
# SCRAPING EMOL
# ─────────────────────────────────────────────────────────

def _extract_copete(soup: BeautifulSoup) -> str:
    for sel in [".copete", "#cuDetalle_cuCopete", "[class*='copete']", "[class*='bajada']"]:
        elem = soup.select_one(sel)
        if elem:
            text = _clean(elem.get_text(" ", strip=True))
            if len(text) > 40:
                return text
    return ""


def fetch_article_body(url: str, max_chars: int = 3600) -> str:
    """Descarga el artículo y devuelve copete + primeros párrafos (para la IA)."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        log(f"[WARN] No se pudo leer {url}: {e}")
        return ""

    parts = []
    copete = _strip_byline(_extract_copete(soup))
    if copete:
        parts.append(copete)

    for p in soup.find_all("p"):
        t = _strip_byline(_clean(p.get_text(" ", strip=True)))
        low = t.lower()
        if any(w in low for w in ["cookie", "suscrí", "suscri", "publicidad", "javascript",
                                  "newsletter", "©", "derechos reservados"]):
            continue
        if len(t) < 60 or t in parts:
            continue
        parts.append(t)
        if sum(len(x) for x in parts) > max_chars:
            break

    return _clean(" ".join(parts))[:max_chars]


_MESES_RE = ("enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
             "septiembre|setiembre|octubre|noviembre|diciembre")


def _strip_byline(text: str) -> str:
    """Quita la firma/fechado de Emol ('01 de Septiembre de 2026 | 13:29 | Por X, Emol.')."""
    text = re.sub(rf"\s*\d{{1,2}}\s+de\s+(?:{_MESES_RE})\s+de\s+\d{{4}}\s*\|\s*\d{{1,2}}:\d{{2}}"
                  r"(?:\s*\|\s*Por\s+[^.]+?)?\.?\s*$", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\s*\|\s*Por\s+[^|]+?,\s*Emol\.?\s*$", "", text, flags=re.IGNORECASE).strip()
    return text


def get_emol_dollar() -> dict:
    """Artículo del dólar de hoy en Emol + valor de cierre. Fallback: mindicador.cl."""
    today = now_chile()
    date_path = f"/{today.year}/{today.month:02d}/{today.day:02d}/"
    kw = ["dólar", "dolar", "dollar", "tipo de cambio", "mercado cambiario",
          "divisa", "peso chileno", "moneda"]

    art_url = art_title = None
    for search_url in ["https://www.emol.com/", "https://www.emol.com/noticias/Economia/"]:
        try:
            resp = requests.get(search_url, headers=HEADERS, timeout=15)
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.startswith("/"):
                    href = "https://www.emol.com" + href
                if date_path not in href or "/noticias/Economia/" not in href:
                    continue
                title = _clean(a.get_text(" ", strip=True))
                slug = href.rsplit("/", 1)[-1].lower()
                if any(k in title.lower() for k in kw) or "dolar" in slug or "cambiario" in slug:
                    art_url, art_title = href, title or "Mercado cambiario"
                    break
            if art_url:
                break
        except Exception as e:
            log(f"[WARN] Buscando dólar en {search_url}: {e}")

    value_str, body = None, ""
    if art_url:
        body = fetch_article_body(art_url)
        _, value_str = extract_dollar_value(body)

    if not value_str:
        for attempt in range(1, 4):
            try:
                r = requests.get("https://mindicador.cl/api/dolar", headers=HEADERS, timeout=10)
                serie = r.json()["serie"][0]
                val, val_date = serie["valor"], serie["fecha"][:10]
                value_str = f"${val:,.2f}".replace(",", ".")
                if val_date != today.strftime("%Y-%m-%d"):
                    value_str += f" (cierre {val_date})"
                    log(f"[WARN] Dólar mindicador.cl es de {val_date}, no de hoy")
                break
            except Exception as e:
                log(f"[WARN] mindicador.cl intento {attempt}/3: {e}")
                if attempt < 3:
                    time.sleep(4)
        if not value_str:
            value_str = "No disponible"

    return {
        "title":        art_title or f"Dólar cierra en {value_str}",
        "url":          art_url or "https://www.emol.com/economia/",
        "category":     "Mercado Cambiario",
        "summary":      "",
        "dollar_value": value_str,
        "is_dollar":    True,
        "_body":        body,
    }


def extract_dollar_value(text: str):
    patterns = [
        r'\$\s*(\d{3,4}[,\.]\d{1,2})',
        r'\$\s*(\d{3,4})\b',
        r'(\d{3,4}[,\.]\d{1,2})\s*(?:pesos|CLP)',
        r'(\d{3,4})\s*(?:pesos|CLP)\b',
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, text):
            try:
                val = float(m.group(1).replace(",", "."))
                if 700 < val < 1500:
                    return val, f"${val:,.2f}".replace(",", ".")
            except ValueError:
                pass
    return None, None


def get_emol_news() -> list:
    """
    Candidatos de hoy de la sección Economía de Emol (portada). Excluye el dólar.
    Solo si hay menos de MIN_ECO_POOL notas de Economía se agregan, como respaldo,
    notas de Nacional que tengan carga económica evidente.
    """
    today = now_chile()
    date_path = f"/{today.year}/{today.month:02d}/{today.day:02d}/"
    dollar_kw = {"dólar", "dollar", "tipo de cambio", "mercado cambiario"}

    eco, pol, seen_urls, seen_titles = [], [], set(), set()

    for page_url in ["https://www.emol.com/"]:
        try:
            resp = requests.get(page_url, headers=HEADERS, timeout=15, allow_redirects=True)
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            log(f"[WARN] Scrapeando {page_url}: {e}")
            continue

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/"):
                href = "https://www.emol.com" + href

            is_today    = date_path in href
            is_economia = "/noticias/Economia/" in href
            is_politica = "/noticias/Nacional/" in href or "/noticias/Politica/" in href
            if not (is_today and (is_economia or is_politica)) or href in seen_urls:
                continue

            title = _clean(a.get_text(" ", strip=True))
            if len(title) < 15:
                child = a.find(["h1", "h2", "h3", "h4", "h5", "strong", "span"])
                title = _clean(child.get_text(strip=True)) if child else ""

            key = title.lower()[:60]
            if len(title) < 15 or key in seen_titles:
                continue
            if any(k in title.lower() for k in dollar_kw):
                continue

            seen_urls.add(href)
            seen_titles.add(key)
            item = {
                "title":    title,
                "url":      href,
                "category": "Economía" if is_economia else "Nacional",
                "summary":  "",
                "is_dollar": False,
            }
            (eco if is_economia else pol).append(item)

    log(f"[INFO] Candidatos: {len(eco)} de Economía, {len(pol)} de Nacional (respaldo).")
    if len(eco) >= MIN_ECO_POOL:
        return eco
    return eco + pol


def get_most_viewed_bonus(exclude_urls: set) -> list:
    """2 artículos más comentados de '+ Comentado en Economía' (API interna de Emol)."""
    import html as html_module
    bonus, seen_urls, seen_titles = [], set(exclude_urls), set()
    try:
        api_url = ("https://cache-comentarios.ecn.cl/Comments/Api"
                   "?action=getMostCommentedPages&site=emol&siteSection=economia")
        resp = requests.get(api_url, headers={**HEADERS, "Referer": "https://www.emol.com/economia/"}, timeout=15)
        items = resp.json()
        for item in items:
            url = item.get("url", "").replace("http://", "https://")
            title = _clean(html_module.unescape(item.get("title", "")))
            key = title.lower()[:60]
            if not url or "/noticias/Economia/" not in url:
                continue
            if url in seen_urls or key in seen_titles:
                continue
            seen_urls.add(url)
            seen_titles.add(key)
            bonus.append({
                "title": title, "url": url, "category": "Economía",
                "summary": "", "is_dollar": False, "is_bonus": True,
            })
            if len(bonus) >= 2:
                break
    except Exception as e:
        log(f"[WARN] Obteniendo '+ Comentado en Economía': {e}")
    return bonus


# ─────────────────────────────────────────────────────────
# ANÁLISIS IA — Google Gemini (free tier) / Anthropic (opcional)
# ─────────────────────────────────────────────────────────

SYSTEM_ANALISTA = """Eres un periodista económico y analista de mercados senior. Redactas un briefing diario para Chile.

Reglas de fuente (estrictas):
- Trabajas EXCLUSIVAMENTE con el contenido de los artículos de Emol que te entrego en el mensaje.
- No incorpores cifras, hechos, cotizaciones ni declaraciones que no estén en ese texto.
- Tu lectura de impacto es razonamiento editorial sobre ese contenido, no un dato inventado.

Criterio editorial:
- Todas las noticias del cuerpo salen de la sección Economía de Emol. Si un candidato viene
  de la sección Nacional, inclúyelo SOLO si tiene impacto económico o de mercado directo.
- Prioriza noticias con consecuencias económicas reales por sobre las meramente declarativas.
- Relevancia de mayor a menor: Crítica, Alta, Media, Baja.
- Horizonte: "Corto plazo", "Mediano plazo" o "Largo plazo".

El "resumen" de cada noticia:
- Son 2 a 3 párrafos, separados por un salto de línea doble (\\n\\n). Nunca más de 3.
- Párrafo 1-2: cuenta la noticia como periodista experto — qué pasó, quién, las cifras
  y actores clave mencionados en el texto. Concreto, claro, sin relleno.
- Párrafo final: TU lectura, como analista, de cómo afecta esto a Chile en lo económico
  (dólar, tasas, inflación, cobre, inversión, crecimiento, empleo — lo que aplique) y en
  lo político (qué actor impulsa qué, qué traba o disputa existe). Sé neutral, sin postura
  partidista. Si algún plano no aplica, no lo menciones.
- El lector queda completamente informado sin abrir la fuente. Español de Chile, tono sobrio.
- Nada de markdown, viñetas ni títulos dentro del texto.

Respondes ÚNICAMENTE con un objeto JSON válido, sin texto antes ni después, sin fences."""

JSON_SHAPE = """{
  "resumen_ejecutivo": "2 a 4 frases: la lectura del día para Chile, hilando lo más importante.",
  "dolar": {
    "titular": "titular breve del cierre cambiario",
    "resumen": "2-3 párrafos (\\n\\n entre ellos) según las reglas: qué pasó con el peso/dólar y tu lectura económica y política.",
    "relevancia": "Baja|Media|Alta|Crítica",
    "horizonte": "Corto plazo|Mediano plazo|Largo plazo"
  },
  "noticias": [
    {
      "id": <número del candidato entre corchetes>,
      "titular": "titular breve y descriptivo",
      "resumen": "2-3 párrafos (\\n\\n entre ellos) según las reglas: la noticia + tu lectura económica y política para Chile.",
      "relevancia": "Baja|Media|Alta|Crítica",
      "horizonte": "Corto plazo|Mediano plazo|Largo plazo"
    }
  ],
  "bonus": [
    {
      "titular": "titular breve",
      "resumen": "2 párrafos (\\n\\n entre ellos): el tema, por qué genera debate y tu lectura de sus implicancias.",
      "relevancia": "Baja|Media|Alta|Crítica"
    }
  ]
}"""


def _build_prompt(dollar_item: dict, candidates: list, bonus: list) -> str:
    lines = [
        f"DÓLAR DE CIERRE (dato ya verificado, va como noticia #1): {dollar_item['dollar_value']}",
        "",
        "ARTÍCULO DEL DÓLAR (Emol Economía):",
        dollar_item.get("_body") or dollar_item["title"],
        "",
        f"CANDIDATOS DE LA SECCIÓN ECONOMÍA DE EMOL — elige y ordena los {TOP_N} más relevantes",
        "para la economía chilena (campo \"id\" = número entre corchetes; no repitas el tema del",
        "dólar). Los marcados (Nacional) son respaldo: úsalos solo si tienen impacto económico",
        "directo y superan a una nota de Economía:",
        "",
    ]
    for i, c in enumerate(candidates, 1):
        lines.append(f"[{i}] ({c['category']}) {c['title']}")
        body = c.get("_body") or ""
        if body:
            lines.append(f"    {first_sentences(body, 1700)}")
        lines.append("")

    if bonus:
        lines.append("CANDIDATOS BONUS — sección '+ Comentado en Economía'. Analízalos en este mismo "
                     f"orden, devuelve exactamente {len(bonus)} objetos en \"bonus\", no los reordenes:")
        lines.append("")
        for i, b in enumerate(bonus, 1):
            lines.append(f"(B{i}) {b['title']}")
            body = b.get("_body") or ""
            if body:
                lines.append(f"    {first_sentences(body, 1200)}")
            lines.append("")

    lines.append(f'"noticias" debe tener exactamente {TOP_N} objetos, del más al menos relevante. '
                 f'Devuelve SOLO este JSON (exactamente esta forma):')
    lines.append(JSON_SHAPE)
    return "\n".join(lines)


def _call_gemini(system: str, user: str) -> str:
    """Llama a Gemini (REST, free tier). Devuelve el texto (JSON) o lanza excepción."""
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": 0.35,
            "maxOutputTokens": 12000,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    r = requests.post(url, headers={"x-goog-api-key": GEMINI_API_KEY,
                                    "Content-Type": "application/json"},
                      json=payload, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    cand = (data.get("candidates") or [{}])[0]
    parts = cand.get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts)
    if not text:
        raise RuntimeError(f"Gemini sin texto (finishReason={cand.get('finishReason')})")
    return text


def _call_anthropic(system: str, user: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(model=AI_MODEL, max_tokens=16000,
                                  system=system, messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


def analyze_newsletter(dollar_item: dict, candidates: list, bonus: list):
    """Una llamada al modelo. Devuelve el dict de análisis o None (modo básico)."""
    if GEMINI_API_KEY:
        provider, call = "Gemini", _call_gemini
    elif ANTHROPIC_API_KEY:
        provider, call = "Anthropic", _call_anthropic
    else:
        log("[INFO] Sin GEMINI_API_KEY ni ANTHROPIC_API_KEY — briefing en modo básico.")
        return None

    prompt = _build_prompt(dollar_item, candidates, bonus)
    try:
        text = call(SYSTEM_ANALISTA, prompt)
        data = _extract_json(text)
        n_news = len(data.get("noticias", []))
        if not data.get("dolar") or n_news == 0:
            raise ValueError("JSON incompleto (sin dolar/noticias)")
        log(f"[OK] Análisis con {provider}: {n_news} noticias + dólar + lectura del día.")
        return data
    except Exception as e:
        log(f"[WARN] Falló el análisis IA ({provider}: {e}) — briefing en modo básico.")
        return None


def apply_analysis(dollar_item: dict, candidates: list, bonus: list, analysis: dict):
    """Fusiona el análisis de la IA en los items. Devuelve (top_noticias, exec_summary)."""
    dollar_item["analysis"] = analysis.get("dolar") or {}

    top, used = [], set()
    for n in analysis.get("noticias", []):
        idx = n.get("id")
        if not isinstance(idx, int) or not (1 <= idx <= len(candidates)) or idx in used:
            continue
        used.add(idx)
        base = candidates[idx - 1]
        base["analysis"] = n
        if n.get("titular"):
            base["title"] = n["titular"]
        top.append(base)
        if len(top) >= TOP_N:
            break

    for c in candidates:                     # completar si la IA devolvió < TOP_N
        if len(top) >= TOP_N:
            break
        if c not in top:
            c["summary"] = first_sentences(c.get("_body", ""))
            top.append(c)

    for b, ba in zip(bonus, analysis.get("bonus", [])):
        b["analysis"] = ba
        if ba.get("titular"):
            b["title"] = ba["titular"]
    for b in bonus:                           # bonus sin análisis → copete
        if "analysis" not in b:
            b["summary"] = first_sentences(b.get("_body", ""), 900)

    return top[:TOP_N], _clean(analysis.get("resumen_ejecutivo", ""))


# ─────────────────────────────────────────────────────────
# HTML
# ─────────────────────────────────────────────────────────

def date_in_spanish() -> str:
    raw = now_chile().strftime("%A, %d de %B de %Y")
    for en, es in {**DAYS_ES, **MONTHS_ES}.items():
        raw = raw.replace(en, es)
    return raw


# ── Paleta minimalista, cálida y clara ───────────────────
C = {
    "page":   "#FAF6EF",   # crema papel
    "panel":  "#F2EBDF",   # crema más profundo
    "card":   "#FFFDF8",   # blanco cálido
    "inset":  "#F6EFE2",   # bloque de impacto
    "line":   "#E7DDCB",   # borde cálido
    "text":   "#33291E",   # café muy oscuro (no negro)
    "muted":  "#6E6252",   # gris cálido
    "dim":    "#9C8F7C",   # gris cálido claro
    "indigo": "#B0752C",   # dorado (acento secundario)
    "cyan":   "#BE5B2A",   # terracota (acento primario)
}
MONO = "'SF Mono','SFMono-Regular',ui-monospace,'Roboto Mono',Menlo,Consolas,monospace"

REL_STYLE = {
    "Crítica": ("#F6DAD0", "#9E3418"),
    "Critica": ("#F6DAD0", "#9E3418"),
    "Alta":    ("#F5E3C6", "#8F5A15"),
    "Media":   ("#E4E7D6", "#5A6338"),
    "Baja":    ("#ECE5D6", "#7C7060"),
}


def _pill(text: str, bg: str, fg: str) -> str:
    return (f'<span style="display:inline-block;background:{bg};color:{fg};'
            f'border-radius:6px;padding:3px 9px;font-family:{MONO};font-size:10px;'
            f'font-weight:600;letter-spacing:.5px;text-transform:uppercase;'
            f'margin:0 6px 6px 0">{text}</span>')


def _rel_pill(rel: str) -> str:
    bg, fg = REL_STYLE.get(_clean(rel), REL_STYLE["Media"])
    return _pill(f"◆ {rel}", bg, fg)


def _mini_lbl(text: str, color: str) -> str:
    return (f'<div style="font-family:{MONO};font-size:9.5px;letter-spacing:2.5px;'
            f'text-transform:uppercase;color:{color};font-weight:600;margin-bottom:9px">{text}</div>')


def _paragraphs(text: str, color: str, size: str = "13.5px") -> str:
    parts = [p for p in re.split(r"\n\s*\n|\n", _clean_multiline(text)) if p.strip()]
    if not parts:
        return ""
    return "".join(
        f'<p style="margin:0 0 11px;font-size:{size};color:{color};line-height:1.72">{p.strip()}</p>'
        for p in parts
    )


def _clean_multiline(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _link_row(url: str) -> str:
    label = url.replace("https://", "").replace("http://", "")
    label = label if len(label) < 80 else label[:77] + "…"
    return (f'<div style="margin-top:16px;padding-top:13px;border-top:1px solid {C["line"]}">'
            f'<a href="{url}" style="font-family:{MONO};font-size:10.5px;color:{C["dim"]};'
            f'text-decoration:none;word-break:break-all">→ {label}</a></div>')


def _render_card(i: int, art: dict, accent: str) -> str:
    a = art.get("analysis") or {}
    num = f"0{i}" if i < 10 else str(i)
    meta = (
        f'<span style="font-family:{MONO};font-size:22px;font-weight:700;color:{accent};'
        f'letter-spacing:1px">{num}</span>'
        f'<span style="margin-left:12px;font-family:{MONO};font-size:9.5px;letter-spacing:2.5px;'
        f'font-weight:600;text-transform:uppercase;color:{C["dim"]}">{art["category"]}</span>'
    )

    pills = ""
    if a.get("relevancia"):
        pills += _rel_pill(a["relevancia"])
    if a.get("horizonte"):
        pills += _pill(a["horizonte"], "#EDE4D2", "#7A6B52")
    pills_row = f'<div style="margin-top:14px">{pills}</div>' if pills else ""

    if art.get("is_dollar"):
        dv = art["dollar_value"]
        size = "44px" if dv.strip().startswith("$") else "20px"
        headline = (f'<div style="font-family:{MONO};font-size:{size};font-weight:700;color:{C["text"]};'
                    f'letter-spacing:-1px;line-height:1.1;margin:18px 0 10px">{dv}</div>')
        title = (f'<h2 style="font-size:15px;font-weight:600;color:{C["muted"]};line-height:1.4;'
                 f'margin:0 0 4px">{a.get("titular") or art["title"]}</h2>')
    else:
        headline = ""
        title = (f'<h2 style="font-size:19px;font-weight:700;color:{C["text"]};line-height:1.35;'
                 f'letter-spacing:-.3px;margin:16px 0 4px">{a.get("titular") or art["title"]}</h2>')

    body_text = a.get("resumen", "") or art.get("summary", "")
    body_html = _paragraphs(body_text, C["muted"])
    body_html = f'<div style="margin-top:14px">{body_html}</div>' if body_html else ""

    return f"""
      <div class="card" style="background:{C['card']};border:1px solid {C['line']};border-radius:14px;padding:24px;margin-bottom:16px">
        <div style="display:flex;align-items:baseline">{meta}</div>
        {headline}
        {title}
        {pills_row}
        {body_html}
        {_link_row(art["url"])}
      </div>"""


def _render_bonus(art: dict) -> str:
    a = art.get("analysis") or {}
    accent = C["cyan"]
    pills = _rel_pill(a["relevancia"]) if a.get("relevancia") else ""
    title = a.get("titular") or art["title"]
    body_html = _paragraphs(a.get("resumen", "") or art.get("summary", ""), C["muted"], "12.5px")
    body_html = f'<div style="margin-top:12px">{body_html}</div>' if body_html else ""
    return f"""
      <div class="card" style="background:{C['card']};border:1px solid {C['line']};border-left:2px solid {accent};border-radius:12px;padding:20px 22px;margin-bottom:12px">
        <div style="font-family:{MONO};font-size:9.5px;letter-spacing:2.5px;font-weight:600;text-transform:uppercase;color:{accent}">
          &#128293; + Comentado en Economía
        </div>
        <h3 style="font-size:16px;font-weight:700;color:{C['text']};line-height:1.35;margin:14px 0 0">{title}</h3>
        <div style="margin-top:11px">{pills}</div>
        {body_html}
        {_link_row(art["url"])}
      </div>"""


def build_html(dollar_item: dict, articles: list, bonus: list,
               exec_summary: str = "", public_url: str = "") -> str:
    date_str = date_in_spanish()
    all_items = [dollar_item] + articles

    browser_bar = ""
    if public_url:
        browser_bar = (
            f'<div style="max-width:660px;margin:0 auto;padding:10px 30px;background:{C["panel"]};'
            f'border-bottom:1px solid {C["line"]};text-align:center">'
            f'<a href="{public_url}" style="font-family:{MONO};font-size:10px;letter-spacing:1.5px;'
            f'color:{C["cyan"]};text-decoration:none;text-transform:uppercase;font-weight:600">'
            f'Ver esta edición en el navegador →</a></div>'
        )

    ACCENTS = [C["cyan"], C["indigo"], "#7A8B4F", "#B0607A", "#A07C46"]
    cards_html = ""
    for i, art in enumerate(all_items, 1):
        cards_html += _render_card(i, art, ACCENTS[min(i - 1, len(ACCENTS) - 1)])

    exec_html = ""
    if exec_summary:
        exec_html = f"""
    <div style="background:{C['panel']};border:1px solid {C['line']};border-left:2px solid {C['cyan']};border-radius:14px;padding:20px 22px;margin-bottom:20px">
      {_mini_lbl("&#9613; Lectura del día", C['cyan'])}
      <p style="margin:0;font-size:14px;line-height:1.75;color:{C['text']}">{exec_summary}</p>
    </div>"""

    bonus_html = "".join(_render_bonus(b) for b in bonus)
    bonus_section = f"""
  <div style="padding:8px 30px 34px">
    <div style="font-family:{MONO};font-size:9.5px;letter-spacing:3px;text-transform:uppercase;color:{C['dim']};margin:0 0 16px;padding-top:22px;border-top:1px solid {C['line']};font-weight:600">
      Bonus · lo más comentado
    </div>
    {bonus_html}
  </div>""" if bonus_html else ""

    has_ai = any(it.get("analysis") for it in all_items)
    section_lbl = "Las noticias que mueven el mercado" if has_ai else "Las noticias más relevantes del día"

    legend = ""
    if has_ai:
        legend = (f'<div style="font-family:{MONO};margin:-4px 0 20px;font-size:9.5px;'
                  f'color:{C["dim"]};line-height:1.7;letter-spacing:.5px">'
                  'RELEVANCIA &nbsp; <span style="color:#9E3418">◆ CRÍTICA</span> &nbsp;'
                  '<span style="color:#8F5A15">◆ ALTA</span> &nbsp;'
                  '<span style="color:#5A6338">◆ MEDIA</span> &nbsp;'
                  '<span style="color:#7C7060">◆ BAJA</span></div>')

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>Briefing Económico Chile — {date_str}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Helvetica Neue', Arial, sans-serif;
    background: {C['page']};
    color: {C['text']};
    -webkit-font-smoothing: antialiased;
  }}
  a {{ color: {C['cyan']}; }}
  .wrap {{ max-width: 660px; margin: 0 auto; background: {C['page']}; }}
  .hd {{ padding: 44px 30px 30px; border-bottom: 1px solid {C['line']}; }}
  .brand {{ font-family: {MONO}; font-size: 10px; letter-spacing: 4px; text-transform: uppercase; color: {C['cyan']}; font-weight: 600; margin-bottom: 16px; }}
  .hd h1 {{ font-size: 30px; font-weight: 800; color: {C['text']}; letter-spacing: -1px; line-height: 1.1; margin-bottom: 12px; }}
  .hd .fecha {{ font-family: {MONO}; font-size: 11.5px; letter-spacing: 1px; color: {C['dim']}; text-transform: uppercase; }}
  .rule {{ height: 1px; background-color: {C['line']}; background: linear-gradient(90deg, {C['cyan']} 0%, {C['indigo']} 50%, transparent 100%); }}
  .body {{ padding: 26px 30px 20px; }}
  .section-lbl {{ font-family: {MONO}; font-size: 9.5px; letter-spacing: 3px; text-transform: uppercase; color: {C['dim']}; margin-bottom: 16px; padding-bottom: 14px; border-bottom: 1px solid {C['line']}; font-weight: 600; }}
  .ft {{ padding: 24px 30px 40px; border-top: 1px solid {C['line']}; }}
  .ft p {{ font-family: {MONO}; font-size: 9.5px; color: {C['dim']}; line-height: 1.9; letter-spacing: .5px; }}
  @media (max-width: 640px) {{
    .hd, .body, .ft, .wrap > div[style] {{ padding-left: 16px !important; padding-right: 16px !important; }}
    .card {{ padding: 18px !important; }}
    .hd h1 {{ font-size: 25px; }}
  }}
</style>
</head>
<body>
{browser_bar}
<div class="wrap">

  <div class="hd">
    <div class="brand">&#9670; Noticias Diarias &nbsp;·&nbsp; Emol Economía</div>
    <h1>Briefing Económico<br>&amp; Mercados</h1>
    <div class="fecha">{date_str}</div>
  </div>

  <div class="rule"></div>

  <div class="body">
    {exec_html}
    <div class="section-lbl">{section_lbl}</div>
    {legend}
    {cards_html}
  </div>

  {bonus_section}

  <div class="ft">
    <p>Fuente &nbsp; emol.com — Economía y "+ Comentado en Economía"<br>
    Selección y análisis editorial generados automáticamente · {date_str}<br>
    {f'<a href="{public_url}">{public_url}</a><br>' if public_url else ''}
    Este correo también trae el briefing adjunto como archivo .html.</p>
  </div>

</div>
</body>
</html>"""


def save_html_file(html: str) -> str:
    date_str = now_chile().strftime("%Y-%m-%d")
    path = os.path.join(OUTPUT_DIR, f"noticias_{date_str}.html")
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        with open(os.path.join(OUTPUT_DIR, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)   # index.html = última edición (raíz de GitHub Pages)
    except Exception as e:
        log(f"[WARN] No se pudo guardar el HTML: {e}")
    return path


# ─────────────────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────────────────

def send_email(subject: str, html_body: str, text_body: str = "", attach_path: str = "") -> None:
    if GMAIL_APP_PASSWORD in ("", "PONER_AQUI_CONTRASEÑA_DE_APP"):
        log("[ERROR] Falta configurar GMAIL_APP_PASSWORD")
        sys.exit(1)

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = RECIPIENT_EMAIL

    body = MIMEMultipart("alternative")
    if text_body:
        body.attach(MIMEText(text_body, "plain", "utf-8"))
    body.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(body)

    if attach_path and os.path.exists(attach_path):
        try:
            with open(attach_path, "rb") as f:
                part = MIMEApplication(f.read(), _subtype="html")
            part.add_header("Content-Disposition", "attachment",
                            filename=os.path.basename(attach_path))
            msg.attach(part)
        except Exception as e:
            log(f"[WARN] No se pudo adjuntar {attach_path}: {e}")

    for attempt in range(1, 4):
        try:
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as srv:
                srv.ehlo()
                srv.starttls()
                srv.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                srv.sendmail(GMAIL_USER, RECIPIENT_EMAIL, msg.as_string())
            log(f"[OK] Email enviado a {RECIPIENT_EMAIL}")
            return
        except Exception as e:
            log(f"[WARN] Intento {attempt}/3 fallido: {e}")
            if attempt < 3:
                time.sleep(15)

    log("[ERROR] No se pudo enviar el email tras 3 intentos.")
    sys.exit(1)


# ─────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────

def main() -> None:
    log("=== Noticias Diarias iniciando ===")

    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        clt_hour = now_chile().hour
        if clt_hour != 18:
            log(f"[INFO] Cron fuera de ventana (hora CLT: {clt_hour}h) — se omite.")
            sys.exit(0)

    log("Obteniendo dólar de cierre…")
    dollar_item = get_emol_dollar()
    log(f"Dólar: {dollar_item['dollar_value']} — {dollar_item['title'][:60]}")

    log("Scrapeando sección Economía de Emol…")
    candidates = get_emol_news()[:MAX_CANDIDATES]
    for c in candidates:
        c["_body"] = fetch_article_body(c["url"])

    exclude = {dollar_item["url"]} | {c["url"] for c in candidates}
    bonus = get_most_viewed_bonus(exclude)
    for b in bonus:
        b["_body"] = fetch_article_body(b["url"])
    log(f"Bonus '+ Comentado': {len(bonus)}")

    log("Analizando con IA…")
    analysis = analyze_newsletter(dollar_item, candidates, bonus)

    if analysis:
        top, exec_summary = apply_analysis(dollar_item, candidates, bonus, analysis)
    else:
        top = candidates[:TOP_N]
        for a in top:
            a["summary"] = first_sentences(a.get("_body", ""))
        for b in bonus:
            b["summary"] = first_sentences(b.get("_body", ""))
        exec_summary = ""

    if not top:
        log("[WARN] Sin noticias del día — correo solo con dólar y bonus.")

    log("Construyendo HTML…")
    public_url = (f"{PUBLIC_BASE_URL}/noticias_{now_chile().strftime('%Y-%m-%d')}.html"
                  if PUBLIC_BASE_URL else "")
    html = build_html(dollar_item, top, bonus, exec_summary, public_url)
    html_path = save_html_file(html)
    log(f"HTML guardado: {html_path}")
    if public_url:
        log(f"URL pública: {public_url}")

    today_str = now_chile().strftime("%d/%m/%Y")
    lead = ""
    if analysis and top:
        crit = next((a for a in top if (a.get("analysis") or {}).get("relevancia") in ("Crítica", "Critica", "Alta")), None)
        if crit:
            lead = " · " + (crit["analysis"].get("titular") or crit["title"])[:70]
    subject = f"📊 Briefing Chile | {today_str} · Dólar {dollar_item['dollar_value']}{lead}"

    text_lines = [subject, ""]
    if exec_summary:
        text_lines += [exec_summary, ""]
    for it in [dollar_item] + top:
        an = it.get("analysis") or {}
        text_lines.append("• " + (an.get("titular") or it["title"]))
    if public_url:
        text_lines += ["", f"Ver en el navegador: {public_url}"]
    text_lines += ["", "El briefing va también adjunto como archivo .html."]
    text_body = "\n".join(text_lines)

    if DRY_RUN:
        log(f"[DRY_RUN] No se envía email. Abre: open \"{html_path}\"")
    else:
        log("Enviando email vía Gmail SMTP…")
        send_email(subject, html, text_body, attach_path=html_path)

    log("=== Proceso completado ✓ ===")


if __name__ == "__main__":
    main()
