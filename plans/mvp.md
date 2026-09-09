# Prompt: Xtream-Codes Filter-Proxy (MVP)

Baue eine selbst gehostete Anwendung, die als Proxy zwischen einem IPTV-Client
(IPTV Smarters Pro) und einem Xtream-Codes-Anbieter sitzt und den Katalog nach
frei konfigurierbaren Regeln filtert.

Der Nutzer trägt in Smarters statt der Provider-URL die URL dieses Proxys ein,
mit unveränderten Provider-Zugangsdaten. Smarters darf nicht merken, dass es
nicht direkt mit dem Provider spricht.

---

## 1. Kernanforderungen

1. **Xtream-API emulieren** – alle Endpunkte, die Smarters nutzt.
2. **Crawler** – holt Audiospur-Metadaten pro Film/Episode und speichert sie lokal.
3. **Lokale Datenbank** – SQLite, persistent über Neustarts.
4. **Crawl-Zeitfenster** – konfigurierbar, z. B. nur nachts.
5. **Pause/Resume** – Crawler jederzeit anhaltbar, Zustand überlebt Neustart.
6. **Sprachfilter als Regex** auf die Audiospuren – **nur VOD und Serien**.
7. **Titelfilter als Regex** – für VOD, Serien **und Live-TV**.
8. **Delta-Erkennung** – neue Titel automatisch einplanen, entfernte markieren.

---

## 2. Technologie

- Python 3.11+, FastAPI, SQLite (WAL), APScheduler oder eigener Scheduler-Thread
- `ffprobe` optional als Fallback (Vorhandensein zur Laufzeit prüfen)
- Docker + docker-compose als Deployment, Config über YAML **und** Web-UI editierbar
- Keine externe DB, kein Redis, kein Message-Broker – MVP bleibt Single-Process

---

## 3. Xtream-API-Emulation

### `GET /player_api.php`

Alle Requests an den Upstream weiterreichen, Antwort ggf. filtern:

| action | Verhalten |
|---|---|
| *(kein action)* | 1:1 durchreichen (Login/`user_info`) |
| `get_live_categories` | Kategorien ohne verbleibende Kanäle entfernen |
| `get_live_streams` | **nur** Titelfilter |
| `get_vod_categories` | Kategorien ohne verbleibende Titel entfernen |
| `get_vod_streams` | Titelfilter **und** Sprachfilter |
| `get_vod_info` | 1:1 durchreichen |
| `get_series_categories` | Kategorien ohne verbleibende Serien entfernen |
| `get_series` | Titelfilter **und** Sprachfilter |
| `get_series_info` | 1:1 durchreichen |
| alles andere | 1:1 durchreichen |

Wichtig:
- Antwort-JSON strukturell unverändert lassen (Feldnamen, Typen). IDs **nicht**
  umschreiben – Smarters baut daraus die Stream-URLs.
- `category_id` kommt je nach Panel als String oder Int – beim Vergleich immer
  auf String normalisieren.
- Filterung darf **niemals** auf einen Netzwerk-Request warten. Es wird
  ausschließlich gegen den lokalen Cache gefiltert.

### Stream-Routen

```
GET /live/{user}/{pass}/{rest:path}
GET /movie/{user}/{pass}/{rest:path}
GET /series/{user}/{pass}/{rest:path}
```
→ HTTP 302 auf die identische Upstream-URL. Kein Video-Traffic durch den Proxy.

### Passthrough

```
GET /get.php     → gestreamt weiterreichen (M3U)
GET /xmltv.php   → gestreamt weiterreichen (EPG, ggf. gzip)
```
`get.php` mit `type=m3u_plus`: Zeilen entsprechend denselben Filtern entfernen.

---

## 4. Datenmodell (SQLite)

