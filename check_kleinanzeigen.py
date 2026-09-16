#!/usr/bin/env python3
"""
Kleinanzeigen Monitor
----------------------
Ruft eine Liste gespeicherter Kleinanzeigen.de-Suchen ab, vergleicht die
Treffer mit dem letzten Lauf (data/state.json), erkennt neue Inserate und
Preisänderungen, baut daraus ein statisches Dashboard (docs/index.html)
und verschickt bei Treffern eine Push-Benachrichtigung über ntfy.sh.

WICHTIG:
- Kleinanzeigen.de verbietet automatisierten Zugriff in seiner robots.txt.
  Dieses Skript ist für den rein privaten Eigengebrauch gedacht (Ersatz für
  manuelles taegliches Nachschauen), nicht fuer Massenabfragen oder Weiter-
  gabe der Daten.
- Die HTML-Struktur von Kleinanzeigen.de kann sich jederzeit aendern. Wenn
  ploetzlich 0 Treffer geparst werden, meldet das Skript das explizit ueber
  ntfy, damit es auffaellt - dann muessen ggf. die CSS-Selektoren unten
  (siehe SELECTORS) angepasst werden.
"""

import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
SEARCHES_FILE = BASE_DIR / "searches.json"
STATE_FILE = BASE_DIR / "data" / "state.json"
DASHBOARD_FILE = BASE_DIR / "docs" / "index.html"

# Für die im Browser laufende Notizen-Synchronisation (siehe build_dashboard):
# das GitHub-Repo, in das die Anmerkungen (data/annotations.json) über die
# GitHub-API zurückgeschrieben werden.
GITHUB_REPO_SLUG = "Reik98/kleinanzeigen-monitor"
GITHUB_BRANCH = "main"
ANNOTATIONS_PATH = "data/annotations.json"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}" if NTFY_TOPIC else None

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9",
}

# Basierend auf der tatsächlichen Seitenstruktur (Stand: Sept. 2026,
# analysiert anhand eines echten debug_page.html-Exports). Kleinanzeigen.de
# nutzt inzwischen ein neues Frontend (Astro/Tailwind) ohne stabile
# CSS-Klassennamen - deshalb stützen wir uns auf die stabilen data-Attribute
# statt auf Klassennamen, wo immer möglich.
LISTING_CARD_SELECTOR = "article[data-adid]"
EMPTY_RESULT_SELECTOR = "#saved-search-empty-result"
ALT_ADS_MARKER = "altads"  # Container-IDs, die "ähnliche Anzeigen andernorts" enthalten


