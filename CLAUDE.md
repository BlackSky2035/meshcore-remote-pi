# MeshCore Remote – Projektkontext für KI-Assistenten

> Diese Datei wird von KI-Assistenten (z. B. Claude Code) automatisch gelesen. Sie gilt auch für dich als Mensch.
> **Das Projekt ist KI-generiert und nicht von einem Menschen überprüft worden.** Prüfe, was eine KI hier tut, besonders alles, was Funk sendet oder `sudo` braucht.

## Für KI-Assistenten: zuerst lesen

**Wenn du das Projekt installieren sollst:** Folge `docs/raspberry-pi.md` Schritt für Schritt und die Doku `README.md`. Frage den Nutzer vorher nach dem, was du nicht erraten darfst:
GitHub-Name und Repo-Namen (Hauptprogramm und Watchdog), Benutzername und Hostname auf dem Pi, ob ein Node per USB angeschlossen ist, ob Tailscale genutzt wird und ob ein
Zugangspasswort gesetzt werden soll. Erfinde keine Werte. Führe `sudo`-Befehle, `systemctl enable`, Änderungen an `/etc` und Löschbefehle erst nach Rückfrage aus.
Gib Passwörter und Tokens nie im Chat, in Commits oder in Protokollen aus. `config.toml`, `data/` und `*.db` gehören nie ins Git.

**Wenn du das Projekt weiterentwickeln sollst:**
1. Lies die Abschnitte „Regeln für den Code“, „Schutzregeln für Automatik“ und „Ausfallsicherheit“ in dieser Datei. Sie sind nicht verhandelbar.
2. Das Programm **sendet über Funk** (LoRa, geteiltes Medium, Sendezeitgrenzen). Teste nie mit echter Hardware ohne ausdrückliche Zustimmung des Nutzers. Lockere kein Sendebudget,
   kein Mindestintervall und keinen Schleifenschutz (Erwähnung bzw. `[auto]`). Keine neuen Funktionen, die ohne Begrenzung senden.
3. Neue Funktion am Node: immer in `app/node/base.py`, `sim.py` UND `real.py`. Die Bibliothek `meshcore` wird nur in `real.py` benutzt.
4. Jede Änderung braucht einen Test (`python -m pytest -q`, dazu der UI-Rauchtest `tests/ui_smoke`). Neue Schutzregeln brauchen einen Test in `tests/test_automation.py`.
5. Pflege die `README.md` mit, und schreibe **ehrlich** dazu, was nur im Simulator und was an echter Hardware getestet wurde. Behaupte nie, etwas funktioniere an Hardware, wenn es nicht geprüft ist.
6. Erhöhe bei jedem Push die letzte Zahl in `app/__init__.py` (`__version__`, Form `0.1.N`).
7. Das Frontend hat bewusst keinen Build-Schritt. Alles, was in HTML eingesetzt wird, läuft durch `esc()`.
8. Bei Unsicherheit über die fachliche Bedeutung einer Anforderung: kurz nachfragen statt still eine Annahme umzusetzen.
9. Keine neuen Abhängigkeiten oder Bezahldienste ohne Rückfrage (aktuell nur kostenlose Dienste ohne Schlüssel).
10. Dokumentation, Kommentare und Commit-Nachrichten sind auf Deutsch (kurz, fachlich).

Das Schwester-Projekt ist der **Watchdog** (eigenes Repo, nur Python-Standardbibliothek). Beide Projekte hängen zusammen: Der Watchdog erwartet `GET /api/version`, den Branch `main` und `requirements.txt` hier.

## Ziel
Web-Oberfläche zur Fernsteuerung von MeshCore-LoRa-Nodes, funktional an die offizielle
MeshCore-App angelehnt, dazu Automatisierung (automatische Pings, Akku-Überwachung von
Repeatern, automatische Ping-Antworten). Zwei Nodes (Heltec V4), je an einem eigenen
Raspberry Pi in zwei verschiedenen lokalen Netzen. Entwickelt und getestet wird auf dem Pi
(Raspberry Pi OS); andere Systeme (macOS, Windows) sind nicht getestet.

Geplante Hardware: Raspberry Pi 3 Model B (Ethernet, 4× USB-A) für die erste Station,
später evtl. ein Pi Zero 2 W für die zweite (braucht OTG-Adapter, nur WLAN). Ein aktiver
USB-Hub ist nicht nötig; der Heltec V4 zieht beim Senden bis ca. 750 mA (Datenblatt), ein
gutes 2,5-A-Netzteil und ein kurzes Kabel genügen. Ein angeschlossener Akku am Node erhöht
den Spitzenstrom (Ladestrom bis 500 mA).

## Anforderungen
- Im selben lokalen Netz wie ein Pi soll die Steuerung auch ohne Internet funktionieren.
- Von außen sollen beide Geräte erreichbar sein, per Tailscale (privates Netz, kein
  öffentlicher Zugriff).
- Backend und UI laufen direkt auf dem jeweiligen Pi (Python/FastAPI), Echtzeit per
  WebSocket, kein Cloud-Umweg.
- Eigenes, funktional gleichwertiges Design. Keine Grafiken, Icons oder Logos aus der
  offiziellen MeshCore-App übernehmen und den Namen „MeshCore“ nicht so verwenden, als wäre
  die Seite offiziell. (Lizenz der offiziellen App wurde nicht geprüft.)

## Architektur
```
app/
  main.py        App-Fabrik (create_app), WebSocket
  supervisor.py  Supervisor: Verbindung halten, USB-Anschluss beobachten, Herzschlag, Neuverbinden mit Wartezeit
  auth.py        optionale Anmeldung (Passwort, signiertes Cookie, Sperre nach Fehlversuchen)
  network.py     Netzgraph aus Kontaktpfaden und Nachbarlisten
  stats.py       Funkstatistik (StatsSampler, derive)
  configbackup.py Sicherung/Wiederherstellung der Einstellungen (ohne Privatschlüssel)
  room.py        Raum-Bot (Software-Room per Direktnachricht)
  peers.py       gegenseitige Überwachung mehrerer Stationen
  nodeconfig.py  Eigener Node: Felder, Wertebereiche, Bestätigung riskanter Änderungen, Uhr, Neustart
  repeater.py    Repeater verwalten: Regeln (Sperren/Bestätigung/Schwärzen/Tempo), Einstellungskatalog, Passwort-Datei
  usb.py         Port-Erkennung (Punkte), UsbMonitor (Zustand, Ereignisse in `usb_events`, Verfügbarkeit, Meldung)
  api.py         REST-Endpunkte (make_router)
  runtime.py     Hub (WebSocket + interne Listener) und build(): verdrahtet alles
  automation.py  Regeln, Zeitplan, Auto-Antwort, SendBudget (Schutz vor zu viel Funkverkehr)
  storage.py     SQLite (Nachrichten, Akku, Pings, Regeln, Einstellungen, Log) + AsyncStorage
  schemas.py     Eingaben und Regel-Validierung (Mindestintervalle!)
  config.py      config.toml + MC_*-Umgebungsvariablen (Feldname = MC_<FELDNAME>)
  node/
    base.py      Schnittstelle BaseNode, normalize_contact, NodeError
    real.py      echter Node über die Bibliothek `meshcore`
    sim.py       Simulator (SimNode), mit Test-Hilfen (inject_message, fail_status, sent)
  static/        Oberfläche: Vanilla-JS (ES-Module), Seiten in pages/, Leaflet lokal in vendor/
tests/           pytest (Backend), ui_smoke/ (jsdom-Rauchtest der Oberfläche)
```