```sql
CREATE TABLE items (
  kind          TEXT NOT NULL,          -- 'live' | 'vod' | 'series'
  item_id       TEXT NOT NULL,
  name          TEXT NOT NULL,
  category_id   TEXT,
  container_ext TEXT,
  first_seen    INTEGER NOT NULL,
  last_seen     INTEGER NOT NULL,
  removed_at    INTEGER,                -- NULL = aktuell im Katalog
  PRIMARY KEY (kind, item_id)
);

CREATE TABLE audio_tracks (
  kind       TEXT NOT NULL,
  item_id    TEXT NOT NULL,
  track_idx  INTEGER NOT NULL,
  language   TEXT,                      -- Roh-Tag, z.B. 'ger'
  title      TEXT,                      -- Roh-Tag, z.B. 'German DD5.1'
  codec      TEXT,
  channels   INTEGER,
  match_text TEXT NOT NULL,             -- normalisiert, Ziel des Sprach-Regex
  PRIMARY KEY (kind, item_id, track_idx)
);

CREATE TABLE probe_state (
  kind        TEXT NOT NULL,
  item_id     TEXT NOT NULL,
  status      TEXT NOT NULL,            -- 'pending'|'ok'|'no_audio_info'|'error'
  source      TEXT,                     -- 'api' | 'ffprobe'
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_try    INTEGER,
  next_try    INTEGER,
  error       TEXT,
  PRIMARY KEY (kind, item_id)
);

CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE crawl_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL, level TEXT, message TEXT
);
```

Indizes auf `items(kind, removed_at)` und `probe_state(status, next_try)`.

---

## 5. Filterlogik

### 5.1 Titelfilter (alle drei Typen)

Konfiguration:
```yaml
title_filters:
  live:
    include: ['^DE\s*\|', '^AT\s*\|']     # leer = alles erlaubt
    exclude: ['(?i)\bxxx\b', '(?i)adult']
  vod:
    include: []
    exclude: ['(?i)\bxxx\b']
  series:
    include: []
    exclude: []
```

Regeln:
- Geprüft wird gegen `name`; zusätzlich optional gegen den Kategorienamen
  (`match_category: true`).
- Ein Item passiert, wenn **mindestens ein** `include` matcht (oder `include`
  leer ist) **und kein** `exclude` matcht.
- Alle Regex case-sensitiv ausgewertet – Nutzer setzt `(?i)` selbst.
- Ungültige Regex: beim Speichern ablehnen, mit Fehlermeldung in der UI.

### 5.2 Sprachfilter (nur VOD und Serien)

Konfiguration:
```yaml
audio_filters:
  vod:
    include: ['(?i)\b(ger|deu|german|deutsch)\b']
    exclude: []
  series:
    include: ['(?i)\b(ger|deu|german|deutsch)\b']
    exclude: []
  on_unknown: keep        # keep | drop  – Verhalten bei ungeprüften Items
```

Der Regex wird pro Audiospur gegen `match_text` geprüft. `match_text` wird beim
Speichern gebildet als:

```
"{language} {title} {codec} {channels}ch"
```
mit leeren Feldern übersprungen, auf Kleinschreibung normalisiert, Mehrfach-
Whitespace kollabiert. Beispiel: `ger german dd5.1 ac3 6ch`

Ein Item passiert, wenn **mindestens eine Spur** einen `include`-Treffer hat und
**keine Spur** einen `exclude`-Treffer. Hat das Item keine bekannten Spuren
(`status != 'ok'`), entscheidet `on_unknown`.

**Wichtig:** Titelfilter und Sprachfilter sind UND-verknüpft, aber unabhängig
konfigurierbar. Ein Item muss beide bestehen.

### 5.3 Live-TV

Für `kind = 'live'` wird **ausschließlich** der Titelfilter angewendet.
Kein Crawling, kein ffprobe, keine `audio_tracks`-Einträge. Das ist bewusst so:
Live-Streams zu proben würde eine echte Verbindung pro Kanal aufbauen.

---

## 6. Crawler

### 6.1 Ablauf

Der Crawler läuft als einzelner Worker-Thread und arbeitet eine Queue aus
`probe_state` ab (`status='pending'`, `next_try <= now`, Priorität: neue Items
zuerst, dann Fehler-Retries).

Pro Item:
1. **API-Weg**: `get_vod_info&vod_id=…` bzw. `get_series_info&series_id=…`
   aufrufen und Audiospuren extrahieren.
2. Wenn keine verwertbaren Spuren: `status='no_audio_info'` setzen.
3. **ffprobe-Fallback** (nur wenn `ffprobe.enabled: true` und Schritt 2 leer):
   ```
   ffprobe -v quiet -print_format json -show_streams \
           -analyzeduration 3M -probesize 5M <stream-url>
   ```
   mit Timeout (Default 25 s) und anschließendem harten Kill.

Bei Serien: Metadaten der **ersten Episode mit verwertbaren Audiodaten** je
Staffel reichen; Ergebnis gilt für die ganze Serie. Nicht alle Episoden proben.

### 6.2 Parsing-Robustheit

