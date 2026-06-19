#!/usr/bin/env python3
"""
Noticias Diarias Chile — Top 5 Economía & Política
Corre de lunes a viernes a las 17:00 CLT via GitHub Actions.
"""

import os
import re
import sys
import time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

CHILE_TZ = ZoneInfo("America/Santiago")


def now_chile() -> datetime:
    return datetime.now(CHILE_TZ)


# ─────────────────────────────────────────────────────────
# CONFIGURACIÓN  ← Lee desde env vars (GitHub Actions) o usa fallback local
# ─────────────────────────────────────────────────────────
GMAIL_USER         = os.environ.get("GMAIL_USER",         "matiasargoter@gmail.com")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENT_EMAIL    = os.environ.get("RECIPIENT_EMAIL",    "matiasargoter@gmail.com")
OUTPUT_DIR         = os.environ.get("OUTPUT_DIR",         "/Users/matiasargote/Desktop/Noticias diarias")
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


# ─────────────────────────────────────────────────────────
# DÓLAR DESDE EMOL
# ─────────────────────────────────────────────────────────

def extract_dollar_value(text: str):
    """
    Busca el valor de cierre del dólar en texto libre.
    Retorna (valor_float, string_formateado) o (None, None).
    """
    patterns = [
        r'\$\s*(\d{3,4}[,\.]\d{1,2})',   # $915,50 o $915.50
        r'\$\s*(\d{3,4})\b',              # $915
        r'(\d{3,4}[,\.]\d{1,2})\s*(?:pesos|CLP)',
        r'(\d{3,4})\s*(?:pesos|CLP)\b',
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, text):
            val_str = m.group(1).replace(",", ".")
            try:
                val = float(val_str)
                if 700 < val < 1500:
                    return val, f"${val:,.2f}".replace(",", ".")
            except ValueError:
                pass
    return None, None