### Regeln für den Code
- Alles, was die `meshcore`-Bibliothek betrifft, gehört nach `app/node/real.py`. Der Rest
  kennt nur `BaseNode`. Neue Node-Funktionen immer in `base.py`, `sim.py` UND `real.py`
  ergänzen, damit alles ohne Hardware testbar bleibt.
- Nur ein Prozess darf den Node ansprechen. Automatik läuft deshalb im selben Prozess
  (Backend), nicht im Browser.
- Event-Listener im Hub laufen als eigene Tasks und werden nicht abgewartet. Grund: Der
  Node ruft `emit` aus dem Lese-Loop der Bibliothek auf; ein Listener, der dort selbst einen
  Befehl sendet und auf die Antwort wartet, würde den Loop blockieren (Deadlock). Das nicht
  ändern.
- Keine festen Pfade, keine systemspezifischen Annahmen. Plattformunterschiede (serieller
  Port etc.) stehen in `config.toml` bzw. `MC_*`.
- Python 3.11 oder neuer (Raspberry Pi OS Bookworm hat 3.11; `tomllib` wird genutzt).
- Frontend ohne Build-Schritt, damit der Pi kein Node.js braucht und der spätere Update-Weg
  (`git pull`) einfach bleibt. Alles, was in HTML eingesetzt wird, läuft durch `esc()`.

## Schutzregeln für Automatik (nicht aufweichen)
LoRa ist ein geteiltes Medium mit begrenzter Sendezeit (EU-Bänder: Duty Cycle).
- Globales Sendebudget (`SendBudget`): Mindestabstand (Standard 15 s) und Obergrenze pro
  Stunde (Standard 30), einstellbar in `[automation]`.
- Mindestintervall 5 Minuten für Ping- und Akku-Regeln, 15 Minuten für `channel_ping` (in `schemas.py`), Abkühlzeit je
  Absender mindestens 30 s, Antworttext höchstens 150 Zeichen. Die fertige Auto-Antwort (Erwähnung + gefüllte Platzhalter) wird auf `REPLY_MAX_BYTES` = 139 Byte (UTF-8) gekürzt, die führende Erwähnung bleibt immer erhalten (`Automation._reply_text`, `cut_bytes`). „enthält“-Treffer: ganzes Wort per `(?<!\w)…(?!\w)`, damit greifen auch Auslöser wie „!ping“. `SendBudget.acquire(n)` bucht n Plätze (alle oder keinen). Aufräumen (`retention_days`) läuft gleich nach dem Start und auch bei Pause.
- Antworten der Regelart `auto_reply` beginnen mit einer Erwähnung des Absenders (`@[Name] `) und werden nie auf Nachrichten beantwortet, die selbst mit einer Erwähnung (`MENTION_RE`)
  oder mit `[auto]` beginnen (Schleifenschutz). Das Präfix `[auto] ` (`AUTO_PREFIX`) benutzt nur noch der Raum-Bot (`room.py`, Schleifenschutz auch zwischen zwei Stationen). Nur normaler Text
  Der Platzhalter `{name}` wird ebenfalls als Erwähnung `@[Name]` eingefügt (`mention_tag`, eckige Klammern im Namen entfernt); beginnt der gefüllte Text schon mit einer Erwähnung, kommt keine zweite davor.
  (`txt_type == 0`); Direktnachrichten nur an bekannte Kontakte, Kanalnachrichten nur in den Kanälen, die in der Regel gewählt sind.
- Ping-Nachrichten (`mode message`, `channel_ping`) werden BEWUSST ohne `[auto]` gesendet (viele
  fremde Bots reagieren nur auf das nackte „ping“). Schleifen verhindert weiter, dass unsere
  Auto-Antworten mit einer Erwähnung beginnen und solche eingehenden Nachrichten ignoriert werden.
- `mode trace` = Zero-Hop-Ping: `send_trace(path=<erstes Byte des Public Keys>)`, Antwort `TRACE_DATA`
  mit SNR (hin = `path[0].snr`, zurück = letzter Eintrag). Nur für Repeater/Rooms in direkter
  Reichweite; gleiche 1-Byte-Hashes sind nicht unterscheidbar. An echter Hardware NICHT getestet.
- Platzhalter in Antworten werden per `replace` ersetzt, nicht per `format` (keine
  Format-String-Injektion).
- Globaler Pausenschalter, bleibt über Neustarts erhalten. Greift sofort: `SendBudget.halted` (= `automation.paused`) wird vor UND nach der Wartezeit
  geprüft, Ablehnungsgrund in `budget.why` („Automatik pausiert“/„Stundenlimit erreicht“). Test `test_pause_during_budget_wait_stops_the_send`.
- Auto-Antwort: Auslöser nur aus Leerzeichen → 422 (`AutoReplyConfig._match_not_blank`). Kanal-Ping: Platz muss auf dem Node eingerichtet sein
  (`api._channel_exists`, 422; Liste nicht lesbar → nicht blockieren; unveränderte Regel beim Ein/Aus nicht prüfen), Formular `required`, leer nie als 0.
- Konfigurationsgrenzen (`config.LIMITS`, `apply_limits`, nicht aufweichen): `auto_min_gap_s` ≥ 5, `auto_max_per_hour` ≤ 60, `manual_min_gap_s` ≥ 1,
  `manual_max_per_min` ≤ 30 (Funk-Grenzen nicht im Modus sim), `retention_days` ≥ 7 (immer). Hinweise in `settings.config_notes` → `problems`.
  Falscher Typ → Standardwert + Hinweis. TOML-Syntaxfehler → Standardwerte mit `mode = "serial"` + Hinweis, ABER mit `access_password` in der Datei (ohne
  `MC_ACCESS_PASSWORD`) → `SystemExit` (nie ungewollt ohne Anmeldung). Tests: `tests/test_small_fixes.py`.
- Startverzögerung (20 s) und Staffelung (10 s) der Regeln nach Neustart; der nächste Termin
  wird vor der Ausführung gesetzt, damit Fehler nie zu Dauerschleifen führen.
  Der letzte Lauf je Regel steht in `settings` (`rule_last_run_<id>`, Wanduhr): nach Neustart, Update oder Bearbeiten/Ein-Aus-Schalten einer Regel
  läuft sie frühestens ein Intervall nach dem letzten Lauf (`_wait_since_last_run`, höchstens ein Intervall Wartezeit, falls die Uhr des Pi
  nach dem Start noch falsch geht; Lesefehler → nur Startverzögerung). Löschen einer Regel entfernt den Eintrag (`forget_rule`).
  Die Oberfläche warnt, wenn zwei aktive Kanal-Pings denselben Kanal anpingen. Tests: `tests/test_automation.py` (Neustart, Bearbeiten, Uhr).
  Protokoll: jede erfolgreiche automatische Sendung (Kanal-Ping, Ping, Akku-Abfrage, Advert) und die erste Planung je Regel („geplant: nächster Lauf …“ mit Grund) werden mit `info` protokolliert; Namen über `_channel_label`/`_contact_label` (Fehler → Platznummer/Schlüssel). Test `test_automatic_sends_and_planning_are_logged`.
- Akku-Warnung nur einmal pro Unterschreitung (mit 100 mV Hysterese).
- Temperatur: eigene Funkanfrage (`repeater_telemetry`), verbraucht Sendebudget; ohne Sensor 24 h Pause
  (`NO_TEMP_BACKOFF_S`). Seite „Akku“: `/api/battery/*`, Temperatur gehört zu einer Akku-Messung,
  wenn sie ≤120 s entfernt ist. Diagramm: `static/lib/linechart.js` (Fadenkreuz, Tooltip, CSS-Variablen; Messpunkte nur, wenn sie im Mittel ≥ 10 px auseinanderliegen oder es höchstens 2 sind, sonst nur die Linie; Rand der Punkte hinter der Füllung).