Panels liefern uneinheitlich. Der Parser muss alle folgenden Formen abdecken:
- `info.audio` als Objekt
- `info.audio` als Array von Objekten
- `info.streams` als Array, gefiltert auf `codec_type == 'audio'`
- Sprache unter `tags.language`, `tags.LANGUAGE`, `tags.title` oder `language`
- Fehlende oder leere `info`-Objekte

Fehlt alles: sauber als `no_audio_info` verbuchen, nicht crashen.

### 6.3 Verbindungslimit respektieren

- Vor jedem Crawl-Zyklus `user_info` abrufen und `active_cons` / `max_connections`
  auslesen.
- Ist `active_cons >= max_connections - reserve_slots` (Default `reserve_slots: 1`),
  pausiert der Crawler und prüft nach `slot_recheck_seconds` (Default 60) erneut.
- Zwischen zwei Upstream-Requests `request_delay_seconds` warten (Default 1.0).
- Das gilt besonders für ffprobe – ein Probe belegt eine echte Verbindung.

### 6.4 Zeitfenster

```yaml
crawl_schedule:
  enabled: true
  windows:
    - days: [mon, tue, wed, thu, fri, sat, sun]
      start: "02:00"
      end:   "06:00"
  timezone: "Europe/Berlin"
```

- Mehrere Fenster erlaubt, auch überlappend.
- Fenster über Mitternacht (`start: "23:00"`, `end: "03:00"`) müssen korrekt
  behandelt werden.
- Außerhalb der Fenster schläft der Worker.
- `crawl_schedule.enabled: false` → Crawler läuft durchgehend.
- Läuft gerade ein Probe, wenn das Fenster endet: aktuelles Item zu Ende
  bearbeiten, dann anhalten. Kein hartes Abbrechen mitten im Schreibvorgang.

### 6.5 Pause

- Globaler Schalter in `settings` (`crawler_paused = '1'|'0'`), persistent.
- Umschaltbar über UI-Button und `POST /api/crawler/pause` bzw. `/resume`.
- Pause hat Vorrang vor dem Zeitfenster.
- Reaktion innerhalb von 5 Sekunden; laufendes Item darf zu Ende laufen.
- Status jederzeit über `GET /api/crawler/status` abrufbar:
  `running | paused | outside_window | waiting_for_slot | idle`

### 6.6 Delta-Erkennung

Separater Sync-Job, Intervall `sync_interval_minutes` (Default 360), unabhängig
vom Crawl-Zeitfenster (er ist billig – drei Listen-Requests):

1. `get_live_streams`, `get_vod_streams`, `get_series` abrufen.
2. Für jedes gelieferte Item `last_seen = now` setzen, neue Items anlegen
   (`first_seen = now`, `removed_at = NULL`).
3. Neue VOD-/Serien-Items bekommen `probe_state` mit `status='pending'` und
   hoher Priorität.
4. Items in der DB, die **nicht** in der aktuellen Liste stehen und
   `removed_at IS NULL` haben: `removed_at = now`. Nicht löschen.
5. Taucht ein Item mit `removed_at` wieder auf: `removed_at = NULL` setzen und
   **neu proben** – Provider recyceln IDs, der Inhalt kann ein anderer sein.
   Auslöser: `name` weicht vom gespeicherten Namen ab → `audio_tracks` löschen
   und `status='pending'`.
6. Items mit `removed_at` älter als `purge_after_days` (Default 30) endgültig
   löschen.
7. Ergebnis ins `crawl_log`: X neu, Y entfernt, Z zurückgekehrt.

---

## 7. Web-UI (minimal, aber vollständig)

Eine Seite, serverseitig gerendert oder simples HTMX – kein SPA-Framework nötig.

**Dashboard**
- Crawler-Status, aktuelles Item, Fortschritt (`ok` / `pending` / `error` je Typ)
- Buttons: Pause / Resume, „Sync jetzt", „Alle Probes zurücksetzen"
- Letzte 50 Zeilen `crawl_log`

**Einstellungen**
- Upstream-URL, Username, Passwort
- Zeitfenster hinzufügen/entfernen
- `request_delay_seconds`, `reserve_slots`, `sync_interval_minutes`, ffprobe an/aus
- Alle Regex-Felder aus Abschnitt 5, je Typ, mit Live-Validierung

**Filter-Vorschau** (wichtigstes Feature zum Justieren)
- Typ wählen, Regex eingeben → zeigt sofort, wie viele Items durchkommen,
  plus je 20 Beispiele „passiert" und „gefiltert", jeweils mit den erkannten
  Audiospuren. Rein lesend, ändert nichts an der Konfiguration.