def get_emol_dollar() -> dict:
    """
    Busca el artículo del dólar de hoy en Emol, extrae el valor de cierre
    y construye el item de noticia #1.
    Fallback: mindicador.cl (Banco Central).
    """
    today = now_chile()
    date_path = f"/{today.year}/{today.month:02d}/{today.day:02d}/"
    dollar_keywords = ["dólar", "dollar", "tipo de cambio", "mercado cambiario", "divisa"]

    # Buscar artículo del dólar en Emol economía
    dollar_article_url = None
    dollar_article_title = None

    try:
        resp = requests.get("https://www.emol.com/economia/", headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/"):
                href = "https://www.emol.com" + href
            if date_path not in href:
                continue
            title = a.get_text(separator=" ", strip=True)
            if any(kw in title.lower() for kw in dollar_keywords):
                dollar_article_url = href
                dollar_article_title = title
                break
    except Exception as e:
        log(f"[WARN] Error buscando artículo del dólar: {e}")

    # Extraer valor y resumen del artículo
    value_str = None
    summary = ""

    if dollar_article_url:
        try:
            resp = requests.get(dollar_article_url, headers=HEADERS, timeout=12)
            soup = BeautifulSoup(resp.text, "html.parser")
            full_text = soup.get_text(" ", strip=True)

            # Extraer valor de cierre
            _, value_str = extract_dollar_value(full_text)

            # Extraer resumen (copete o primer párrafo)
            summary = _extract_one_paragraph(soup)
        except Exception as e:
            log(f"[WARN] Error leyendo artículo del dólar: {e}")

    # Fallback: mindicador.cl (Banco Central) — verificar que sea de hoy
    if not value_str:
        try:
            r = requests.get("https://mindicador.cl/api/dolar", headers=HEADERS, timeout=10)
            serie = r.json()["serie"][0]
            val = serie["valor"]
            val_date = serie["fecha"][:10]   # "2026-06-19"
            today_str = today.strftime("%Y-%m-%d")
            value_str = f"${val:,.2f}".replace(",", ".")
            if val_date != today_str:
                value_str += f" (cierre {val_date})"
                log(f"[WARN] Dólar de mindicador.cl es de {val_date}, no de hoy ({today_str})")
        except Exception:
            value_str = "No disponible"

    title_display = dollar_article_title or f"Dólar cierra en {value_str}"

    return {
        "title":        title_display,
        "url":          dollar_article_url or "https://www.emol.com/economia/",
        "category":     "Mercado Cambiario",
        "summary":      summary,
        "dollar_value": value_str,
        "is_dollar":    True,
    }


# ─────────────────────────────────────────────────────────
# NOTICIAS
# ─────────────────────────────────────────────────────────

def get_emol_news() -> list:
    """
    Scrape Emol para artículos de hoy en Economía y Nacional/Política.
    Excluye artículos sobre el dólar (van en el item #1).
    """
    today = now_chile()
    date_path = f"/{today.year}/{today.month:02d}/{today.day:02d}/"
    dollar_keywords = {"dólar", "dollar", "tipo de cambio", "mercado cambiario"}

    candidates = []
    seen_urls: set = set()
    seen_titles: set = set()

    pages = [
        "https://www.emol.com/economia/",
        "https://www.emol.com/noticias/Economia/",
        "https://www.emol.com/noticias/Nacional/",
        "https://www.emol.com/",
    ]

    for page_url in pages:
        try:
            resp = requests.get(page_url, headers=HEADERS, timeout=15, allow_redirects=True)
            soup = BeautifulSoup(resp.text, "html.parser")

            for a in soup.find_all("a", href=True):
                href: str = a["href"]
                if href.startswith("/"):
                    href = "https://www.emol.com" + href

                is_today    = date_path in href
                is_economia = "/noticias/Economia/" in href
                is_politica = "/noticias/Nacional/" in href or "/noticias/Politica/" in href

                if not (is_today and (is_economia or is_politica)):
                    continue
                if href in seen_urls:
                    continue

                title = a.get_text(separator=" ", strip=True)
                if not title or len(title) < 15:
                    child = a.find(["h1", "h2", "h3", "h4", "h5", "strong", "span"])
                    title = child.get_text(strip=True) if child else ""
                title = re.sub(r"\s+", " ", title).strip()

                title_key = title.lower()[:60]
                if not title or len(title) < 15 or title_key in seen_titles:
                    continue

                # Excluir noticias sobre el dólar (van en #1)
                if any(kw in title.lower() for kw in dollar_keywords):
                    continue

                seen_urls.add(href)
                seen_titles.add(title_key)

                candidates.append({
                    "title":    title,
                    "url":      href,
                    "category": "Economía" if is_economia else "Política / Nacional",
                    "summary":  "",
                    "is_dollar": False,
                })

        except Exception as e:
            log(f"[WARN] Error scrapeando {page_url}: {e}")

    return candidates


def get_most_viewed_bonus(exclude_urls: set) -> list:
    """
    Extrae los 2 primeros artículos de la sección '+ Comentando en Economía' de Emol.
    No filtra por fecha: la sección puede incluir artículos de días anteriores.
    """
    bonus      = []
    seen_urls  = set(exclude_urls)
    seen_titles: set = set()

    try:
        resp = requests.get("https://www.emol.com/economia/", headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Localizar el encabezado "+ Comentando en Economía"
        section = None
        for tag in soup.find_all(["h2", "h3", "h4", "div", "span", "p"]):
            txt = tag.get_text(strip=True).lower()
            if "comentando" in txt and "econom" in txt:
                section = tag.find_parent(["section", "div", "ul", "aside", "nav", "article"])
                if section:
                    break

        if not section:
            log("[WARN] No se encontró la sección '+ Comentando en Economía' en Emol")
            return bonus

        for a in section.find_all("a", href=True):
            href: str = a["href"]
            if href.startswith("/"):
                href = "https://www.emol.com" + href

            if "/noticias/Economia/" not in href:
                continue
            if href in seen_urls:
                continue

            title = a.get_text(separator=" ", strip=True)
            title = re.sub(r"\s+", " ", title).strip()
            title_key = title.lower()[:60]
            if not title or len(title) < 15 or title_key in seen_titles:
                continue

            seen_urls.add(href)
            seen_titles.add(title_key)
            bonus.append({
                "title":     title,
                "url":       href,
                "category":  "Economía",
                "summary":   "",
                "is_dollar": False,
                "is_bonus":  True,
            })
            if len(bonus) >= 2:
                break

    except Exception as e:
        log(f"[WARN] Error obteniendo '+ Comentando en Economía': {e}")

    return bonus


def _extract_one_paragraph(soup: BeautifulSoup) -> str:
    """Extrae el primer párrafo completo y significativo de un artículo."""
    # Intentar copete/bajada primero
    for sel in [".copete", "#cuDetalle_cuCopete", "[class*='copete']", "[class*='bajada']"]:
        elem = soup.select_one(sel)
        if elem:
            text = elem.get_text(strip=True)
            if len(text) > 40:
                return text

    # Fallback: primer párrafo sustancial
    for p in soup.find_all("p"):
        text = p.get_text(strip=True)
        skip = any(w in text.lower() for w in ["cookie", "suscri", "publicidad", "javascript", "©"])
        if not skip and len(text) > 80:
            # Cortar al final de la primera oración si el texto es muy largo
            if len(text) > 350:
                cut = text[:350]
                last_dot = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
                if last_dot > 100:
                    text = text[:last_dot + 1]
                else:
                    text = cut.rstrip() + "…"
            return text

    return ""


def get_article_summary(url: str) -> str:
    """Obtiene 1 párrafo de resumen desde la URL del artículo."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        soup = BeautifulSoup(resp.text, "html.parser")
        return _extract_one_paragraph(soup)
    except Exception as e:
        log(f"[WARN] No se pudo obtener resumen de {url}: {e}")
        return ""


# ─────────────────────────────────────────────────────────
# HTML
# ─────────────────────────────────────────────────────────

def date_in_spanish() -> str:
    raw = now_chile().strftime("%A, %d de %B de %Y")
    for en, es in {**DAYS_ES, **MONTHS_ES}.items():
        raw = raw.replace(en, es)
    return raw


def _render_card(i: int, art: dict, palette: list) -> str:
    """Renderiza un card de noticia con su paleta asignada."""
    bg_b, tx_b, bg_c, border, cat_c = palette
    summary_html = f'<p class="summary">{art["summary"]}</p>' if art.get("summary") else ""
    link_label   = art["url"].replace("https://", "").replace("http://", "")
    link_label   = link_label if len(link_label) < 82 else link_label[:79] + "…"
    badge_label  = f"0{i}" if i < 10 else str(i)

    if art.get("is_dollar"):
        return f"""
      <div class="card" style="background:{bg_c};border-left:4px solid {border};border-radius:12px;padding:24px;margin-bottom:12px">
        <div style="margin-bottom:14px">
          <span style="background:{bg_b};color:{tx_b};border-radius:20px;padding:5px 14px;font-size:12px;font-weight:800;letter-spacing:.5px;display:inline-block">{badge_label}</span>
          <span style="margin-left:10px;font-size:9px;letter-spacing:2.5px;font-weight:700;text-transform:uppercase;color:{cat_c}">★ Cierre del día · Mercado Cambiario</span>
        </div>
        <div style="font-size:54px;font-weight:900;color:{cat_c};letter-spacing:-3px;line-height:1;margin-bottom:10px">{art["dollar_value"]}</div>
        <h2 style="font-size:16px;font-weight:700;color:#1A1230;line-height:1.4;margin-bottom:10px">{art["title"]}</h2>
        {summary_html}
        <div style="margin-top:10px;padding-top:10px;border-top:1px solid {border}55">
          <a href="{art["url"]}" style="font-size:11px;color:{cat_c};text-decoration:none;opacity:.8;word-break:break-all">↗ {link_label}</a>
        </div>
      </div>"""

    return f"""
      <div class="card" style="background:{bg_c};border-left:4px solid {border};border-radius:12px;padding:24px;margin-bottom:12px">
        <div style="margin-bottom:12px">
          <span style="background:{bg_b};color:{tx_b};border-radius:20px;padding:5px 14px;font-size:12px;font-weight:800;letter-spacing:.5px;display:inline-block">{badge_label}</span>
          <span style="margin-left:10px;font-size:9px;letter-spacing:2px;font-weight:700;text-transform:uppercase;color:{cat_c}">{art["category"]}</span>
        </div>
        <h2 style="font-size:17px;font-weight:700;color:#1A1230;line-height:1.38;margin-bottom:10px">{art["title"]}</h2>
        {summary_html}
        <div style="margin-top:10px;padding-top:10px;border-top:1px solid {border}55">
          <a href="{art["url"]}" style="font-size:11px;color:{cat_c};text-decoration:none;opacity:.75;word-break:break-all">↗ {link_label}</a>
        </div>
      </div>"""


def build_html(dollar_item: dict, articles: list, bonus: list) -> str:
    date_str  = date_in_spanish()
    all_items = [dollar_item] + articles

    # Paleta pastel top-5 (bg_badge, text_badge, bg_card, border_color, cat_color)
    PALETTE = [
        ("#FEF3C7", "#92400E", "#FFFDF5", "#FCD34D", "#B45309"),  # 1 dorado  — dólar
        ("#EDE9FE", "#4C1D95", "#FDFCFF", "#C4B5FD", "#6D28D9"),  # 2 violeta
        ("#DBEAFE", "#1E3A8A", "#FAFCFF", "#93C5FD", "#1D4ED8"),  # 3 azul
        ("#D1FAE5", "#064E3B", "#FAFFFE", "#6EE7B7", "#059669"),  # 4 menta
        ("#FFE4E6", "#881337", "#FFFAFA", "#FDA4AF", "#BE123C"),  # 5 rosa
    ]
    # Paleta bonus — naranja cálido
    BONUS_PALETTE = ("#FFEDD5", "#9A3412", "#FFFBF5", "#FDBA74", "#EA580C")

    cards_html = ""
    for i, art in enumerate(all_items, 1):
        cards_html += _render_card(i, art, PALETTE[min(i - 1, 4)])

    bonus_html = ""
    for art in bonus:
        link_label = art["url"].replace("https://", "").replace("http://", "")
        link_label = link_label if len(link_label) < 82 else link_label[:79] + "…"
        summary_b  = f'<p class="summary">{art["summary"]}</p>' if art.get("summary") else ""
        bg_b, tx_b, bg_c, border, cat_c = BONUS_PALETTE
        bonus_html += f"""
      <div class="card" style="background:{bg_c};border-left:4px solid {border};border-radius:12px;padding:24px;margin-bottom:12px">
        <div style="margin-bottom:12px">
          <span style="background:{bg_b};color:{tx_b};border-radius:20px;padding:5px 14px;font-size:12px;font-weight:800;letter-spacing:.5px;display:inline-block">🔥</span>
          <span style="margin-left:10px;font-size:9px;letter-spacing:2px;font-weight:700;text-transform:uppercase;color:{cat_c}">{art["category"]} · + Comentando</span>
        </div>
        <h2 style="font-size:17px;font-weight:700;color:#1A1230;line-height:1.38;margin-bottom:10px">{art["title"]}</h2>
        {summary_b}
        <div style="margin-top:10px;padding-top:10px;border-top:1px solid {border}55">
          <a href="{art["url"]}" style="font-size:11px;color:{cat_c};text-decoration:none;opacity:.75;word-break:break-all">↗ {link_label}</a>
        </div>
      </div>"""

    bonus_section = f"""
  <div style="background:#FFF8F3;border-top:2px dashed #FDBA7466;padding:24px 32px 32px">
    <div style="font-size:9px;letter-spacing:4px;text-transform:uppercase;color:#FDBA74;margin-bottom:18px;padding-bottom:14px;border-bottom:1px dashed #FDBA7444;font-weight:700">
      Bonus · + Comentando en Economía
    </div>
    {bonus_html}
  </div>""" if bonus_html else ""

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Noticias Diarias Chile — {date_str}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Helvetica Neue', Arial, sans-serif;
    background: #F0EDFF;
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{
    max-width: 660px;
    margin: 32px auto;
    border-radius: 22px;
    overflow: hidden;
    box-shadow: 0 10px 50px rgba(100, 80, 200, 0.13);
  }}
  .hd {{
    background: linear-gradient(135deg, #E4DAFF 0%, #C6DFFF 100%);
    padding: 44px 40px 36px;
    text-align: center;
  }}
  .eyebrow {{
    font-size: 10px;
    letter-spacing: 5px;
    text-transform: uppercase;
    color: #7C5CBF;
    font-weight: 700;
    margin-bottom: 10px;
  }}
  .hd h1 {{
    font-size: 26px;
    font-weight: 800;
    color: #1E1245;
    letter-spacing: -0.5px;
    margin-bottom: 7px;
  }}
  .hd .fecha {{
    font-size: 13px;
    color: #7B6BAE;
  }}
  .divider {{
    height: 3px;
    background: linear-gradient(90deg, #A78BFA 0%, #60A5FA 100%);
  }}
  .body {{
    background: #F8F6FF;
    padding: 24px 32px 32px;
  }}
  .section-lbl {{
    font-size: 9px;
    letter-spacing: 4px;
    text-transform: uppercase;
    color: #B0A4D8;
    margin-bottom: 18px;
    padding-bottom: 14px;
    border-bottom: 1px dashed #DDD8F5;
    font-weight: 700;
  }}
  .card {{ transition: box-shadow .2s; }}
  .card:hover {{ box-shadow: 0 4px 20px rgba(100,80,200,.1); }}
  .summary {{
    font-size: 13.5px;
    color: #5C5070;
    line-height: 1.75;
    margin-bottom: 0;
  }}
  .ft {{
    background: #EDE9FF;
    padding: 18px 40px;
    text-align: center;
  }}
  .ft p {{
    font-size: 10px;
    color: #A99EC0;
    line-height: 1.8;
  }}
  @media (max-width: 640px) {{
    .hd, .body, .ft {{ padding-left: 20px; padding-right: 20px; }}
  }}
</style>
</head>
<body>
<div class="wrap">

  <div class="hd">
    <div class="eyebrow">Noticias Diarias</div>
    <h1>Top 5 — Economía &amp; Política</h1>
    <div class="fecha">{date_str}</div>
  </div>

  <div class="divider"></div>

  <div class="body">
    <div class="section-lbl">Las 5 noticias más relevantes del día · Chile</div>
    {cards_html}
  </div>

  {bonus_section}

  <div class="ft">
    <p>Fuente: <strong>emol.com</strong> &nbsp;·&nbsp; Generado automáticamente &nbsp;·&nbsp; {date_str}</p>
  </div>

</div>
</body>
</html>"""


def save_html_file(html: str) -> str:
    date_str = now_chile().strftime("%Y-%m-%d")
    path = os.path.join(OUTPUT_DIR, f"noticias_{date_str}.html")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        log(f"[WARN] No se pudo guardar el HTML en disco: {e}")
    return path


# ─────────────────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────────────────

def send_email(subject: str, html_body: str) -> None:
    if GMAIL_APP_PASSWORD in ("", "PONER_AQUI_CONTRASEÑA_DE_APP"):
        log("[ERROR] Falta configurar GMAIL_APP_PASSWORD")
        sys.exit(1)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html_body, "html", "utf-8"))

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
            log(f"[WARN] Intento {attempt}/3 fallido al enviar email: {e}")
            if attempt < 3:
                time.sleep(15)

    log("[ERROR] No se pudo enviar el email después de 3 intentos.")
    sys.exit(1)