def load_json(path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def parse_price_to_number(price_text):
    """'15.000 €' -> 15000 ; 'VB' / 'Zu verschenken' -> None"""
    if not price_text:
        return None
    digits = re.sub(r"[^\d]", "", price_text)
    return int(digits) if digits else None


def is_inside_alt_ads(card):
    """True, wenn die Karte in einem 'ähnliche Anzeigen andernorts'-Container
    steckt (z.B. id='srchrslt-adtable-altads') - solche Karten sind KEINE
    echten Treffer für die eigene Suche und muessen ausgeschlossen werden."""
    parent = card
    while parent is not None:
        pid = parent.get("id") if hasattr(parent, "get") else None
        if pid and ALT_ADS_MARKER in pid:
            return True
        parent = parent.parent
    return False


def extract_price(card):
    """Sucht den Preis anhand des €-Zeichens statt anhand von (instabilen)
    Tailwind-Klassennamen."""
    for p in card.find_all("p"):
        text = p.get_text(strip=True)
        if "€" in text:
            return text
    return ""


def extract_image(card):
    """Sucht das Titelbild der Anzeige (falls vorhanden)."""
    img_el = card.select_one("[data-image-container] img") or card.select_one("img")
    if not img_el:
        return None
    return img_el.get("src")


SIZE_PATTERN = re.compile(r"(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?)\s*(?:m²|m2|qm)\b", re.IGNORECASE)
GEMARKUNG_PATTERN = re.compile(r"Gemarkung:?\s*([A-ZÄÖÜ][\wäöüß\-]{2,})")
FLURSTUECK_PATTERN = re.compile(r"Flurst(?:ü|ue)cks?(?:nummer)?:?\s*([\d/]+)", re.IGNORECASE)
HUETTE_KEYWORDS = ("hütte", "gartenhaus", "gartenhäuschen", "geräteschuppen", "schuppen", "bungalow", "laube")
SCHUTZGEBIET_KEYWORDS = (
    "naturschutzgebiet", "landschaftsschutzgebiet", "wasserschutzgebiet",
    "schutzgebiet", "biotop", "fauna-flora-habitat", "ffh-gebiet",
)


def extract_description_snippet(card):
    """Findet den Beschreibungs-Ausschnitt der Karte (nicht Preis, nicht die
    separate Größenangabe) - das ist der laengste passende Absatz."""
    best = ""
    for p in card.find_all("p"):
        text = p.get_text(" ", strip=True)
        if "€" in text:
            continue
        if SIZE_PATTERN.fullmatch(text):
            continue
        if len(text) > len(best):
            best = text
    return best


def extract_size_field(card):
    """Kleinanzeigen.de zeigt bei Grundstücken die Größe oft als eigenes
    Feld an (z.B. '498 m²') - falls vorhanden, zuverlässiger als Text-Suche."""
    for p in card.find_all("p"):
        text = p.get_text(strip=True)
        if "€" not in text and SIZE_PATTERN.fullmatch(text):
            return text
    return ""


def guess_facts(title, description, size_field):
    """Best-effort-Vorschläge aus Titel/Beschreibung - immer nur Vorschlag,
    nie verlässliche Angabe (siehe README)."""
    combined = f"{title}\n{description}".lower()
    facts = {"ort": "", "flurstueck": "", "groesse": "", "huette_hint": "", "schutzgebiet_hint": ""}

    m = GEMARKUNG_PATTERN.search(description)
    if m:
        facts["ort"] = m.group(1)

    m = FLURSTUECK_PATTERN.search(description)
    if m:
        facts["flurstueck"] = m.group(1)

    if size_field:
        facts["groesse"] = size_field
    else:
        m = SIZE_PATTERN.search(description)
        if m:
            facts["groesse"] = m.group(0)

    if any(k in combined for k in HUETTE_KEYWORDS):
        facts["huette_hint"] = "ja"
    if any(k in combined for k in SCHUTZGEBIET_KEYWORDS):
        facts["schutzgebiet_hint"] = "ja"

    return facts


def fetch_search(url, debug_path=None, max_retries=2):
    """Holt eine Suchseite und gibt eine Liste von Listing-Dicts zurueck.
    Bei 403/429 (Blockade/Rate-Limit) wird mit steigender Wartezeit erneut
    versucht, bevor endgueltig aufgegeben wird."""
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, headers=REQUEST_HEADERS, timeout=20)
            resp.encoding = "utf-8"  # Kleinanzeigen.de liefert UTF-8; explizit setzen
            # gegen Mojibake, falls requests die Codierung falsch errät.
            print(f"   HTTP {resp.status_code}, {len(resp.text)} Zeichen erhalten")

            if resp.status_code in (403, 429) and attempt < max_retries:
                wait = 8 * (attempt + 1) + random.uniform(0, 4)
                print(f"   Blockiert (HTTP {resp.status_code}), warte {wait:.0f}s und versuche erneut ...")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            break
        except requests.RequestException as e:
            last_error = e
            if attempt < max_retries:
                wait = 8 * (attempt + 1) + random.uniform(0, 4)
                print(f"   Fehler ({e}), warte {wait:.0f}s und versuche erneut ...")
                time.sleep(wait)
            else:
                raise
    soup = BeautifulSoup(resp.text, "html.parser")

    genuinely_empty = soup.select_one(EMPTY_RESULT_SELECTOR) is not None

    listings = []
    skipped_alt = 0
    for card in soup.select(LISTING_CARD_SELECTOR):
        if is_inside_alt_ads(card):
            skipped_alt += 1
            continue

        adid = card.get("data-adid")
        if not adid:
            continue

        title_el = card.select_one("h3 a") or card.select_one("a")
        title = title_el.get_text(strip=True) if title_el else "(kein Titel)"

        href = card.get("data-href") or (title_el.get("href") if title_el else None)
        link = f"https://www.kleinanzeigen.de{href}" if href and href.startswith("/") else href

        image = extract_image(card)
        price_text = extract_price(card)
        description = extract_description_snippet(card)
        size_field = extract_size_field(card)
        facts = guess_facts(title, description, size_field)

        listings.append(
            {
                "id": adid,
                "title": title,
                "url": link or url,
                "image": image,
                "price_text": price_text,
                "price_num": parse_price_to_number(price_text),
                "auto_ort": facts["ort"],
                "auto_flurstueck": facts["flurstueck"],
                "auto_groesse": facts["groesse"],
                "auto_huette_hint": facts["huette_hint"],
                "auto_schutzgebiet_hint": facts["schutzgebiet_hint"],
            }
        )

    if skipped_alt:
        print(f"   ({skipped_alt} 'ähnliche Anzeigen andernorts' ignoriert)")

    if not listings and not genuinely_empty and debug_path:
        # Unerwarteter Fall: 0 Treffer, aber auch kein "keine Ergebnisse"-Hinweis
        # gefunden -> vermutlich hat sich die Struktur wieder geändert oder es
        # gab eine Blockade. HTML zur Analyse speichern.
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(resp.text)
        print(f"   Debug: rohes HTML gespeichert unter {debug_path}")

    return listings, genuinely_empty