- Akku-Seite: bis zu 3 Repeater (Plätze „Repeater 1–3“, je Platz feste Farbe `--c-batt/2/3`, alle Linien durchgezogen (`SHAPES` in `pages/battery.js`; gestrichelt wirkte abgehackt), identisch in Akku- UND Temperatur-Diagramm); die Auswahl (`battery_selection` in `settings`, Liste mit 3 Plätzen) wird beim Ändern gespeichert (`PUT /api/battery/selection`; alter `battery_default_node`/`battery_compare_node` dient als Ersatz, wenn noch nichts gespeichert ist). Unter jedem Feld steht der letzte Wert. Die Tabelle zeigt je einen Repeater, umgeschaltet über Reiter (`#tableTabs`, gleiche Farbe wie im Diagramm); die CSV gehört zum gewählten Reiter. „Jetzt abfragen“ geht Repeater für Repeater mit Pause `battery_poll_gap_s` (Standard 20 s, Obergrenze 300 s) dazwischen, in `pages/battery.js`: nie mehrere Funkabfragen unmittelbar hintereinander (Spannungsabfall am Pi). Tests: `tests/test_battery.py`, UI-Smoke „Akku-Seite“.
- Auto-Antwort: Limit je Regel `max_per_hour` (rollend 60 min, synchron reserviert) und Pause je Person;
  gilt für Direktnachrichten und Kanäle.
- Funk von Hand (`ratelimit.py`, `ManualLimit`, in `api.py` `radio(n)` vor `/message`, `/channel`, `/advert`, `/nodes/*/poll` (mit Temperatur 2), `/nodes/*/ping`, `/sensors/*/read`):
  Mindestabstand `manual_min_gap_s` (2 s) und `manual_max_per_min` (20), sonst 429 ohne Senden. Repeater-Verwaltung nicht (eigenes `_radio_slot`). Tests lockern über
  `conftest.py` (rt-Settings 0/10000); UI-Rauchtest-Server mit `MC_MANUAL_MIN_GAP_S=0 MC_MANUAL_MAX_PER_MIN=10000` starten. Tests: `tests/test_ratelimit.py`.
- Raum-Teilbudget (`room.py`): höchstens `max_per_hour // ROOM_SHARE` (3) Plätze je Stunde (mindestens `CONFIRM_TX_MAX`), eigene Zählung `_room_sent`;
  unbekannte Befehle → Hilfe höchstens 1× je Stunde und Mitglied (`UNKNOWN_HELP_S`). Tests: `tests/test_room.py`.
- Temperatur bucht den Budget-Platz erst unmittelbar vor `repeater_telemetry` (`poll_node(..., budget=)`), nicht bei 24-h-Pause oder gescheitertem Status.
- Pausiert starten: `Automation.start` liest `automation_paused`; nicht lesbar oder RAM-Rückfall (`start_paused`, gesetzt in `build()`) → pausiert + `start_note` (Hinweis in `problems`);
  scheitert `start()` ganz, setzt `main.py` `paused = True`. Tests: `tests/test_robustness.py`.

## Stand
Fertig und im Simulator getestet (pytest und UI-Rauchtest):
- Seiten: Übersicht, Kontakte (suchen, filtern, hinzufügen per Kontaktkarte, löschen, Pfad
  zurücksetzen, Karte teilen, Chat), Knoten (Akku, Erreichbarkeit, Verlauf, Abfragen, Ping),
  Karte (Leaflet, lokal ausgeliefert), Automatik (Regeln anlegen/bearbeiten/löschen, Pause,
  Budget, Protokoll).
- Automatik: Akku-Überwachung von Repeatern, automatische Pings (Statusabfrage oder
  Nachricht), Auto-Antworten.
- Speicherung in SQLite (`data/meshcore.db`), Aufbewahrung `retention_days` (Standard 90).

**`app/node/real.py` läuft im Betrieb auf einem Raspberry Pi mit einem Node per USB (seriell), wurde aber nicht systematisch geprüft;** Bluetooth und WLAN/TCP sind gar nicht getestet. Feldnamen und Signaturen
sind gegen den Quelltext von `meshcore` 2.3.15 geprüft (Kontaktfelder, `req_status_sync`
liefert `bat` in mV, `import_contact` erwartet Bytes, `export_contact` liefert
`meshcore://…`). Zu verifizieren am echten Node (`MC_MODE=serial MC_DEBUG=1`):
- Kommt Name/Modell/Firmware/Akku im Status an?
- Funktionieren Kontakte hinzufügen (Kontaktkarte einfügen), Löschen, Pfad zurücksetzen?
- Liefert `req_status_sync` bei Repeatern eine Antwort, und ist `bat` wirklich in mV?
- Kommt bei Direktnachrichten `pubkey_prefix` (12 Hex-Zeichen) an und wird der Absender
  gefunden?
- Meldet `wait_for_event(ACK, {"code": …})` beim Ping im Nachrichtenmodus korrekt?

## Bekannte Grenzen
- Kartenkacheln brauchen Internet (Standard OpenStreetMap). Ohne Internet bleiben die Marker
  sichtbar, der Hintergrund fehlt. `tile_url` in `[map]` erlaubt später einen lokalen
  Kachelserver. OSM-Nutzungsbedingungen beachten (leichte Nutzung, Quellenangabe vorhanden).
- Anmeldung ist optional (`access_password`, siehe unten). Ohne Passwort gibt es keinen Login; Zugriff ist dann nur durch `127.0.0.1`, das lokale Netz
  oder Tailscale begrenzt. Der Watchdog (eigener Port, Link „System ↗“) hat KEINE Anmeldung dieses Programms.
- Auto-Antworten gibt es für Direktnachrichten und Kanäle (`handle_channel_incoming`, im Kanal zählt der Absendername für die Pause); an echter Hardware nicht getestet.
- Die Akku-Skala ist LINEAR (3000–4200 mV = 0–100 %) und hat genau EINE Definition je Sprache: `app/battery.py` (`battery_pct`/`battery_mv`, rundet wie JavaScript: 0,5 auf) und `lib/util.js` (`batteryPct`/`batteryMv`); beide müssen gleich bleiben (Test `tests/test_battery_scale.py`). Die Warnstufe einer Akku-Regel wird im Formular in PROZENT eingegeben (`low_pct`), gespeichert aber in mV (`low_mv`, Schema unverändert); `lowMvFromPct` lässt den gespeicherten mV-Wert unverändert, solange der Prozentwert nicht geändert wurde. Früher rechnete `push.py` mit einer anderen LiPo-Kurve (3700 mV = 12 % statt 58 %), das ist entfernt. Warnfarbe in der UI unter
  3500 mV; die Warnschwelle der Automatik steht in der jeweiligen Regel.

## Kanäle
- Seite `pages/channels.js`, API `/api/channels` (Liste ohne Schlüssel; Schlüssel nur über
  `/api/channels/{idx}/secret`), Node-Methoden `channels/set_channel/remove_channel/max_channels`.
- Kanalplatz-Schema des Nodes: leer = Name leer und Schlüssel nur Nullen. Löschen schreibt genau das.
- Namen mit `#` sind Hashtag-Kanäle: Schlüssel = SHA-256(Name)[:16] (so macht es die Bibliothek);
  ein mitgegebener Schlüssel wird ignoriert. Sonst ist der Schlüssel 16 Byte, ohne Angabe zufällig.
