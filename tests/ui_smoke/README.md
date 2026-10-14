# UI-Rauchtest

Lädt die Seitenmodule (Übersicht, Kontakte, Knoten, Karte, Automatik) in jsdom und bedient sie
gegen einen laufenden Server im Simulator-Modus. Braucht Node.js, aber nur zum Testen, nicht
auf dem Pi.

```bash
# 1) Server mit frischer Test-Datenbank im Simulator starten (eigenes Terminal)
MC_MODE=sim MC_PORT=8767 MC_DB_PATH=/tmp/ui-test.db MC_MANUAL_MIN_GAP_S=0 MC_MANUAL_MAX_PER_MIN=10000 python -m app.main   # Tempolimit aus: der Test klickt schneller als 2 s

# 2) Test ausführen (Datenbank vor jedem Lauf löschen, sonst stimmen die Zählungen nicht)
cd tests/ui_smoke && npm install && npm test
```

`BASE=http://127.0.0.1:8767` ist der Standard; per Umgebungsvariable `BASE` änderbar.
jsdom kann keine Layouts rendern: geprüft wird Inhalt und Verhalten, nicht das Aussehen.

Mit `MC_BATTERY_POLL_GAP_S=1` beim Starten des Servers (Schritt 1) prüft der Test zusätzlich, dass bei „Jetzt abfragen“ auf der
Akku-Seite der zweite Repeater nach der Pause abgefragt wird; mit der Standardpause (20 s) entfallen diese beiden Prüfungen.

Wettläufe (verspätete Antworten, schnelles Umschalten) prüft der Test mit `fetchHook` in `smoke.mjs`: einzelne Anfragen werden verzögert
(`late(ms)`) oder durch eine feste Antwort ersetzt (`fake(daten, ms)`). Die Seiten-Wischgeste wird mit einem eingesetzten `window.matchMedia`
für die einspaltige (≤ 640 px) und die zweispaltige Ansicht geprüft. Was jsdom nicht kann (echtes `setPointerCapture` auf der Netz-Seite),
wurde zusätzlich in Chromium (Playwright) geprüft.