def send_ntfy(title, message, click_url=None, priority="default"):
    if not NTFY_URL:
        print("Kein NTFY_TOPIC gesetzt - ueberspringe Benachrichtigung.")
        return
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": priority,
    }
    if click_url:
        headers["Click"] = click_url
    try:
        requests.post(NTFY_URL, data=message.encode("utf-8"), headers=headers, timeout=10)
    except requests.RequestException as e:
        print(f"ntfy-Benachrichtigung fehlgeschlagen: {e}")


def build_dashboard(searches, state, new_items, price_drops, run_time, errors):
    def escape(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def by_price(item):
        # Inserate ohne Preisangabe (VB, "Zu verschenken" ohne Zahl, etc.)
        # landen ans Ende statt ganz nach vorne (0). "1 €" ist bei
        # Kleinanzeigen.de meist nur ein Platzhalter für "Verhandlungsbasis",
        # kein echter Preis - wird deshalb genauso behandelt.
        num = item.get("price_num")
        if num is not None and num <= 1:
            num = None
        return (num is None, num if num is not None else 0)

    def thumb(item):
        img = item.get("image")
        if img:
            return f'<img class="thumb" src="{escape(img)}" alt="" loading="lazy">'
        return '<div class="thumb thumb-placeholder">🏡</div>'

    def annotation_panel(item):
        aid = escape(item["id"])
        huette_hint = ' <span class="hint" title="Im Anzeigentext erwähnt">💡</span>' if item.get("auto_huette_hint") else ""
        schutz_hint = ' <span class="hint" title="Im Anzeigentext erwähnt">💡</span>' if item.get("auto_schutzgebiet_hint") else ""
        auto_ort = escape(item.get("auto_ort", ""))
        auto_flurstueck = escape(item.get("auto_flurstueck", ""))
        auto_groesse = escape(item.get("auto_groesse", ""))
        return f"""
            <div class="ann-bar">
              <select class="ann-field" data-ann="status" data-id="{aid}">
                <option value="">Bewertung ...</option>
                <option value="angeschaut">👀 Angeschaut</option>
                <option value="interessant">✅ Interessant</option>
                <option value="nicht_interessant">❌ Nicht interessant</option>
              </select>
              <span>
                <select class="ann-field" data-ann="schutzgebiet" data-id="{aid}">
                  <option value="">Schutzgebiet?</option>
                  <option value="ja">Schutzgebiet: Ja</option>
                  <option value="nein">Schutzgebiet: Nein</option>
                </select>{schutz_hint}
              </span>
              <span>
                <select class="ann-field" data-ann="huette" data-id="{aid}">
                  <option value="">Hütte?</option>
                  <option value="ja">Hütte: Ja</option>
                  <option value="nein">Hütte: Nein</option>
                </select>{huette_hint}
              </span>
              <button type="button" class="ann-toggle" data-id="{aid}">📝 Details</button>
            </div>
            <div class="ann-details" data-id="{aid}" hidden>
              <input type="text" class="ann-field" data-ann="flurstueck" data-id="{aid}" data-auto="{auto_flurstueck}" value="{auto_flurstueck}" placeholder="Flurstücknummer">
              <input type="text" class="ann-field" data-ann="ort" data-id="{aid}" data-auto="{auto_ort}" value="{auto_ort}" placeholder="Ort">
              <input type="text" class="ann-field" data-ann="groesse" data-id="{aid}" data-auto="{auto_groesse}" value="{auto_groesse}" placeholder="Größe (z.B. 500 m²)">
              <textarea class="ann-field" data-ann="comment" data-id="{aid}" placeholder="Kommentar ..." rows="2"></textarea>
            </div>"""

    def item_row(item, is_new=False, has_drop=False):
        num = item.get("price_num")
        data_price = "" if num is None else str(num)
        return f"""
        <li class="item" data-id="{escape(item['id'])}" data-price="{data_price}" data-search="{escape(item.get('search_name',''))}" data-title="{escape(item['title'].lower())}" data-is-new="{'1' if is_new else '0'}" data-price-drop="{'1' if has_drop else '0'}">
          {thumb(item)}
          <div class="item-body">
            <a href="{escape(item['url'])}" target="_blank" rel="noopener">{escape(item['title'])}</a>
            <div class="item-meta">
              <span class="price">{escape(item.get('price_text') or '–')}</span>
              <span class="search-tag">{escape(item.get('search_name',''))}</span>
            </div>
            {annotation_panel(item)}
          </div>
        </li>"""

    new_items_sorted = sorted(new_items, key=by_price)
    price_drops_sorted = sorted(price_drops, key=by_price)

    new_html = "".join(item_row(i) for i in new_items_sorted) or "<li class='empty'>Keine neuen Inserate.</li>"
    drops_html = "".join(
        f"""
        <li class="item" data-id="{escape(d['id'])}" data-price="{'' if d.get('price_num') is None else d['price_num']}" data-search="{escape(d.get('search_name',''))}" data-title="{escape(d['title'].lower())}">
          {thumb(d)}
          <div class="item-body">
            <a href="{escape(d['url'])}" target="_blank" rel="noopener">{escape(d['title'])}</a>
            <div class="item-meta">
              <span class="price">{escape(d['old_price'])} → <b>{escape(d['new_price'])}</b></span>
              <span class="search-tag">{escape(d.get('search_name',''))}</span>
            </div>
            {annotation_panel(d)}
          </div>
        </li>"""
        for d in price_drops_sorted
    ) or "<li class='empty'>Keine Preissenkungen.</li>"


    new_ids = {i["id"] for i in new_items_sorted}
    drop_ids = {d["id"] for d in price_drops_sorted}
    all_current = sorted(state.values(), key=by_price)
    all_html = "".join(
        item_row(i, is_new=i["id"] in new_ids, has_drop=i["id"] in drop_ids) for i in all_current
    ) or "<li class='empty'>Noch keine Daten.</li>"

    errors_html = ""
    if errors:
        error_items = "".join(f"<li>{escape(e)}</li>" for e in errors)
        errors_html = f"""
        <section class="errors">
          <h2>⚠️ Fehler beim letzten Lauf</h2>
          <ul>{error_items}</ul>
        </section>"""

    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kleinanzeigen Monitor – Wernau Grundstücke</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 800px; margin: 2rem auto; padding: 0 1rem; background: #fafafa; color: #222; }}
  h1 {{ font-size: 1.4rem; }}
  h2 {{ font-size: 1.1rem; margin-top: 2rem; border-bottom: 2px solid #eee; padding-bottom: .3rem; }}
  .meta {{ color: #666; font-size: .85rem; margin-bottom: 1.5rem; }}
  ul {{ list-style: none; padding: 0; }}
  li.item {{ background: white; border: 1px solid #e0e0e0; border-radius: 8px; padding: .6rem; margin-bottom: .5rem; display: flex; align-items: center; gap: .8rem; }}
  li.empty {{ color: #999; font-style: italic; padding: .5rem 0; }}
  .thumb {{ width: 72px; height: 72px; object-fit: cover; border-radius: 6px; flex-shrink: 0; background: #eee; }}
  .thumb-placeholder {{ display: flex; align-items: center; justify-content: center; font-size: 1.6rem; }}
  .item-body {{ flex: 1; min-width: 0; display: flex; flex-direction: column; gap: .25rem; }}
  .item-body a {{ overflow-wrap: anywhere; }}
  .item-meta {{ display: flex; align-items: center; gap: .5rem; flex-wrap: wrap; }}
  .price {{ font-weight: 600; white-space: nowrap; }}
  .search-tag {{ font-size: .75rem; color: #888; background: #f0f0f0; border-radius: 4px; padding: .1rem .4rem; }}
  .errors {{ background: #fff3f3; border: 1px solid #ffc9c9; border-radius: 8px; padding: 1rem; }}
  a {{ color: #0a5; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .filters {{ background: white; border: 1px solid #e0e0e0; border-radius: 8px; padding: .8rem; display: flex; flex-wrap: wrap; gap: .6rem; align-items: center; margin-bottom: 1rem; position: sticky; top: .5rem; z-index: 10; }}
  .filters input[type="text"], .filters input[type="number"], .filters select {{ padding: .4rem .5rem; border: 1px solid #ccc; border-radius: 6px; font-size: .9rem; }}
  .filters input[type="text"] {{ flex: 1; min-width: 140px; }}
  .filters input[type="number"] {{ width: 90px; }}
  .filters label {{ font-size: .85rem; display: flex; align-items: center; gap: .3rem; white-space: nowrap; }}
  .filters button {{ padding: .4rem .7rem; border: 1px solid #ccc; border-radius: 6px; background: #f5f5f5; cursor: pointer; font-size: .85rem; }}
  .filters button:hover {{ background: #eee; }}
  .filter-count {{ font-size: .8rem; color: #666; margin: -0.5rem 0 1rem; }}
  li.item {{ align-items: flex-start; }}
  .ann-bar {{ display: flex; flex-wrap: wrap; gap: .4rem; margin-top: .3rem; }}
  .ann-bar select, .ann-bar button {{ font-size: .78rem; padding: .2rem .4rem; border: 1px solid #ccc; border-radius: 5px; background: #fafafa; }}
  .ann-details {{ display: flex; flex-wrap: wrap; gap: .4rem; margin-top: .4rem; }}
  .ann-details input, .ann-details textarea {{ font-size: .8rem; padding: .3rem .4rem; border: 1px solid #ccc; border-radius: 5px; font-family: inherit; }}
  .ann-details input {{ width: 140px; }}
  .ann-details textarea {{ width: 100%; resize: vertical; }}
  li.status-interessant {{ border-left: 4px solid #2a7f3f; }}
  li.status-nicht_interessant {{ border-left: 4px solid #c0392b; opacity: .7; }}
  li.status-angeschaut {{ border-left: 4px solid #999; }}
  .sync-bar {{ background: white; border: 1px solid #e0e0e0; border-radius: 8px; padding: .6rem .8rem; margin-bottom: 1rem; font-size: .82rem; display: flex; align-items: center; gap: .6rem; flex-wrap: wrap; }}
  .sync-bar input[type="password"] {{ flex: 1; min-width: 160px; padding: .3rem .5rem; border: 1px solid #ccc; border-radius: 5px; }}
  .sync-status {{ color: #666; }}
  .sync-status.ok {{ color: #2a7f3f; }}
  .sync-status.err {{ color: #c0392b; }}
  .hint {{ cursor: help; font-size: .8rem; }}
</style>
</head>
<body>
  <h1>🌱 Kleinanzeigen Monitor – Grundstücke Wernau</h1>
  <div class="meta">Letzter Check: <span id="lastCheckTime" data-utc="{run_time}">{run_time}</span> · {len(searches)} gespeicherte Suchen · {len(state)} bekannte Inserate insgesamt</div>

  {errors_html}

  <div class="sync-bar">
    <span>🔑 Notizen-Sync (GitHub):</span>
    <input type="password" id="ghPatInput" placeholder="Personal Access Token einfügen ...">
    <button type="button" id="ghPatSave">Speichern</button>
    <button type="button" id="ghPatClear">Entfernen</button>
    <span class="sync-status" id="syncStatus">nicht eingerichtet</span>
  </div>

  <div class="filters">
    <input type="text" id="filterText" placeholder="Titel/Ort/Flurstück/Kommentar enthält ...">
    <select id="filterSearch"><option value="">Alle Suchen</option></select>
    <input type="number" id="filterMinPrice" placeholder="Preis von €">
    <input type="number" id="filterMaxPrice" placeholder="Preis bis €">
    <label><input type="checkbox" id="filterHideVB"> VB/ohne Preis ausblenden</label>
    <select id="filterStatus">
      <option value="">Bewertung: alle</option>
      <option value="none">– keine Bewertung –</option>
      <option value="angeschaut">👀 Angeschaut</option>
      <option value="interessant">✅ Interessant</option>
      <option value="nicht_interessant">❌ Nicht interessant</option>
    </select>
    <select id="filterSchutzgebiet">
      <option value="">Schutzgebiet: egal</option>
      <option value="ja">Schutzgebiet: Ja</option>
      <option value="nein">Schutzgebiet: Nein</option>
    </select>
    <select id="filterHuette">
      <option value="">Hütte: egal</option>
      <option value="ja">Hütte: Ja</option>
      <option value="nein">Hütte: Nein</option>
    </select>
    <button id="filterReset" type="button">Zurücksetzen</button>
  </div>
  <div class="filter-count" id="filterCount"></div>

  <section>
    <h2>🆕 Neu seit letztem Check</h2>
    <ul id="neuList">{new_html}</ul>
  </section>

  <section>
    <h2>💸 Preissenkungen</h2>
    <ul id="preissenkungenList">{drops_html}</ul>
  </section>

  <section>
    <h2>🔍 Noch nicht geprüft/bewertet</h2>
    <ul id="unbewertetList"><li class="empty">Lädt ...</li></ul>
  </section>

  <section>
    <h2>📋 Alle aktuell bekannten Inserate</h2>
    <ul id="alleList">{all_html}</ul>
  </section>

  <section>
    <h2>🚫 Nicht Interessant – Aussortiert</h2>
    <ul id="aussortiertList"><li class="empty">Keine aussortiert.</li></ul>
  </section>

<script>
(function() {{
  const searchSelect = document.getElementById('filterSearch');
  const initialItems = Array.from(document.querySelectorAll('li.item'));
  const names = Array.from(new Set(initialItems.map(li => li.dataset.search).filter(Boolean))).sort();
  names.forEach(name => {{
    const opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name;
    searchSelect.appendChild(opt);
  }});

  const textInput = document.getElementById('filterText');
  const minInput = document.getElementById('filterMinPrice');
  const maxInput = document.getElementById('filterMaxPrice');
  const hideVBBox = document.getElementById('filterHideVB');
  const statusSelect = document.getElementById('filterStatus');
  const schutzSelect = document.getElementById('filterSchutzgebiet');
  const huetteSelect = document.getElementById('filterHuette');
  const countEl = document.getElementById('filterCount');
  const resetBtn = document.getElementById('filterReset');

  function fieldValue(li, field) {{
    const el = li.querySelector('[data-ann="' + field + '"]');
    return el ? el.value : '';
  }}

  function applyFilters() {{
    // Frisch abfragen statt gecachter Liste, da Notizen-Sync Kopien in
    // "Noch nicht geprüft" / "Aussortiert" nachträglich einfügt.
    const items = Array.from(document.querySelectorAll('li.item'));
    const text = textInput.value.trim().toLowerCase();
    const search = searchSelect.value;
    const min = parseFloat(minInput.value);
    const max = parseFloat(maxInput.value);
    const hideVB = hideVBBox.checked;
    const statusFilter = statusSelect.value;
    const schutzFilter = schutzSelect.value;
    const huetteFilter = huetteSelect.value;
    let visibleCount = 0;

    items.forEach(li => {{
      if (li.dataset.hidden === '1') {{ li.style.display = 'none'; return; }}

      const priceRaw = li.dataset.price;
      const price = priceRaw === '' ? null : parseFloat(priceRaw);
      const title = li.dataset.title || '';
      const searchName = li.dataset.search || '';
      const status = fieldValue(li, 'status');
      const schutz = fieldValue(li, 'schutzgebiet');
      const huette = fieldValue(li, 'huette');
      const extraText = [
        fieldValue(li, 'ort'), fieldValue(li, 'flurstueck'), fieldValue(li, 'comment')
      ].join(' ').toLowerCase();
      let visible = true;

      if (text && !title.includes(text) && !extraText.includes(text)) visible = false;
      if (search && searchName !== search) visible = false;
      if (!isNaN(min) && (price === null || price < min)) visible = false;
      if (!isNaN(max) && (price === null || price > max)) visible = false;
      if (hideVB && (price === null || price <= 1)) visible = false;
      if (statusFilter === 'none' && status !== '') visible = false;
      else if (statusFilter && statusFilter !== 'none' && status !== statusFilter) visible = false;
      if (schutzFilter && schutz !== schutzFilter) visible = false;
      if (huetteFilter && huette !== huetteFilter) visible = false;

      li.style.display = visible ? '' : 'none';
      if (visible) visibleCount++;
    }});

    countEl.textContent = visibleCount + ' von ' + items.length + ' Inseraten sichtbar';
  }}

  [textInput, searchSelect, minInput, maxInput, hideVBBox, statusSelect, schutzSelect, huetteSelect].forEach(el => {{
    el.addEventListener('input', applyFilters);
    el.addEventListener('change', applyFilters);
  }});

  resetBtn.addEventListener('click', () => {{
    textInput.value = '';
    searchSelect.value = '';
    minInput.value = '';
    maxInput.value = '';
    hideVBBox.checked = false;
    statusSelect.value = '';
    schutzSelect.value = '';
    huetteSelect.value = '';
    applyFilters();
  }});

  // Notizen-Sync-Skript ruft window.__applyFilters() nach jeder
  // Statusänderung / jedem Reorganisieren erneut auf.
  window.__applyFilters = applyFilters;

  // Lokale Zeitzone fürs "Letzter Check"
  const timeEl = document.getElementById('lastCheckTime');
  if (timeEl && timeEl.dataset.utc) {{
    const d = new Date(timeEl.dataset.utc);
    if (!isNaN(d.getTime())) {{
      timeEl.textContent = d.toLocaleString('de-DE', {{ dateStyle: 'medium', timeStyle: 'short' }});
    }}
  }}

  applyFilters();
}})();

(function() {{
  const REPO = "{GITHUB_REPO_SLUG}";
  const BRANCH = "{GITHUB_BRANCH}";
  const FILE_PATH = "{ANNOTATIONS_PATH}";
  const API_BASE = "https://api.github.com/repos/" + REPO + "/contents/" + FILE_PATH;

  const patInput = document.getElementById('ghPatInput');
  const patSaveBtn = document.getElementById('ghPatSave');
  const patClearBtn = document.getElementById('ghPatClear');
  const statusEl = document.getElementById('syncStatus');

  let pat = localStorage.getItem('ghPat') || '';
  let annotations = {{}};
  let currentSha = null;
  let saveQueue = Promise.resolve();

  function setStatus(text, cls) {{
    statusEl.textContent = text;
    statusEl.className = 'sync-status' + (cls ? ' ' + cls : '');
  }}

  function b64EncodeUnicode(str) {{
    return btoa(encodeURIComponent(str).replace(/%([0-9A-F]{{2}})/g, function(match, p1) {{
      return String.fromCharCode(parseInt(p1, 16));
    }}));
  }}

  function b64DecodeUnicode(str) {{
    return decodeURIComponent(atob(str.replace(/\\n/g, '')).split('').map(function(c) {{
      return '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2);
    }}).join(''));
  }}

  async function loadAnnotations() {{
    if (!pat) {{
      setStatus('nicht eingerichtet');
      applyAnnotationsToDOM();
      return;
    }}
    setStatus('lade ...');
    try {{
      const resp = await fetch(API_BASE + '?ref=' + BRANCH, {{
        headers: {{ 'Authorization': 'token ' + pat, 'Accept': 'application/vnd.github+json' }}
      }});
      if (resp.status === 404) {{
        annotations = {{}};
        currentSha = null;
        setStatus('bereit (noch keine Notizen)', 'ok');
      }} else if (resp.ok) {{
        const data = await resp.json();
        currentSha = data.sha;
        annotations = JSON.parse(b64DecodeUnicode(data.content));
        setStatus('geladen (' + Object.keys(annotations).length + ' Notizen)', 'ok');
      }} else {{
        setStatus('Fehler beim Laden (HTTP ' + resp.status + ')', 'err');
        return;
      }}
      applyAnnotationsToDOM();
    }} catch (e) {{
      setStatus('Fehler beim Laden: ' + e.message, 'err');
    }}
  }}

  function applyAnnotationsToDOM() {{
    document.querySelectorAll('[data-ann]').forEach(function(el) {{
      const id = el.dataset.id;
      const field = el.dataset.ann;
      // Nur überschreiben, wenn für dieses Feld schon einmal explizit etwas
      // gespeichert wurde - sonst bleibt die automatische Vorbefüllung
      // (data-auto, direkt im HTML) unangetastet stehen.
      if (annotations[id] && Object.prototype.hasOwnProperty.call(annotations[id], field)) {{
        el.value = annotations[id][field];
      }}
    }});
    document.querySelectorAll('li.item[data-id]').forEach(function(li) {{
      const id = li.dataset.id;
      const status = annotations[id] && annotations[id].status;
      li.classList.remove('status-angeschaut', 'status-interessant', 'status-nicht_interessant');
      if (status) li.classList.add('status-' + status);
    }});
    reorganize();
  }}

  function setFieldValues(li, id) {{
    const data = annotations[id] || {{}};
    li.querySelectorAll('[data-ann]').forEach(function(el) {{
      const field = el.dataset.ann;
      if (Object.prototype.hasOwnProperty.call(data, field)) {{
        el.value = data[field];
      }} else if (el.dataset.auto) {{
        el.value = el.dataset.auto;
      }}
    }});
  }}

  function reorganize() {{
    const unbewertetList = document.getElementById('unbewertetList');
    const aussortiertList = document.getElementById('aussortiertList');
    if (!unbewertetList || !aussortiertList) return;
    unbewertetList.innerHTML = '';
    aussortiertList.innerHTML = '';
    document.querySelectorAll('li.item[data-hidden]').forEach(function(li) {{
      li.removeAttribute('data-hidden');
    }});

    const alleItems = Array.from(document.querySelectorAll('#alleList li.item[data-id]'));
    let unbewertetCount = 0;
    let aussortiertCount = 0;

    alleItems.forEach(function(li) {{
      const id = li.dataset.id;
      const status = (annotations[id] && annotations[id].status) || '';
      const isNew = li.dataset.isNew === '1';
      const hasDrop = li.dataset.priceDrop === '1';

      if (status === 'nicht_interessant') {{
        const clone = li.cloneNode(true);
        setFieldValues(clone, id);
        aussortiertList.appendChild(clone);
        aussortiertCount++;
        li.dataset.hidden = '1';
      }} else if (status === '' && !isNew && !hasDrop) {{
        const clone = li.cloneNode(true);
        setFieldValues(clone, id);
        unbewertetList.appendChild(clone);
        unbewertetCount++;
      }}
    }});

    ['#neuList', '#preissenkungenList'].forEach(function(sel) {{
      document.querySelectorAll(sel + ' li.item[data-id]').forEach(function(li) {{
        const id = li.dataset.id;
        const status = (annotations[id] && annotations[id].status) || '';
        if (status === 'nicht_interessant') {{
          li.dataset.hidden = '1';
        }}
      }});
    }});

    if (unbewertetCount === 0) unbewertetList.innerHTML = '<li class="empty">Alles bewertet 🎉</li>';
    if (aussortiertCount === 0) aussortiertList.innerHTML = '<li class="empty">Keine aussortiert.</li>';

    if (window.__applyFilters) window.__applyFilters();
  }}

  function queueSave() {{
    saveQueue = saveQueue.then(saveAnnotations);
  }}

  async function saveAnnotations() {{
    if (!pat) return;
    setStatus('speichere ...');
    try {{
      const body = {{
        message: 'Notizen aktualisiert (' + new Date().toISOString() + ')',
        content: b64EncodeUnicode(JSON.stringify(annotations, null, 2)),
        branch: BRANCH
      }};
      if (currentSha) body.sha = currentSha;
      const resp = await fetch(API_BASE, {{
        method: 'PUT',
        headers: {{
          'Authorization': 'token ' + pat,
          'Accept': 'application/vnd.github+json',
          'Content-Type': 'application/json'
        }},
        body: JSON.stringify(body)
      }});
      if (resp.ok) {{
        const data = await resp.json();
        currentSha = data.content.sha;
        setStatus('gespeichert ✓', 'ok');
      }} else {{
        const errText = await resp.text();
        setStatus('Fehler beim Speichern (HTTP ' + resp.status + ')', 'err');
        console.error(errText);
      }}
    }} catch (e) {{
      setStatus('Fehler beim Speichern: ' + e.message, 'err');
    }}
  }}

  document.addEventListener('change', function(ev) {{
    const el = ev.target;
    if (!el.matches('[data-ann]')) return;
    const id = el.dataset.id;
    const field = el.dataset.ann;
    if (!annotations[id]) annotations[id] = {{}};
    annotations[id][field] = el.value;

    document.querySelectorAll('[data-ann="' + field + '"][data-id="' + CSS.escape(id) + '"]').forEach(function(other) {{
      other.value = el.value;
    }});
    if (field === 'status') {{
      document.querySelectorAll('li.item[data-id="' + CSS.escape(id) + '"]').forEach(function(li) {{
        li.classList.remove('status-angeschaut', 'status-interessant', 'status-nicht_interessant');
        if (el.value) li.classList.add('status-' + el.value);
      }});
      reorganize();
    }} else if (window.__applyFilters) {{
      window.__applyFilters();
    }}
    queueSave();
  }});

  document.addEventListener('click', function(ev) {{
    const btn = ev.target.closest('.ann-toggle');
    if (!btn) return;
    const id = btn.dataset.id;
    document.querySelectorAll('.ann-details[data-id="' + CSS.escape(id) + '"]').forEach(function(panel) {{
      panel.hidden = !panel.hidden;
    }});
  }});

  patInput.value = pat ? '••••••••' : '';
  patSaveBtn.addEventListener('click', function() {{
    const val = patInput.value.trim();
    if (!val || val === '••••••••') return;
    pat = val;
    localStorage.setItem('ghPat', pat);
    patInput.value = '••••••••';
    loadAnnotations();
  }});
  patClearBtn.addEventListener('click', function() {{
    pat = '';
    localStorage.removeItem('ghPat');
    patInput.value = '';
    setStatus('nicht eingerichtet');
  }});

  loadAnnotations();
}})();
</script>
</body>
</html>"""
    DASHBOARD_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    searches = load_json(SEARCHES_FILE, [])
    state = load_json(STATE_FILE, {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    print(f"searches.json: {SEARCHES_FILE} (existiert: {SEARCHES_FILE.exists()})")
    print(f"{len(searches)} Suchen geladen.")
    if not searches:
        print(
            "WARNUNG: Keine Suchen geladen! Liegt searches.json im selben Ordner "
            "wie dieses Skript?"
        )

    new_items = []
    price_drops = []
    errors = []
    debug_saved = False

    for search in searches:
        print(f"→ Prüfe '{search['name']}' ...")
        try:
            debug_path = (BASE_DIR / "debug_page.html") if not debug_saved else None
            listings, genuinely_empty = fetch_search(search["url"], debug_path=debug_path)
            if debug_path and debug_path.exists():
                debug_saved = True
        except Exception as e:
            print(f"   Fehler: {e}")
            errors.append(f"{search['name']}: {e}")
            continue

        print(f"   {len(listings)} Treffer geparst.")

        if not listings and not genuinely_empty:
            errors.append(
                f"{search['name']}: 0 Treffer geparst und keine 'keine Ergebnisse'-Meldung "
                f"gefunden – evtl. hat sich das Kleinanzeigen-Layout geaendert, oder die Suche "
                f"wurde blockiert (CAPTCHA/403)."
            )

        for item in listings:
            item["search_name"] = search["name"]
            existing = state.get(item["id"])

            if existing is None:
                new_items.append(item)
            elif (
                existing.get("price_num") is not None
                and item.get("price_num") is not None
                and item["price_num"] < existing["price_num"]
            ):
                price_drops.append(
                    {
                        **item,
                        "old_price": existing["price_text"],
                        "new_price": item["price_text"],
                    }
                )

            state[item["id"]] = {**item, "last_seen": now}

        time.sleep(random.uniform(4, 9))  # zufällige Pause zwischen Requests, aus Fairness

    save_json(STATE_FILE, state)
    build_dashboard(searches, state, new_items, price_drops, now, errors)

    if errors:
        send_ntfy(
            "⚠️ Kleinanzeigen Monitor: Fehler",
            "\n".join(errors[:5]),
            priority="high",
        )

    if new_items or price_drops:
        lines = []
        if new_items:
            lines.append(f"{len(new_items)} neue Inserate:")
            lines += [f"• {i['title']} ({i['price_text']})" for i in new_items[:5]]
        if price_drops:
            lines.append(f"{len(price_drops)} Preissenkung(en):")
            lines += [f"• {d['title']}: {d['old_price']} → {d['new_price']}" for d in price_drops[:5]]
        send_ntfy(
            "🌱 Kleinanzeigen: Neue Treffer!",
            "\n".join(lines),
            click_url=os.environ.get("DASHBOARD_URL"),
        )
    else:
        print("Keine neuen Inserate oder Preissenkungen in diesem Lauf.")

    print(f"Fertig. {len(new_items)} neu, {len(price_drops)} Preissenkungen, {len(errors)} Fehler.")


if __name__ == "__main__":
    sys.exit(main())
