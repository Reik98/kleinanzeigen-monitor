# Kleinanzeigen Monitor (Wernau – Grundstücke/Gärten)

Automatischer Check deiner gespeicherten Kleinanzeigen.de-Suchen: erkennt neue
Inserate und Preissenkungen, zeigt sie in einem Dashboard und schickt eine
Push-Benachrichtigung.

## ⚠️ Wichtig vorab

- Kleinanzeigen.de verbietet automatisierten Zugriff in seiner `robots.txt`.
  Dieses Skript ist nur für den privaten Eigengebrauch gedacht – als Ersatz
  fürs manuelle tägliche Nachschauen, nicht für Massenabfragen.
- Ich konnte die Seitenstruktur wegen genau dieses robots.txt-Verbots nicht
  live testen, aber wir haben inzwischen anhand eines echten HTML-Exports
  von deinem PC die aktuelle Struktur analysiert und die Selektoren im
  Skript entsprechend angepasst (Stand: September 2026). Ändert
  Kleinanzeigen.de sein Layout erneut, siehst du das an "0 Treffer geparst"
  ohne "keine Ergebnisse"-Hinweis in der Konsole bzw. an einer neu
  erzeugten `debug_page.html` im Projektordner.
- Cloud-IPs (wie die von GitHub Actions) werden von Anti-Bot-Systemen
  manchmal schneller blockiert als eine private Internet-Verbindung. Wenn
  das dauerhaft nicht funktioniert, ist "auf dem eigenen Rechner per
  Aufgabenplanung/Cron laufen lassen" die zuverlässigere Alternative –
  das Skript selbst müsste dafür nicht verändert werden.

## 1. ntfy.sh einrichten (2 Minuten, kein Account nötig)

1. App installieren: [ntfy für iOS](https://apps.apple.com/app/ntfy/id1625396347)
   oder [ntfy für Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy),
   alternativ reicht auch der Browser unter `https://ntfy.sh/wernau-grundstueck-8f3k2q1z`.
2. In der App "Thema abonnieren" → Namen eingeben:
   `wernau-grundstueck-8f3k2q1z` (oder deinen eigenen, einzigartigen Namen –
   dann aber in Schritt 3 entsprechend anpassen).

## 2. Lokal testen (empfohlen, bevor es automatisch in der Cloud läuft)

```bash
git clone <dein-neues-repo>
cd kleinanzeigen-monitor
pip install -r requirements.txt
NTFY_TOPIC=wernau-grundstueck-8f3k2q1z python check_kleinanzeigen.py
```

Danach `docs/index.html` im Browser öffnen und prüfen, ob Inserate mit
Titel/Preis auftauchen. Steht dort "0 Treffer geparst" bzw. kommt eine
Fehlermeldung, hat sich vermutlich die HTML-Struktur von Kleinanzeigen.de
geändert oder der Zugriff wurde geblockt – dann bräuchten die Selektoren in
`check_kleinanzeigen.py` (ganz oben, `SELECTORS = {...}`) eine Anpassung
anhand der aktuellen Seitenquelle (Rechtsklick → "Seitenquelltext anzeigen"
auf einer Kleinanzeigen-Suchseite).

## 3. In GitHub einrichten

1. Neues **privates** GitHub-Repo anlegen und diese Dateien hochladen/pushen.
2. Repo → Settings → Secrets and variables → Actions:
   - Unter **Secrets**: `NTFY_TOPIC` = `wernau-grundstueck-8f3k2q1z`
   - Unter **Variables** (optional): `DASHBOARD_URL` = deine spätere
     GitHub-Pages-URL (siehe Schritt 4) – macht die Push-Benachrichtigung
     direkt anklickbar.
3. Repo → Settings → Actions → General → "Workflow permissions" auf
   **"Read and write permissions"** stellen (nötig, damit der Bot Ergebnisse
   zurück ins Repo committen darf).
4. Repo → Settings → Pages → Source: **Deploy from branch**, Branch: `main`,
   Ordner: `/docs`. Nach ein paar Minuten ist das Dashboard erreichbar unter
   `https://<dein-username>.github.io/<repo-name>/`.

## 4. Suchen anpassen

Alle 14 von dir genannten gespeicherten Suchen liegen in `searches.json`.
Weitere hinzufügen, entfernen oder umbenennen: Datei einfach als JSON-Liste
mit `name` und `url` bearbeiten.

## 5. Lauf-Frequenz ändern

In `.github/workflows/check.yml`, Zeile mit `cron:`. Aktuell stündlich
(`0 * * * *`). Für alle 30 Minuten: `*/30 * * * *`. Kürzere Intervalle
erhöhen das Blockrisiko.

## 6. Notizen/Bewertungen einrichten (optional, Geräte-übergreifend)

Auf dem Dashboard kannst du pro Inserat eine Bewertung (Angeschaut/
Interessant/Nicht interessant), Schutzgebiet Ja/Nein, Hütte Ja/Nein,
Flurstücknummer, Ort, Größe und einen Kommentar hinterlegen. Damit das auf
allen deinen Geräten (Handy + PC) sichtbar ist, werden diese Notizen als
`data/annotations.json` direkt in dieses Repo geschrieben - dafür braucht
jedes Gerät einmalig einen **GitHub Personal Access Token**:

1. Auf github.com → oben rechts dein Profilbild → **Settings** →
   ganz unten **Developer settings** → **Personal access tokens** →
   **Fine-grained tokens** → **Generate new token**.
2. Name z.B. "kleinanzeigen-monitor-notizen", Ablaufdatum nach Wahl.
3. **Repository access** → "Only select repositories" → dieses Repo
   (`kleinanzeigen-monitor`) auswählen.
4. **Permissions** → "Repository permissions" → **Contents** → auf
   **"Read and write"** stellen. Alle anderen Berechtigungen auf "No access"
   lassen.
5. **Generate token** → den angezeigten Token (beginnt mit `github_pat_...`)
   kopieren - er wird nur einmal angezeigt!
6. Auf dem Dashboard oben in der Zeile "🔑 Notizen-Sync" den Token einfügen
   und "Speichern" klicken.

Das musst du auf jedem Gerät (Handy, PC, ...) einmal separat machen, mit
demselben oder einem eigenen Token pro Gerät. Der Token wird ausschließlich
lokal im Browser gespeichert (`localStorage`) und nirgendwo sonst
hinterlegt - aber da das Repo öffentlich ist, sollte der Token wirklich nur
auf "Contents: Read and write" für genau dieses eine Repo beschränkt sein,
falls er doch einmal in falsche Hände geraten sollte (z.B. bei Verlust des
Geräts).

## Wie es funktioniert

- `check_kleinanzeigen.py` ruft jede URL aus `searches.json` ab, parst die
  Trefferliste und vergleicht sie mit `data/state.json` (Gedächtnis vom
  letzten Lauf).
- Neue Anzeigen-IDs → "neu". Bekannte IDs mit gesunkenem Preis →
  "Preissenkung".
- Ergebnis wird als `docs/index.html` (Dashboard) geschrieben und bei
  Treffern per ntfy gepusht.
- GitHub Actions führt das stündlich aus und committed die aktualisierten
  Dateien automatisch zurück ins Repo.