- Eingehende Kanalnachrichten kommen als „Absender: Text“; `runtime.split_sender` trennt das.
- Verlauf liegt in `messages` (kind `channel`, peer = Platznummer). Beim Löschen eines Kanals oder
  einem neuen Schlüssel auf demselben Platz wird der Verlauf gelöscht (Platz kann neu belegt werden).
- `RealNode.channels()`: gelesene Plätze bleiben in `_chan_known` (auch bei Teil-Liste, `_chan_idx_by_name` wird trotzdem gesetzt); stumme Plätze (`_chan_failed`) erst nach
  `CHANNELS_RETRY_S` (60 s) erneut, je Aufruf höchstens `CHANNELS_TOTAL_S` (45 s); solange einer fehlt → `NodeError` (nie Teil-Liste ausgeben: belegter Platz könnte als frei gelten).
  `invalidate_channels()`/`connect()` vergessen alles. Tests: `tests/test_real_channels.py` (silent_slot, overall_time_limit).
- Gegen meshcore 2.3.15 (Quelltext) geprüft, an echter Hardware NICHT getestet: `get_channel` für
  leere Plätze, `max_channels` aus device_query, Verhalten beim Senden auf nicht eingerichtete Plätze.

### Pfade und Repeats (Kanal-Chat)
- Gespeichert wird immer (`messages`: `sender_ts, path, path_hash_size, hops, snr, rssi`; Tabelle
  `msg_repeats`), die Anzeige schaltet je Kanal die Einstellung `channel_view_<idx>` (API
  `PUT /api/channels/{idx}/view`). Beim Löschen/neuem Schlüssel wird sie zurückgesetzt.
- Quelle (RealNode): `mc.set_decrypt_channel_logs(True)`; die Bibliothek hängt dann `path`, `SNR`,
  `RSSI` an `CHANNEL_MSG_RECV`. Gehörte Wiederholungen eigener Nachrichten kommen aus `RX_LOG_DATA`
  (nur `payload_type == 5` mit entschlüsseltem `message` wird als Event `channel_rx` weitergereicht).
  Zuordnung in `runtime.py`: gleicher Kanal + gleicher Zeitstempel (wir geben ihn beim Senden vor
  und speichern ihn als `sender_ts`) + gleicher Text; je Weg (`path`) nur ein Eintrag.
- Hop-Kennungen sind die ersten Bytes des Repeater-Public-Keys; die Oberfläche löst sie über die
  Kontaktliste in Namen auf (bei Mehrdeutigkeit wird die Kennung gezeigt).
- Doppelklick auf eine fremde Nachricht setzt `@[Name] ` ins Eingabefeld (Erwähnung, keine echte
  Antwort-Funktion; das Format `@[Name]` ist eine Annahme über die MeshCore-App: seit App 1.27 gibt es Erwähnungen in Kanälen und Namen dürfen keine eckigen
  Klammern mehr enthalten; Schreibweise `@[Name]` in der MeshCore-App bestätigt: eine Raum-Nachricht mit `@[Alice]` erscheint dort als blauer Erwähnungs-Chip; ungeprüft: Benachrichtigung „nur Erwähnungen“). Es wird nur die Erwähnung eingefügt, keine Uhrzeit (die frühere Uhrzeit hinter der Erwähnung, `withReceiptTime`, ist entfernt): gesendet wird genau der Text im Eingabefeld.
  `mentionTag` entfernt eckige Klammern aus dem Namen. Tests: UI-Smoke „Erwähnung“.
- NICHT an Hardware getestet: ob der Heltec die RX-Log-Pakete immer liefert, und ob die
  Pfadangaben zu den Nachrichten passen.

## Watchdog (separates Repo)
Der Watchdog verwaltet dieses Programm. Anforderungen an dieses Repo: Branch `main`, sauberer
Git-Stand, `GET /api/version` (Gesundheitsprüfung, nicht entfernen oder umbenennen!),
`requirements.txt` für Abhängigkeiten, Start per `python -m app.main`. Der Link „System ↗“
nutzt `watchdog_port` aus `/api/config`. Ein später möglicher Ausbau: den Watchdog hinter
diesem Server als Proxy einbinden, damit nur ein Port freigegeben werden muss.

## Nächste Schritte
1. Die Liste „Zu verifizieren am echten Node“ oben am Pi abarbeiten und `real.py` bei Abweichungen anpassen.
2. Die Pi-Anleitung (`docs/raspberry-pi.md`) auf einem frisch eingerichteten Pi von vorne bis hinten durchspielen.
3. (ERLEDIGT, eigenes Repo Watchdog; siehe dessen README) Watchdog: eigenes kleines Programm mit eigener Status-/Update-Schnittstelle,
   unabhängig vom Hauptprogramm. Update-Ablauf: Dienst stoppen, `git pull`, Dienst starten,
   prüfen ob er läuft, Erfolg/Fehler melden. Letzten funktionierenden Commit merken und bei
   Fehlschlag automatisch zurückspielen. Prozessverwaltung über systemd
   (`systemctl is-active`), ohne systemd über eine einfache Subprozess-Variante, damit die
   Logik auch dort testbar ist. Keine GitHub Actions nötig.
4. systemd-Units für Hauptprogramm und Watchdog (Autostart, Neustart bei Absturz).
5. Tailscale auf beiden Pis und den Clients einrichten; lokal zusätzlich per mDNS
   (`<hostname>.local`) erreichbar machen.
6. Weitere Funktionen der MeshCore-App nachziehen (Radio-Einstellungen, Telemetrie). Priorität mit dem Nutzer abstimmen.

## Tests
- Backend: `pip install -r requirements-dev.txt && pytest` (läuft im Simulator, ohne Hardware).
- UI-Rauchtest: siehe `tests/ui_smoke/README.md` (jsdom gegen einen laufenden Sim-Server).
- Neue Schutzregeln der Automatik brauchen immer einen Test in `tests/test_automation.py`.
- `Hub.drain()`/`PushService.drain()` (nur Test-Hilfen) warten nur auf NOCH LAUFENDE Aufgaben und geben der Ereignisschleife danach Luft (`sleep(0)`). Die frühere Form `while self._tasks: await gather(...)`
  drehte sich endlos (98 % CPU), wenn eine fertige Aufgabe noch eingetragen war: `await` auf Fertiges unterbricht nie, der `discard`-Rückruf kam nicht dran. Regressionstest: `tests/test_drain.py`.
- Hinweis zum Arbeiten in der Shell: nicht `pkill -f`/`pgrep -f` mit Mustern verwenden, die im
  eigenen Befehlstext vorkommen (beendet sonst die eigene Shell). PID-Datei nutzen.

## Ausfallsicherheit (nicht aufweichen)
- Neue Endpunkte/Listener/Schleifen: Fehler abfangen und melden, nie das Programm beenden. Startschritte in `lifespan` einzeln schützen.
- Datenbank kann ausfallen (`build()` fällt auf RAM zurück, `sqlite3.Error` → 503). Tests: `tests/test_resilience.py`.

## Vor jedem Push auf GitHub

**Versionsnummer:** Bei jedem Push die letzte Zahl der Version in `app/__init__.py` (`__version__`, Form `0.1.N`) um 1 erhöhen
(0.1.1 → 0.1.2 …). Die Version steht im Kopf der Oberfläche und in `/api/version`.
Bevor eine KI selbst nach GitHub pusht, **immer beide Projekte** (dieses und den Watchdog) auf die vereinbarten
Absicherungen prüfen. Nur pushen, wenn alles unten erfüllt ist; sonst zuerst beheben oder den Nutzer informieren und NICHT pushen.
Das Ergebnis der Prüfung (kurz, Punkt für Punkt) dem Nutzer mitteilen.