# ─────────────────────────────────────────────────────────
# UTILIDADES
# ─────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(f"[{now_chile().strftime('%H:%M:%S')}] {msg}", flush=True)


# ─────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────

def main() -> None:
    log("=== Noticias Diarias iniciando ===")

    # Guard DST: con dos crons (invierno/verano), solo ejecutar en la ventana correcta (18:xx CLT).
    # En runs manuales (workflow_dispatch) se omite el chequeo para poder probar a cualquier hora.
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        clt_hour = now_chile().hour
        if clt_hour != 18:
            log(f"[INFO] Cron fuera de ventana (hora CLT actual: {clt_hour}h) — se omite esta ejecución.")
            sys.exit(0)

    # 1. Dólar (siempre #1)
    log("Obteniendo dólar de cierre desde Emol…")
    dollar_item = get_emol_dollar()
    log(f"Dólar: {dollar_item['dollar_value']} — {dollar_item['title'][:60]}")

    # 2. Noticias del día
    log("Scrapeando Emol (Economía + Nacional)…")
    articles = get_emol_news()
    log(f"Artículos candidatos: {len(articles)}")

    if not articles:
        log("[WARN] No se encontraron artículos del día en Emol — se enviará correo solo con dólar y bonus.")

    # 3. Resúmenes (1 párrafo c/u)
    log("Obteniendo resúmenes…")
    for art in articles[:6]:
        art["summary"] = get_article_summary(art["url"])
        log(f"  · {art['title'][:65]}…")

    top4 = articles[:4]   # Dólar ocupa el #1, quedan 4 noticias

    # 4. Bonus: noticias más vistas en Economía
    log("Obteniendo bonus (más vistas en Economía)…")
    used_urls = {dollar_item["url"]} | {a["url"] for a in top4}
    bonus = get_most_viewed_bonus(used_urls)
    for art in bonus:
        art["summary"] = get_article_summary(art["url"])
        log(f"  · [BONUS] {art['title'][:65]}…")

    # 5. Construir HTML
    log("Construyendo HTML…")
    html = build_html(dollar_item, top4, bonus)

    # 5. Guardar archivo HTML
    html_path = save_html_file(html)
    log(f"Archivo HTML guardado: {html_path}")

    # 6. Enviar email
    today_str = now_chile().strftime("%d/%m/%Y")
    subject   = f"📰 Top 5 Chile | {today_str} · Dólar {dollar_item['dollar_value']}"
    log("Enviando email vía Gmail SMTP…")
    send_email(subject, html)

    log("=== Proceso completado ✓ ===")
    log(f"Abre el HTML en tu navegador: open \"{html_path}\"")


if __name__ == "__main__":
    main()
