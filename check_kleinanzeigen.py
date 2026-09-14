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


def fetch_search(url, debug_path=None):
    """Holt eine Suchseite und gibt eine Liste von Listing-Dicts zurueck."""
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=20)
    resp.encoding = "utf-8"  # Kleinanzeigen.de liefert UTF-8; explizit setzen
    # gegen Mojibake, falls requests die Codierung falsch errät.
    print(f"   HTTP {resp.status_code}, {len(resp.text)} Zeichen erhalten")
    resp.raise_for_status()
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

        listings.append(
            {
                "id": adid,
                "title": title,
                "url": link or url,
                "image": image,
                "price_text": price_text,
                "price_num": parse_price_to_number(price_text),
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
        # landen ans Ende statt ganz nach vorne (0).
        num = item.get("price_num")
        return (num is None, num if num is not None else 0)

    def thumb(item):
        img = item.get("image")
        if img:
            return f'<img class="thumb" src="{escape(img)}" alt="" loading="lazy">'
        return '<div class="thumb thumb-placeholder">🏡</div>'

    def item_row(item):
        return f"""
        <li class="item">
          {thumb(item)}
          <div class="item-body">
            <a href="{escape(item['url'])}" target="_blank" rel="noopener">{escape(item['title'])}</a>
            <div class="item-meta">
              <span class="price">{escape(item.get('price_text') or '–')}</span>
              <span class="search-tag">{escape(item.get('search_name',''))}</span>
            </div>
          </div>
        </li>"""

    new_items_sorted = sorted(new_items, key=by_price)
    price_drops_sorted = sorted(price_drops, key=by_price)

    new_html = "".join(item_row(i) for i in new_items_sorted) or "<li class='empty'>Keine neuen Inserate.</li>"
    drops_html = "".join(
        f"""
        <li class="item">
          {thumb(d)}
          <div class="item-body">
            <a href="{escape(d['url'])}" target="_blank" rel="noopener">{escape(d['title'])}</a>
            <div class="item-meta">
              <span class="price">{escape(d['old_price'])} → <b>{escape(d['new_price'])}</b></span>
              <span class="search-tag">{escape(d.get('search_name',''))}</span>
            </div>
          </div>
        </li>"""
        for d in price_drops_sorted
    ) or "<li class='empty'>Keine Preissenkungen.</li>"

    all_current = sorted(state.values(), key=by_price)
    all_html = "".join(item_row(i) for i in all_current) or "<li class='empty'>Noch keine Daten.</li>"

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
</style>
</head>
<body>
  <h1>🌱 Kleinanzeigen Monitor – Grundstücke Wernau</h1>
  <div class="meta">Letzter Check: {run_time} · {len(searches)} gespeicherte Suchen · {len(state)} bekannte Inserate insgesamt</div>

  {errors_html}

  <section>
    <h2>🆕 Neu seit letztem Check</h2>
    <ul>{new_html}</ul>
  </section>

  <section>
    <h2>💸 Preissenkungen</h2>
    <ul>{drops_html}</ul>
  </section>

  <section>
    <h2>📋 Alle aktuell bekannten Inserate</h2>
    <ul>{all_html}</ul>
  </section>
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

        time.sleep(2)  # kleine Pause zwischen Requests, aus Fairness

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