**Watchdog (hat Vorrang: Erreichbarkeit ist oberste Priorität)**
1. Status kommt aus dem `StatusCache`; im Anfragepfad (`server.py`) wird nirgends auf Git, systemctl, Prozesse oder das Netz gewartet.
2. Alle Handler laufen über `_guarded` (Fehler → JSON-500, nie Abbruch); `/api/ping` greift auf nichts zu.
3. Server-Schleife (`serve`) baut den HTTP-Server nach Absturz/belegtem Port neu auf; Verbindungslimit und Socket-Timeout aktiv.
4. Notbetrieb (`FallbackUpdater`/`load_config_safe`/`build_updater`): kaputte Konfiguration oder Manager stoppen den Server nicht.
5. Lauf-Sperre wird immer frei (`_finish`, `MAX_JOB_S`); `status()` fängt Abschnitte einzeln ab.
6. systemd-Herzschlag (`Heartbeat`, `WatchdogSec`, `Restart=always`) unverändert vorhanden.
7. Protokoll-Datenbank (`logstore.py`) wirft nie und blockiert nie.
8. Nur Standardbibliothek, keine neue Abhängigkeit.

**Hauptprogramm**
1. Exception-Handler für `Exception` und `sqlite3.Error` vorhanden; neue Endpunkte werfen keine ungefangenen Fehler.
2. `build()` fällt bei Datenbankproblemen auf RAM zurück; `lifespan`-Schritte einzeln geschützt.
3. Hub-Listener, Regeln und Zeitplan fangen Fehler ab; WebSocket-Senden hat ein Zeitlimit; Browser zeigt Fehler als Banner.
4. Auto-Antwort-/Sende-Schutzregeln (Budget, Mindestintervalle, Limits, Schleifenschutz) nicht abgeschwächt.
5. Push (falls vorhanden): Senden nie im Ereignispfad blockierend, Fehler werden geschluckt, ungültige Abos werden entfernt.
6. Anmeldung: neue `/api/`-Pfade bleiben geschützt (nur die Liste in `auth.py` ist offen); `/api/health` und `/api/version` bleiben offen.
7. Repeater-Verwaltung: Sperr-/Bestätigungs-/Schwärzungsregeln und das Tempolimit in `repeater.py` nicht abgeschwächt; Passwörter nie in Datenbank, Protokoll oder API-Antworten.

**Technisch vor dem Push**
- In BEIDEN Projekten alle Tests ausführen (`python -m pytest -q`); im Hauptprogramm zusätzlich den UI-Rauchtest (`tests/ui_smoke`).
- `tests/test_resilience.py` existiert in beiden Projekten und läuft grün; für jede neue Funktion gibt es einen Test, der ihren Ausfall simuliert.
- Keine Geheimnisse (Tokens, VAPID-Schlüssel, `config.toml`, `*.db`, `data/`) im Commit (`git status` und `git diff --cached` ansehen).
- Der Watchdog wird nie automatisch aus GitHub gezogen; Updates bleiben manuell.

## Repeater verwalten
- `repeater.py` (`RepeaterAdmin`) + API `/api/repeater/*` + Oberfläche `pages/repeater_admin.js` (Knoten-Seite → „Verwalten“). Node-Methoden
  `repeater_login/logout/command/neighbours/acl` in `base.py`, `sim.py` (simulierter Repeater, Passwort „sim“), `real.py`.
- Ablauf wie in der MeshCore-App: Anmelden (`send_login_sync`), dann Textbefehle (`send_cmd`, Antwort = `CONTACT_MSG_RECV` mit `txt_type 1`). Diese
  Antworten werden in `RealNode._forward` NICHT an Hub/Chat weitergegeben (sonst Direktnachrichten-Verlauf + Auto-Antwort-Risiko).
- Regeln (nicht aufweichen): GESPERRT `erase`, `set/get prv.key`; RISKANT (Server verlangt `confirm`, HTTP 409): Neustart/Ausschalten, `password`, Funkparameter,
  `set tx`, `set repeat off`, `region`, `setperm` u. a. (Liste `RISKY`); GEHEIM (nie im Verlauf/Protokoll, Antwort geschwärzt; mit `MC_DEBUG` wird das Protokoll der Bibliothek dabei kurz stummgeschaltet): `password`, `set guest.password`, `get guest.password`;
  OHNE ANTWORT (nicht warten, Sitzung endet): `reboot`, `poweroff`, `shutdown`, `clkreboot`. Höchstens 1 Funkpaket je Repeater alle 2 s auf allen Wegen (Anmelden, Befehle, Nachbarn, Zugriffsliste, Abmelden; `_radio_slot`; nach einer Anmeldung wird die Restzeit gewartet, sonst „Zu schnell“); harmlos ist nur genau `set repeat on`; keine Zeilenumbrüche,
  höchstens 150 Byte. Einstellungen nur über den Katalog `SETTINGS` mit Wertebereichen (Quelle: MeshCore-CLI-Doku docs.meshcore.io/cli_commands).
- Passwörter: nur im Arbeitsspeicher; auf Wunsch in `data/repeater_secrets.json` (Rechte 600, NICHT in der Datenbank/Sicherung). Die API gibt nie ein Passwort zurück.
  Es gibt keine Benutzeranmeldung im Programm: Wer die Oberfläche erreicht, kann mit gemerktem Passwort verwalten.
- `classify`: GESPERRT/RISKANT/GEHEIM/OHNE ANTWORT gelten ab dem WORTANFANG (`low.startswith(prefix)`, ohne Leerzeichen-Bedingung: `erase!`, `rebootnow`), Ausnahme `NOT_RISKY` (`set txdelay`);
  unsichtbare Zeichen (Kategorien Cf/Zs/Cc …, `_INVISIBLE`) → abgelehnt. Tests: `tests/test_repeater.py` (suffixes, invisible).
- Standardfunktionen am echten Repeater getestet und funktionsfähig; nicht jedes Detail geprüft, z. B.: ob Repeater-Antworten wie erwartet als `txt_type 1` ankommen, Antwortformat (`> wert`), Verhalten bei falschem Passwort
  (nur Zeitüberschreitung), ob Nachbarn-/ACL-Abfrage eine Anmeldung brauchen, Firmware-Unterschiede (manche `get`/`set` gibt es erst ab 1.14/1.15).
- Tests: `tests/test_repeater.py` (Regeln, Passwort-Ablage, API, echter Node mit Fake-Bibliothek), UI-Smoke „Verwalten“.

## USB-Überwachung
- `usb.py`: `list_ports()` (wirft nie), `candidates()` nach Punkten (Gerätename, bekannte VIDs, Text), `resolve_port(configured, ports)`.
  Mehrere Kandidaten → bester, Warnung im Log. `serial_port = "auto"` oder fester Pfad.
- `supervisor.py` (alle 2 s): Port verschwunden → sofort trennen (Vergleich über aufgelöste Pfade, `usb.port_present`, by-id-Symlinks gehen; eine gescheiterte Port-Abfrage trennt nie); Herzschlag alle 30 s (`node.alive()`; läuft gerade ein Befehl am Node, zählt das nur als Lebenszeichen, wenn in den letzten `ALIVE_BUSY_GRACE_S` = 90 s irgendein Ereignis vom Node kam (`_heard`, Abo auf alle Ereignisse) – früher galt „Sperre belegt“ immer als lebendig, mit offenem Browser-Tab wurde ein hängender Node so nie erkannt; begrenzt ist nur die Abfrage selbst), 2× keine Antwort → trennen + neu;
  nicht verbunden + Gerät da → nach 2 s Wartezeit verbinden, bei Fehlern Abstand 3→30 s; Zustand an `UsbMonitor`.
