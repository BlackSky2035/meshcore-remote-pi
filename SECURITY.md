# Sicherheit

**Bitte Sicherheitslücken nicht als öffentliches Issue melden.** Nutze stattdessen auf GitHub unter dem Reiter *Security* den Punkt
**„Report a vulnerability“** (privates Melden). So kann das Problem behoben werden, bevor es öffentlich bekannt wird.

Bitte nenne dabei:
- was betroffen ist (Seite, Programmteil, Version aus dem Seitenkopf bzw. `GET /api/version`),
- wie man das Problem nachstellt,
- was ein Angreifer damit erreichen könnte.

## Wichtig zu wissen

- Dieses Projekt ist **KI-generiert** (Claude von Anthropic) und vom Veröffentlichenden **nicht überprüft**. Es gibt keine Garantie auf Antwortzeit oder Behebung,
  siehe auch den Haftungsausschluss in der [README](README.md) und die [LICENSE](LICENSE).
- Das Programm ist **nicht für das offene Internet gedacht**, sondern für `127.0.0.1`, das lokale Netz oder ein privates Tailscale-Netz.
  Es hat nur ein optionales gemeinsames Passwort, keine Benutzerkonten und keine Rechteverwaltung. Wer die Oberfläche erreicht und das Passwort kennt,
  darf alles, auch Repeater verwalten und Nachrichten über Funk senden.
- Die Datei `config.toml` (Passwörter, Tokens) und der Ordner `data/` (Datenbank, gemerkte Repeater-Passwörter) sind geheim und gehören nie ins Git.
- Wer auf den Branch `main` des Repos pushen kann, kann über den Watchdog Code auf den Geräten ausführen lassen: GitHub-Konto mit Zwei-Faktor-Anmeldung schützen.

Unterstützt wird nur der jeweils neueste Stand von `main`.
