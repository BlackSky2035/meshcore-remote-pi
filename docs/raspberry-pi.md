# Installation auf dem Raspberry Pi

Schritt für Schritt, vom leeren Pi bis zum laufenden Dauerbetrieb mit Autostart, Updates und Überwachung.

> **Ehrlicher Stand:** Die Programme laufen auf einem Raspberry Pi 3 Model B mit einem Heltec V4 (USB). Diese Anleitung wurde aber
> **nicht** auf einem frisch eingerichteten Pi von Anfang bis Ende nachvollzogen. Die Befehle sind Standard unter Raspberry Pi OS
> (Bookworm). Wenn ein Schritt bei dir anders aussieht, bitte ein Issue aufmachen.

## Vorher lesen

- Das Projekt ist **KI-generiert und nicht überprüft**, **inoffiziell** und **ohne Gewähr** (siehe README und LICENSE). Du betreibst es auf eigene Verantwortung.
- Das Programm **sendet über Funk**. Du bist selbst dafür verantwortlich, dass das mit den Funkregeln deines Landes vereinbar ist (z. B. Sendezeit im 868-MHz-Band). Lockere die Schutzregeln der Automatik nicht.
- **Wer auf den Branch `main` der Repos pushen kann, kann Code auf deinem Pi ausführen** (der Watchdog zieht Updates von dort). Klone nur Repos, denen du vertraust, und schütze dein GitHub-Konto mit Zwei-Faktor-Anmeldung. Das gilt auch für fremde Forks.
- Vieles ist nur im Simulator getestet, **Sensoren gar nicht** (Überblick: README, Abschnitt „Nicht an echter Hardware getestet“).

## Was du brauchst

- Raspberry Pi 3 B oder neuer (Pi Zero 2 W geht, braucht aber einen OTG-Adapter und hat nur WLAN und 512 MB RAM)
- microSD-Karte, am besten „High Endurance“ (ab 16 GB), und ein gutes **Netzteil mit mindestens 2,5 A** mit kurzem, dickem Kabel
- einen MeshCore-Node (z. B. Heltec V4), der die **Companion-Firmware für USB-Seriell** hat
- einen Rechner zum Einrichten (Windows, Mac oder Linux) mit SSH

## 1. Betriebssystem auf die SD-Karte

1. Auf dem Rechner den **Raspberry Pi Imager** installieren und starten.
2. Gerät wählen, als System **Raspberry Pi OS Lite (64-bit)** (unter „Raspberry Pi OS (other)“), die SD-Karte wählen.
3. Auf „Einstellungen bearbeiten“ (Zahnrad) klicken und eintragen:
   - **Hostname**, z. B. `meshcore` (der Pi ist dann als `meshcore.local` erreichbar)
   - **Benutzername und Passwort** (nicht `pi` mit Standardpasswort)
   - **WLAN** (Name, Passwort, Land `DE`), falls du kein LAN-Kabel nutzt
   - **Zeitzone** und Tastaturlayout
   - unter „Dienste“ **SSH aktivieren** (am besten mit öffentlichem Schlüssel)
4. Schreiben, Karte in den Pi, Strom anschließen, ein bis zwei Minuten warten.

## 2. Anmelden und System aktualisieren

Vom Rechner aus (Benutzername und Hostname wie oben):

```bash
ssh <benutzer>@meshcore.local
```

Auf dem Pi:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git python3-venv
python3 --version        # muss 3.11 oder neuer sein (Bookworm liefert 3.11)
```

Den Benutzer in die Gruppe für USB-Seriell aufnehmen (sonst darf das Programm den Node nicht öffnen), danach **einmal abmelden und neu anmelden**:

```bash
sudo usermod -aG dialout $USER
exit
```
Dann wieder per `ssh <benutzer>@meshcore.local` verbinden.

## 3. Beide Programme herunterladen

Hauptprogramm und Watchdog liegen **nebeneinander im Home-Verzeichnis**. Achte auf die Ordnernamen am Ende der Befehle (so erwarten es die Dienst-Dateien, auch wenn die Repos anders heißen).
Hast du einen **Fork** oder eigene Kopien, ersetze die Adressen entsprechend:

```bash
cd ~
git clone https://github.com/BlackSky2035/meshcore-remote-pi.git meshcore-remote
git clone https://github.com/BlackSky2035/meshcore-watchdog-pi.git meshcore-watchdog
```

## 4. Hauptprogramm einrichten

```bash
cd ~/meshcore-remote
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml
nano config.toml
```

In `config.toml` mindestens ändern:

```toml
[server]
access_password = "ein-langes-geheimes-passwort"   # empfohlen, sobald der Pi nicht nur per SSH-Tunnel erreichbar ist