- Zustände: `ok`, `no_device`, `no_response`, `no_permission` (dialout-Hinweis). Jeder Wechsel steht in `usb_events`; Verfügbarkeit 24 h/7 Tage
  wird daraus gerechnet (Zeiten, in denen die App selbst nicht lief, zählen als „unverändert“). Meldung nach 60 s Ausfall genau einmal
  (Protokoll, Banner, Push an Geräte mit „Warnungen“) und einmal bei Rückkehr.
- RealNode nutzt für seriell `auto_reconnect=False`: Wiederverbinden (auch unter neuem Gerätenamen) macht allein der Supervisor.
- Simulator: `SimNode.unplug()/plug()/hung`, API `POST /api/sim/usb` (nur Modus sim), Knöpfe auf der Übersicht.
- Die Karte „USB-Node“ auf der Übersicht funktioniert am echten Pi (vom Entwickler bestätigt). NICHT einzeln geprüft: Abziehen/Wiederverbinden, Meldungen, echte Namen/VID/PID des Heltec V4, Verhalten der Bibliothek beim Abziehen (`is_connected`), Hotplug auf dem Pi.
  Der Herzschlag ist die Absicherung, falls die Bibliothek das Abziehen nicht meldet.
- Tests: `tests/test_usb.py` (inkl. Ausfall der Datenbank und der Port-Abfrage).

## Push (Web-Push)
- `webpush.py` = eigene RFC-8291/8292-Umsetzung (nur `cryptography`), NICHT durch `pywebpush` ersetzen (`http-ece` lässt sich mit neuem setuptools nicht installieren). Der RFC-Testvektor in `tests/test_push.py` muss grün bleiben.
- `push.py`: eigener `ThreadPoolExecutor` (`SEND_THREADS` 4), NICHT der gemeinsame Pool (den nutzt `AsyncStorage`): hängende Sendungen (DNS ohne Internet) blockierten sonst die Datenbank.
  Der Semaphor-Platz wird erst frei, wenn der Thread wirklich fertig ist (`add_done_callback`, `wait_for(shield(fut))`); kein Platz binnen `SLOT_WAIT_S` → Meldung auslassen.
  Nach `BREAKER_FAILS` (3) Netzfehlern hintereinander (Zeitlimit oder `PushError` ohne „HTTP“) `BREAKER_PAUSE_S` (5 min) keine Versuche. `peers.py` ebenso: eigener Pool, je Station
  höchstens eine laufende Abfrage (hängt sie noch, zählt das als Fehlschlag). Tests: `tests/test_robustness.py` (hanging_push, peer).
- `push.py`: Senden nur als Hintergrund-Task mit Zeitlimit; nie im Ereignispfad awaiten. Endpunkte nur `https` und mit Hostnamen (`validate_endpoint`, Schutz vor Zugriffen auf interne Ziele).
- Service Worker (`static/sw.js`) bewusst ohne Zwischenspeicher der Seiten. Tippen auf eine Meldung bei schon offener App: der Service Worker schickt `{type: 'mc-open', url}` per `postMessage`, `app.js` setzt den Hash (bei gleichem Hash `route()`); `navigate()` wird nicht mehr benutzt (iOS-Home-Bildschirm-App unzuverlässig). Rückkehr aus dem Hintergrund (`visibilitychange`): WebSocket neu verbinden (wenn > 15 s weg oder nicht offen), Status neu laden und `_resume` an die Seite schicken; die Kanal-Seite lädt dann Liste und offenen Chat nach. Vom Entwickler auf dem iPhone bestätigt.
- Getestet vom Entwickler mit iPhone, Mac und Windows-PC; Android und andere Browser nicht geprüft.
- Hub: Kommt ein `send_json` an einen Browser nicht innerhalb `WS_SEND_TIMEOUT_S` an, wird der Client entfernt UND die Verbindung geschlossen (`Hub._close`, 1011), sonst bliebe der Tab „taub“.
  `runtime.on_event`: `add_message` (direkt/Kanal) ist abgefangen – Auto-Antwort, Push, Raum laufen bei Datenbankfehlern weiter (`msg_id` None).
- Ungelesen: Marker je Kanal in `settings` (`channel_read_<idx>`, Grundlinie `unread_baseline`; `POST /api/channels/{idx}/read` liefert die ALTE Markierung `read_upto`, der Marker springt nie zurück). `channels.js` setzt „Ungelesene Nachrichten“ vor die erste eingehende Nachricht mit `id > read_upto` (`unreadMark`, bleibt bis zum Schließen); der Zähler der Kanalliste ist nur Ersatz bei Fehlern (konnte veraltet sein → Sprung viel zu weit nach oben). Tests: `test_read_returns_previous_marker_for_the_unread_separator`, UI-Smoke „Server-Markierung“; „einmal“-Push ist zustandslos über die Nachrichten-ID: gemeldet wird, wenn die Nachricht die älteste ungelesene des Kanals ist (`channel_first_unread`). Beim Löschen/Neuschlüsseln eines Kanals Marker löschen.

## Eigener Node / Einstellungen („Mein Node“)
- `nodeconfig.py` (`FIELDS`, `validate`, `NodeConfig`) + API `/api/node/spec|settings|time-sync|reboot` + Seite `pages/mynode.js`. Node-Methoden
  `node_settings/node_apply/node_set_time/node_reboot` in `base.py`, `sim.py`, `real.py`. Oberfläche ist aus `/api/node/spec` gebaut; der Server prüft alles selbst.
- Riskant (HTTP 409 ohne `confirm`): Funkparameter (nur alle vier zusammen schreiben), Pfad-Kennung, Standort in Adverts „immer senden“, Neustart.
  Unveränderte Werte → 422 „Nichts zu ändern“. Breite/Länge nur zusammen (werden immer als Paar geschrieben, `node_apply` lehnt eine einzelne Koordinate ab). Sendeleistung ≤ `max_tx_power` des Nodes.
- `mynode.js`: Auswahlwert ohne passende Option → eigene Option „unbekannt (Wert) – nicht ändern“ (Wert leer); leere Werte zählen beim Speichern nie als Änderung
  (sonst schrieb der Browser still die erste Option, z. B. falsche Bandbreite). `run()` sperrt/entsperrt nur Knöpfe der eigenen Seite (`page`) und nur solange `alive`. UI-Smoke „unbekannte Bandbreite“.
- Bewusst NICHT enthalten: Privatschlüssel export/import, Werkseinstellung. Keine Funk-Voreinstellungen eingebaut (nicht verifiziert).
- Standardfunktionen am echten Node getestet und funktionsfähig; nicht jedes Detail geprüft, z. B.: alle Schreibbefehle, Reihenfolge, Wirkung von `set_other_params_from_infos`, `path_hash_mode` (Firmware ab 1.14),
  Bedeutung der Werte 0/1/2 bei Telemetrie/Standort-Weitergabe (aus Erinnerung, Zahlen werden deshalb immer mit angezeigt), `set_time`.
- Tests: `tests/test_nodeconfig.py`, UI-Smoke „Mein Node“.

## Anmeldung (optional)
- `access_password` (config.toml `[server]` oder `MC_ACCESS_PASSWORD`); leer = aus. `auth.py`: Cookie `mc_session` (HttpOnly, SameSite=Strict, bei https Secure),
  30 Tage, HMAC aus dem Passwort. Middleware in `main.py` schützt alles unter `/api/` und `/ws`. OFFEN bleiben: `/api/health`, `/api/version` (Watchdog-Prüfung!),
  `/api/auth/*`, `/api/peer/summary` (eigenes Token), Seitenhülle und `/static`. `/docs`, `/redoc`, `/openapi.json` sind mit Passwort geschützt (`API_DOCS`).
  Push und Stationsabfrage folgen keiner Weiterleitung (`webpush.no_redirect_open`). Passwortdatei: `os.fchmod(fd, 0o600)` vor dem Schreiben. Nach 5 Fehlversuchen in 60 s Sperre für 60 s (global).