**Katalog-Browser**
- Tabelle aller Items mit Suche, Filter auf Typ/Status, Spalte mit Audiospuren
- Möglichkeit, ein einzelnes Item manuell neu proben zu lassen

---

## 8. Konfiguration

Vollständige `config.yaml` mit allen oben genannten Schlüsseln, plus:

```yaml
upstream:
  base_url: "http://provider.example:8080"
  username: "..."
  password: "..."
  timeout_seconds: 20
  user_agent: "IPTVSmartersPro"      # manche Panels prüfen das

server:
  host: "0.0.0.0"
  port: 8080

database:
  path: "./data/proxy.db"

ffprobe:
  enabled: false
  timeout_seconds: 25
  binary: "ffprobe"
```

Änderungen über die UI werden in dieselbe Datei zurückgeschrieben und ohne
Neustart wirksam (Hot-Reload der Filter; Crawler-Parameter zum nächsten Zyklus).

Zugangsdaten dürfen nicht im Klartext geloggt werden.

---

## 9. Randfälle, die abgedeckt sein müssen

- **Upstream nicht erreichbar**: Der Proxy muss weiter antworten. Listen aus der
  DB ausliefern, statt einen Fehler an Smarters zu geben. Nur bei fehlendem Cache
  502 zurückgeben.
- **Leere Kategorien**: Nach dem Filtern dürfen keine Kategorien mit null Items
  in der Liste stehen – Smarters zeigt sonst leere Ordner.
- **Smarters cacht lokal**: In der UI einen Hinweis anzeigen, dass nach
  Filteränderungen ein Playlist-Refresh in der App nötig ist.
- **Erster Start**: Cache ist leer. `on_unknown: keep` als Default, sonst ist der
  Katalog bis zum Ende des ersten Crawls praktisch leer.
- **Parallele Requests von Smarters**: Die App feuert beim Playlist-Refresh
  mehrere Requests gleichzeitig – Filterung muss thread-safe und schnell sein
  (reine DB-Reads, keine Upstream-Calls).
- **Große Kataloge**: 20.000+ VOD-Einträge müssen in unter einer Sekunde
  gefiltert und serialisiert werden. Filter-Ergebnisse im Speicher cachen und
  bei Config- oder Sync-Änderung invalidieren.

---

## 10. Nicht-Ziele (bewusst außen vor)

- Mehrere Provider mergen
- Live-Streams proben oder umkodieren
- Multi-User-Verwaltung, Authentifizierung der UI (läuft im LAN)
- Download- oder Aufnahmefunktion
- EPG-Manipulation über das Durchreichen hinaus

---

## 11. Abnahmekriterien

1. Smarters verbindet sich mit dem Proxy und zeigt Live, Filme und Serien an.
2. Wiedergabe funktioniert in allen drei Kategorien (302-Redirect greift).
3. Ein Sprach-Regex auf `ger|deu|german` reduziert die VOD-Liste sichtbar, und
   ein zuvor bekannter deutscher Titel ist weiterhin enthalten.
4. Ein Titel-Regex `(?i)xxx` als `exclude` entfernt entsprechende Live-Kanäle.
5. Pause stoppt den Crawler binnen 5 Sekunden und überlebt einen Neustart.
6. Außerhalb des Zeitfensters wird nicht gecrawlt; Status zeigt `outside_window`.
7. Ein neu im Upstream aufgetauchter Titel wird beim nächsten Sync erkannt und
   landet als `pending` in der Queue.
8. Ein aus dem Upstream verschwundener Titel bekommt `removed_at` und ist aus
   den ausgelieferten Listen verschwunden.
9. Upstream abschalten → Smarters bekommt weiterhin die zuletzt bekannten Listen.
10. `docker compose up` startet die Anwendung ohne weitere Handgriffe.

---

## 12. Umsetzungshinweise

- Beginne mit der API-Emulation im Durchreich-Modus und prüfe, dass Smarters
  sich verbindet. Erst danach Filter und Crawler ergänzen.
- Lege dem Repo eine `README.md` mit Setup, Beispiel-Config und einem Abschnitt
  zu den Grenzen der Sprach-Erkennung bei.
- Schreibe Unit-Tests für den Audio-Parser mit mindestens fünf realistischen,
  unterschiedlich strukturierten `get_vod_info`-Antworten als Fixtures – das ist
  die fehleranfälligste Stelle des Projekts.