[node]
mode = "serial"
serial_port = "auto"        # oder fest, z. B. "/dev/ttyACM0"
```

`host` bleibt `127.0.0.1` (die Seite ist dann nur auf dem Pi selbst erreichbar, der Zugriff läuft über SSH-Tunnel oder Tailscale, siehe unten).
Speichern in nano: `Strg+O`, Enter, `Strg+X`. **`config.toml` ist geheim, sie kommt nie ins Git.** Das Passwort steht darin im Klartext, deshalb Zugriff einschränken: `chmod 600 config.toml`.

### Node anschließen und prüfen

Node per USB an den Pi stecken, dann:

```bash
ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null     # es muss ein Gerät auftauchen
dmesg | tail -n 15                            # zeigt, wie der Pi das Gerät erkannt hat
```

Erscheint nichts: anderes USB-Kabel probieren (manche Kabel laden nur und übertragen keine Daten).

### Probelauf von Hand (nur **vor** Schritt 7, solange der Dienst noch nicht läuft, sonst ist Port 8000 belegt)

```bash
cd ~/meshcore-remote
venv/bin/python -m app.main
```

In einer zweiten SSH-Sitzung: `curl http://127.0.0.1:8000/api/version` sollte eine kurze Antwort mit der Versionsnummer liefern.
Im ersten Fenster stehen Meldungen, ob der Node gefunden und verbunden wurde. Mit `Strg+C` beenden.

## 5. Von deinem Rechner aus die Oberfläche öffnen (SSH-Tunnel)

Auf dem **Rechner** (nicht auf dem Pi), Fenster offen lassen:

```bash
ssh -L 8000:127.0.0.1:8000 -L 8100:127.0.0.1:8100 <benutzer>@meshcore.local
```

Im Browser: `http://localhost:8000` (Hauptprogramm) und `http://localhost:8100` (Watchdog, ab Schritt 6).
Für dauerhaften Zugriff von unterwegs siehe „Tailscale“ weiter unten.

## 6. Watchdog einrichten

Der Watchdog startet das Hauptprogramm, spielt Updates ein und rollt bei Fehlern zurück. Er braucht nur Python (kein venv).

```bash
cd ~/meshcore-watchdog
cp config.example.toml config.toml
sed -i -e "s|^repo_dir = .*|repo_dir = \"$HOME/meshcore-remote\"|" \
       -e 's|^manager = .*|manager = "systemd"|' \
       -e 's|^systemctl_command = .*|systemctl_command = "sudo systemctl"|' config.toml
grep -nE '^(repo_dir|manager|systemctl_command)' config.toml     # Kontrolle der drei Zeilen
chmod 600 config.toml                                              # enthält ggf. ein Token
```

## 7. Autostart mit systemd

Die Dienst-Dateien enthalten als Beispiel den Benutzer `pi`. Die folgenden Befehle setzen **deinen** Benutzer und Pfad automatisch ein:

```bash
cd ~/meshcore-watchdog/deploy
for f in meshcore-remote.service meshcore-watchdog.service; do
  sed -e "s|/home/pi|$HOME|g" -e "s|^User=pi|User=$USER|" -e "s|^Group=pi|Group=$(id -gn)|" "$f" | sudo tee "/etc/systemd/system/$f" > /dev/null
done

# Erlaubt dem Watchdog, genau das Hauptprogramm ohne Passwort zu starten und zu stoppen
sed "s/^pi /$USER /" sudoers.example | sudo tee /etc/sudoers.d/meshcore-watchdog > /dev/null
sudo chmod 440 /etc/sudoers.d/meshcore-watchdog
sudo visudo -cf /etc/sudoers.d/meshcore-watchdog     # muss "parsed OK" melden

sudo systemctl daemon-reload
sudo systemctl enable --now meshcore-remote meshcore-watchdog
```

Prüfen:

```bash
systemctl is-active meshcore-remote meshcore-watchdog      # zweimal "active"
journalctl -u meshcore-remote -n 50 --no-pager             # Meldungen des Hauptprogramms
curl http://127.0.0.1:8100/api/ping                        # Watchdog antwortet
```

Danach im Browser (über den Tunnel aus Schritt 5) `http://localhost:8000` öffnen. Nach einem Neustart des Pi (`sudo reboot`) läuft alles von selbst wieder an.

## 7a. Zugriff von außen mit Tailscale (optional, empfohlen)

Anleitung mit allen Befehlen: Abschnitt „Zugriff über Tailscale“ in der [README](../README.md). Kurz: Tailscale installieren, `tailscale serve` vor die
beiden Ports schalten, in der `config.toml` des Hauptprogramms `watchdog_port = 8443` setzen. Die Seiten sind dann nur in deinem eigenen Tailnet erreichbar.
**Die Seite niemals ohne Schutz ins offene Internet stellen.**

## 7b. Absichern (wichtig, bitte vor dem Dauerbetrieb)