- Rechnernamen- und Fremdseiten-Schutz (`hostguard.py`, Middleware + `/ws` in `main.py`, gilt AUCH ohne Passwort, nicht aufweichen): Host nur IP, localhost, `*.ts.net`,
  eigener Rechnername (auch `.local`) oder `allowed_hosts` (sonst 403, gegen DNS-Rebinding); `/api/…` und `/ws`: `Sec-Fetch-Site: cross-site` → 403/1008, `same-origin`/`none` → ok,
  sonst entscheidet `Origin` STRENG (`origin_ok`): gleich Host bzw. X-Forwarded-Host, eigener Name (Rechnername, `.local`, `<name>.<tailnet>.ts.net`) oder `allowed_hosts` –
  NICHT jede IP/localhost/*.ts.net (fremde Seiten unter IP oder Tailscale Funnel). Chromium schickt bei WebSockets KEIN Sec-Fetch-Site, nur Origin (im Browser geprüft).
  In Tests erlaubt `conftest.py` die Namen „test“/„testserver“; echte Prüfung: `tests/test_hostguard.py`.
- Nicht aufweichen: neue `/api/`-Pfade sind automatisch geschützt; nichts mit Daten auf die offene Liste setzen. Passwort steht im Klartext in config.toml.

## Weitere Seiten (alle im Simulator getestet, nicht an Hardware)
- **Pfad-Kennung** (0/1/2 = 1/2/3 Byte je Hop): `node/base.py` `hop_bytes`, `split_path`, `trace_hash_bytes`. Kontakte: `out_path_hash_mode` der Bibliothek →
  `path_hash_mode` → `normalize_contact` (`path`, `path_hash_size`); Nachrichten: `path_hash_mode` des Pakets; Trace: Pfad-Kennung des eigenen Nodes
  (`RealNode._path_hash_mode` aus device_query, Modus 2 → 4 Byte, flags 2). Nie mehr fest 1 Byte annehmen. Simulator: Bob mit 2-Byte-Pfad.
  An Hardware NICHT getestet. Tests: `tests/test_small_fixes.py`, `tests/test_real_channels.py` (trace).
- **Netz** (`network.py`, `/api/network`, `pages/network.js`): Pfade aus `contact.path` (Kennungslänge je Kontakt, `path_hash_size`), Nachbarlisten aus Tabelle `neighbour_reports`
  (wird beim Abruf in „Verwalten“ gefüllt; nur die neueste Meldung je Repeater). Mehrdeutige Kennungen werden als „?“ gezeigt.
- **Sensoren** (`sensor_readings`, `/api/sensors/*`, `pages/sensors.js`; **bisher gar nicht an echter Hardware getestet**, auch die Telemetrie-Abfrage nicht): alle Zahlenwerte aus der Telemetrie (Cayenne LPP), Listen wie GPS werden übersprungen.
  Die Akku-Regel mit „Temperatur“ speichert mit.
- **Funk** (`stats.py`, `radio_stats`, `/api/stats`): `node_stats()` (meshcore `get_stats_core/radio/packets`, nur lokal, keine Funkzeit), alle 30 min gesichert.
  Sendezeit-Anteil ist Richtgröße; Zähler setzen sich beim Neustart zurück (Differenz < 0 wird übersprungen).
- **Sicherung** (`configbackup.py`, `/api/backup/*`, Abschnitt in „Mein Node“): Node-Einstellungen, Kanäle (Schlüssel nur auf Wunsch), Regeln; KEIN Privatschlüssel, keine
  Passwörter. Wiederherstellen = Trockenlauf + alles prüfen (Kanalplätze gegen `max_channels` des Nodes, doppelte Plätze abgelehnt, falsche Typen → 422), dann schreiben in der Reihenfolge Kanäle, Regeln, Node-Einstellungen (je Kanal Verlauf, `channel_view_`/`channel_read_` gelöscht); riskante Werte brauchen weiter `confirm`; belegte Kanalplätze nur mit `overwrite_channels`.
- **Suche/Stichworte** (`/api/search`, `/api/push/keywords`, `pages/search.js`): LIKE-Suche (Teilwort), Stichwort-Push an ALLE angemeldeten Geräte, auch bei stummem Kanal.
- **Advert-Zeitplan**: Regelart `advert` (mindestens 60 min, netzweit mindestens 360 min), zählt zum Sendebudget.
- **Raum-Bot** (`room.py`, Tabellen `room_posts/room_members`, `/api/room/*`, `pages/room.js`): Befehle `/p /neu /verlauf /anmelden /abmelden /hilfe` per Direktnachricht von
  bekannten Clients; Antworten mit `[auto] `-Präfix (Schleifenschutz auch zwischen zwei Pis), über das gemeinsame Sendebudget; 100 Zeichen je Beitrag (`POST_MAX`, früher 140: Beitrag + „[auto] Raum [12 14:32] @[Name] “ passte nicht in 150), 20 s Pause und 20 Beiträge/h je Mitglied,
  höchstens 10 Angemeldete; alle 20 s je Angemeldetem höchstens 1 Beitrag, insgesamt höchstens 3 Sendungen je Durchlauf (`DELIVER_PER_CYCLE`); nach unbestätigter Zustellung Wartezeit je Mitglied 60 s, verdoppelt bis 30 min, bei Erfolg zurückgesetzt; ein unerreichbares Mitglied hält die anderen nicht auf. 20 s Pause gilt für JEDEN Befehl (in der Pause still ignoriert); bei pausierter Automatik schweigt auch der Raum. Eine bestätigte Nachricht bucht `CONFIRM_TX_MAX` = 3 Plätze im Sendebudget (bis zu 3 Funkversuche). Erscheint in der MeshCore-App als normaler Kontakt, nicht als Room. Nicht an Hardware getestet.
  Zustellformat (`Room.render`): `[auto] <Titel> [Nr Zeit] @[Autor] Text` – Autor als Erwähnung (`mention_tag`, Klammern im Namen entfernt; bei `author_key == 'web'` „Web: Text“), Zeit = wann der Beitrag
  beim Raum ankam (`room_posts.ts`, Ortszeit), nur Uhrzeit wenn heute, sonst mit Datum. Länge: nur wenn die ganze Zeile (mit `[auto] `) ≤ `LINE_MAX` = 150 (Zähler „0/150“ der App), erst ohne Datum, dann ohne Zeit;
  Gemessen wird in BYTES (UTF-8, Umlaute zählen 2, Emojis 4). Passt es auch ohne Zeit nicht (ältere, längere Beiträge, lange Namen), wird der Text mit „...“ gekürzt (`cut_to_bytes`) und `_line` kappt jede
  Raum-Nachricht zusätzlich auf `LINE_MAX`: der Node bekommt nie eine zu lange Nachricht. Tests: `tests/test_room.py` (feste 150 und 100 geprüft).
  Zustellung mit Bestätigung: Raum-Nachrichten gehen mit `send_message(..., confirm=True)` (RealNode: `send_msg_with_retry`, bis 3 Versuche, ab dem 3. per Flut, wartet auf die Empfangsbestätigung). Früher galt „vom Node angenommen“ als zugestellt: ging das Funkpaket verloren, war der Beitrag trotzdem „gesehen“ und fehlte für immer (auch bei `/neu`). Ohne Bestätigung bleibt er ungesehen; die automatische Zustellung versucht es höchstens `MAX_TRIES` (3) Durchläufe lang, dann wird er übersprungen. Tests: `tests/test_room.py` (`no_ack` im Simulator). NICHT an Hardware getestet.
  Je Mitglied gibt es eine `asyncio.Lock` (`Room._lock`): `/neu`, `/verlauf` und `deliver_once` laufen nie gleichzeitig (sonst bekam ein Mitglied denselben Beitrag doppelt, weil beide denselben `last_seen_id` lasen; mit Bestätigung dauert ein Senden länger, das Zeitfenster war größer). Test `test_neu_and_automatic_delivery_never_send_the_same_post_twice`.
  Doppelte Zustellung ist GEWOLLT: `/verlauf` und `/neu` sind ausdrückliche Anfragen und senden unabhängig von der automatischen Zustellung an Angemeldete; ein angemeldetes Mitglied bekommt Beiträge
  daher zweimal, wenn es danach `/verlauf` schickt (Test `test_verlauf_resends_...`). Danach (last_seen_id steht auf dem neuesten) kommt nichts mehr automatisch.
- **Stationen** (`peers.py`, `/api/peer/summary`, `/api/peers`, Karte auf der Übersicht): `peer_token` dieser Station, `peers = "Name|https://adresse|Token-der-anderen; …"`. Abfrage jede Minute,
  Meldung nach 3 Fehlschlägen (einmal) und bei Rückkehr. Endpunkt antwortet ohne gültiges Token mit 404. Es fließt nur der Kurzstatus.
- **Sendeprotokoll** (`sendlog.py`, `/api/sends`): `SendLog.instrument(node)` umhüllt in `runtime.build()` die sendenden Methoden des Nodes (Liste `TX_KINDS`, echter Node UND Simulator);
  Ringpuffer 300 Einträge (Zeit, Art), Arbeitsspeicher, nie Inhalte. Neue Node-Methode, die Funk auslöst → in `TX_KINDS` eintragen. `/api/sends` steht in `OPEN_API` (auth.py) und prüft selbst:
  Sitzung ODER `control_token` wie der Wartungsmodus (`_control_auth` in `api.py`); der Watchdog fragt nur nach einem Einbruch. Tests: `tests/test_sendlog.py`.
- Navigation: auf schmalen Bildschirmen eine wischbare Zeile (`style.css`, Media Query 900 px).
- Kanal-Chat am Handy: Wisch nach rechts = zurück zur Kanalliste (`pages/channels.js`), nur in der einspaltigen Ansicht (`max-width: 640px`, wie in `style.css`), nicht wenn er im Menü (`#nav`, `.nav-backdrop`) beginnt; sonst hat Wischen nirgends eine Funktion. Ein offener Chat legt einen Verlaufseintrag an (`mcChat`), Browser-Zurück/iOS-Randwisch schließt den Chat; ein verwaister Eintrag wird beim Öffnen der Seite ersetzt. Doppeltippen erwähnt (eigene Erkennung, weil iOS `dblclick` nicht zuverlässig liefert). Entwurf und Erwähnung bleiben beim Kanalwechsel nicht hängen. Offener Chat am Handy: Es scrollt die ganze Seite (`#scrollArea`), nicht ein innerer Bereich; Kopf (`.chat-head`) und Eingabe (`.chat-compose`) sind `sticky`. `scroller()` in `channels.js` wählt den Bereich. Neue Nachrichten springen nur nach unten, wenn man schon unten war (`reloadMessages(0, 'follow')`).
- Doppelklick-Schutz: `locked(btn, fn)` in `lib/util.js` (Regel speichern, Pause, Kontakt hinzufügen), Wartungsknöpfe mit `maintBusy`, Regel-Schalter während der Anfrage gesperrt.
  Zähler unter Eingabefeldern: `countLabel` (Zeichen, bei Umlauten/Emojis zusätzlich Byte, Warnung über der Grenze); die genaue Byte-Grenze der Firmware ist NICHT an Hardware geprüft.
- `app.js` `route()`: jede Seite bekommt einen eigenen Container (`display: contents`), späte Schreibzugriffe einer verlassenen Seite landen im
  abgehängten Container (auch Handler auf `root`, z. B. Raum). WebSocket: Neuverbindung 2 s verdoppelt bis 30 s; nach Wiederverbinden `_resume`,
  Status und Glocke neu. Kanalschlüssel-Dialoge leeren sich beim Ereignis `close` (auch Esc).
- Kanal-Chat nach Rückkehr aus dem Hintergrund (beobachtet: Kanaltitel, darunter „Links einen Kanal wählen“, nichts lädt): `closeChat` setzt `msgsFor = null`,
  `openChat` zeigt sofort „lade …“; `reloadMessages` verwirft eine ältere Antwort nur, wenn schon eine neuere GEZEIGT wurde (`msgShown`), nicht wenn eine neuere
  noch läuft; scheitert das Laden und ist noch nichts zu sehen → Hinweis mit „Erneut laden“ (`showLoadError`). Verlauf über `getQuick` (`lib/api.js`: 8 s Zeitlimit,
  ein neuer Versuch; NUR für reine Datenbank-Abfragen, nie für Funk). Randwisch: Wischer, die in den linken `EDGE_PX` (30 px) beginnen, gehören dem Browser
  (sonst zwei Schritte zurück → leere Seite außerhalb der App); `closeChat` ruft `history.back()` höchstens einmal (`backPending`). UI-Smoke „Randwisch“, „Erneut laden“.
- Home-Bildschirm-App (iOS, `STANDALONE` in `app.js`): vor der ersten App-Seite liegt oft about:blank (weiße Seite ohne Rückweg).
  Erster eigener Verlaufseintrag = Boden (`{mcRoot}`), darüber `{mcApp}`; erreicht `popstate` den Boden, wird sofort wieder ein Eintrag gelegt.
  Nur installiert, nicht im Browser-Tab. ACHTUNG: in `app.js` ist `history` die Ereignisliste, immer `window.history` schreiben. Im Browser (Chromium,
  `navigator.standalone` vorgetäuscht) geprüft: 5× Zurück bleibt in der App; im Tab führt das 2. Zurück wie gewohnt zur vorigen Seite.
- Bewusst so: Fehlermeldungen im Browser zeigen weiter die Details (`Interner Fehler: Typ: Text`), weil sie bei der Fehlersuche helfen.
- Seiten verwerfen veraltete Antworten über einen Ladezähler und `alive` (z. B. Akku, Sensoren, Karte, Kanal-/Kontakt-Chat); neue Ladefunktionen genauso bauen. Senden per Enter ist gegen Doppelsenden gesperrt, solange gesendet wird.

## Wartungsmodus (App)
- `maintenance.py` (`Maintenance`), API `/api/maintenance` (GET/POST {minutes}/DELETE), Karte „Wartung“ auf der Übersicht, Leiste `#maintBar` auf allen Seiten.
  Der Supervisor trennt den Node (Anschluss frei), verbindet nicht neu, meldet `usb.MAINTENANCE` (kein Alarm, zählt nicht als Ausfall) und beendet die Wartung nach Ablauf.
  Senden, Raum, Automatik, Statistik ruhen, weil sie `node.connected` voraussetzen. Zeitpunkt in `settings` (`maintenance_until`), überlebt Neustart, höchstens 24 h.
- Zugriff: Sitzung ODER Header `X-Control-Token` = `control_token` (für den Watchdog; leer = nie gültig). Das Token gilt nur für diesen Pfad. `/api/version` zeigt `maintenance_until`.
- Raum-Befehle (`/…`) werden bei aktivem Raum nie von Auto-Antwort-Regeln beantwortet (`runtime.py`, `room_cmd`).
