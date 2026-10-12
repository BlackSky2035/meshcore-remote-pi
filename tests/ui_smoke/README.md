# UI-Rauchtest

Lädt die Seitenmodule (Übersicht, Kontakte, Knoten, Karte, Automatik) in jsdom und bedient sie
gegen einen laufenden Server im Simulator-Modus. Braucht Node.js, aber nur zum Testen, nicht
auf dem Pi.

```bash
# 1) Server mit frischer Test-Datenbank im Simulator starten (eigenes Terminal)
MC_MODE=sim MC_PORT=8767 MC_DB_PATH=/tmp/ui-test.db python -m app.main

# 2) Test ausführen (Datenbank vor jedem Lauf löschen, sonst stimmen die Zählungen nicht)
cd tests/ui_smoke && npm install && npm test
```

`BASE=http://127.0.0.1:8767` ist der Standard; per Umgebungsvariable `BASE` änderbar.
jsdom kann keine Layouts rendern: geprüft wird Inhalt und Verhalten, nicht das Aussehen.

Mit `MC_BATTERY_POLL_GAP_S=1` beim Starten des Servers (Schritt 1) prüft der Test zusätzlich, dass bei „Jetzt abfragen“ auf der
Akku-Seite der zweite Repeater nach der Pause abgefragt wird; mit der Standardpause (20 s) entfallen diese beiden Prüfungen.