Beide Programme lauschen standardmäßig nur auf `127.0.0.1` und sind damit aus dem Heimnetz nicht erreichbar. **Ohne Passwort ist die Oberfläche aber völlig ungeschützt**, sobald du sie freigibst
(`host = "0.0.0.0"`, `tailscale serve`, Portweiterleitung): Jeder, der sie erreicht, kann über Funk senden, Einstellungen ändern und über den Watchdog das Programm stoppen oder aktualisieren.
Mindestens:

```bash
ss -tlnp | grep -E ':8000|:8100'          # muss 127.0.0.1 zeigen, nicht 0.0.0.0 oder *
nano ~/meshcore-remote/config.toml        # [server] access_password = "langes-einmaliges-passwort"
nano ~/meshcore-watchdog/config.toml      # [server] token = "langes-einmaliges-token"
chmod 600 ~/meshcore-remote/config.toml ~/meshcore-watchdog/config.toml
sudo systemctl restart meshcore-remote meshcore-watchdog
```

Die vollständige Checkliste (HTTPS, Tailscale-ACLs, SSH-Schlüssel, Firewall, Router, GitHub-2FA) steht in der [README](../README.md), Abschnitt „Sicherheit“.

## 8. Für den Dauerbetrieb härten (empfohlen)

Im Watchdog-Repo steht unter `deploy/pi-haertung.md`: Hardware-Watchdog (Pi startet bei Systemhänger selbst neu), weniger Schreibzugriffe auf die SD-Karte,
Uhrzeit ohne Echtzeituhr, Datenbanksicherung auf USB-Stick und Hinweise zur Stromversorgung. Besonders die Stromversorgung entscheidet über die Stabilität:
Der Watchdog zeigt Unterspannung an. Tritt sie auf, helfen ein besseres Netzteil und ein kürzeres, dickeres Kabel.

## Updates einspielen

- **Hauptprogramm:** in der Watchdog-Oberfläche (Port 8100) „Update prüfen“, dann „Aktualisieren“. Er holt neue Commits, installiert geänderte
  Abhängigkeiten, startet neu und rollt bei einem Fehler selbst zurück.
- **Watchdog selbst:** aktualisiert sich bewusst nicht automatisch (damit ein fehlerhaftes Update ihn nicht mitreißt). Per SSH, im Ordner des Watchdogs:
  ```bash
  cd ~/meshcore-watchdog && git status --short && git pull --ff-only origin main && sudo systemctl restart meshcore-watchdog && sleep 3 && systemctl is-active meshcore-watchdog
  ```
  Am Ende muss `active` stehen. Einzelheiten und Fehlersuche im README des Watchdogs (Abschnitt „Den Watchdog selbst aktualisieren“).

## Fehlersuche

| Problem | Ursache und Abhilfe |
|---|---|
| Seite lädt nicht | Tunnel offen? Läuft der Dienst (`systemctl is-active meshcore-remote`)? Meldungen mit `journalctl -u meshcore-remote -n 50 --no-pager` |
| Kein Node gefunden / „keine Berechtigung“ | Benutzer in Gruppe `dialout`? (`groups`), danach neu anmelden bzw. Dienst neu starten. Anderes USB-Kabel. Stimmt `serial_port`? |
| Node „reagiert nicht“ | Node vom Strom trennen und neu stecken; läuft auf dem Node die Companion-Firmware? Läuft die MeshCore-App gleichzeitig am Kabel? (Nur ein Programm darf den Anschluss halten, dafür gibt es den **Wartungsmodus**.) |
| Update schlägt fehl | Im Watchdog das Protokoll ansehen; `config.toml` und `data/` sind vom Git ausgenommen, andere lokale Änderungen im Klon stoppen das Update |
| Unterspannung in der Watchdog-Karte „System“ | Netzteil mit 2,5 A+ und kurzes dickes Kabel, USB-Geräte prüfen, keine Hubs ohne eigene Versorgung. Ausführlich im README des Watchdogs (Abschnitt „Unterspannung am Raspberry Pi“) |
| Uhrzeit falsch | Der Pi hat keine Echtzeituhr und stellt sie per Netz. Ohne Netz beim Start stimmt sie nicht. Siehe `pi-haertung.md` |

## Mehrere Stationen

Zwei oder mehr Pis können sich gegenseitig überwachen (`peer_token` und `peers` in der `config.toml`, Einzelheiten in der README, Abschnitt „Weitere Funktionen“).
**Dieses Zusammenspiel wurde noch nicht mit mehreren echten Geräten getestet.**

## Deinstallation

```bash
sudo tailscale serve reset                          # nur falls du Tailscale serve eingerichtet hattest
sudo systemctl disable --now meshcore-remote meshcore-watchdog
sudo rm /etc/systemd/system/meshcore-remote.service /etc/systemd/system/meshcore-watchdog.service /etc/sudoers.d/meshcore-watchdog
sudo systemctl daemon-reload
rm -rf ~/meshcore-remote ~/meshcore-watchdog       # löscht auch die Datenbank unter data/
```
