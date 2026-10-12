// Hilfe-Tipps: erklärt beim Darüberfahren die Bedienelemente. Standardmäßig aus, Wahl wird gemerkt.
// Texte stehen hier zentral (Selektor -> Text); gesetzt wird data-tip, sobald ein Element auftaucht.
const TIPS = [
  // Navigation und Kopfzeile
  ['#dot', 'Grün: Das Programm ist mit dem MeshCore-Node verbunden. Rot: keine Verbindung.'],
  ['#nav a[data-page=dashboard]', 'Übersicht: Node-Status, Advert senden, letzte Ereignisse.'],
  ['#nav a[data-page=contacts]', 'Kontakte: alle bekannten Teilnehmer, Direktnachrichten, Visitenkarten.'],
  ['#nav a[data-page=channels]', 'Kanäle: anlegen, bearbeiten, löschen und im Kanal chatten.'],
  ['#nav a[data-page=nodes]', 'Knoten: Repeater, Rooms und Sensoren mit Akku und Erreichbarkeit.'],
  ['#nav a[data-page=battery]', 'Akku: Verlauf von Akkuspannung und Temperatur als Diagramm und Tabelle.'],
  ['#nav a[data-page=map]', 'Karte: Knoten mit Position auf einer Karte.'],
  ['#nav a[data-page=automation]', 'Automatik: Regeln für Akku-Abfrage, Pings und automatische Antworten.'],
  ['#nav a[data-page=sensors]', 'Sensoren: Telemetrie-Werte (Temperatur, Feuchte, Druck …) eines Geräts lesen und als Verlauf ansehen.'],
  ['#nav a[data-page=room]', 'Raum: Software-Raum für deine Kontakte (Beiträge per Direktnachricht, unbegrenzt gespeichert). Hier ein-/ausschalten und Beiträge lesen.'],
  ['#nav a[data-page=search]', 'Suche: durchsucht alle gespeicherten Nachrichten und legt Stichwörter für Benachrichtigungen fest.'],
  ['#nav a[data-page=radio]', 'Funk: Rauschpegel, Sende- und Empfangszeit sowie Paketzahlen deines Nodes im Verlauf.'],
  ['#nav a[data-page=network]', 'Netz: Funkwege zu deinen Kontakten und Nachbarn, die Repeater gemeldet haben, als Grafik und Liste.'],
  ['#nav a[data-page=mynode]', 'Mein Node: Name, Standort, Funkwerte, Sendeleistung, Datenschutz, Uhr und Neustart des eigenen Nodes.'],
  ['[data-save=Funk]', 'Schreibt Frequenz, Bandbreite, Spreizfaktor und Coding Rate zusammen. Falsche Werte = kein Empfang mehr; es wird vorher nachgefragt.'],
  ['#mnTime', 'Stellt die Uhr des Nodes nach der Uhr dieses Rechners (Pi). Wichtig für Zeitstempel in Nachrichten.'],
  ['#mnReboot', 'Startet den Node neu; das Programm verbindet sich danach selbst wieder.'],
  ['#sysLink', 'System: öffnet den Watchdog (Updates, Neustart des Programms). Nur mit laufendem Watchdog erreichbar.'],
  // Übersicht
  ['#usbCard', 'USB-Node: zeigt, ob der Node per Kabel erkannt wird und antwortet, wie lange schon, und wie verfügbar er war. Wird er abgezogen, verbindet sich das Programm nach dem Einstecken von selbst wieder.'],
  ['#usbSim button', 'Nur Simulation: spielt Abziehen, Einstecken oder einen hängenden Node durch, damit du die Meldungen sehen kannst.'],
  ['#maintCard', 'Wartung: gibt den USB-Anschluss für eine begrenzte Zeit frei und pausiert Senden und Meldungen. Endet von selbst.'],
  ['#advert', 'Sendet eine Visitenkarte (Advert), damit andere diesen Node sehen.'],
  ['#advertFlood', 'Aus: nur direkte Nachbarn hören es (Zero-Hop). An: wird im ganzen Netz weitergeleitet, belegt mehr Funkzeit.'],
  // Kontakte
  ['#search', 'Kontakte nach Name oder Schlüssel filtern.'],
  ['#myCard', 'Zeigt die eigene Visitenkarte zum Teilen.'],
  ['[data-act=chat]', 'Direktnachricht an diesen Kontakt schreiben.'],
  ['[data-act=export]', 'Visitenkarte dieses Kontakts zum Weitergeben anzeigen.'],
  ['[data-act=reset]', 'Gespeicherten Funkweg vergessen; der nächste Versand sucht den Weg neu.'],
  // Knoten
  ['#showClients', 'Auch normale Clients (Personen) in der Liste anzeigen.'],
  ['[data-act=poll]', 'Fragt den Status jetzt ab (eine Funkanfrage) und speichert den Akkustand.'],
  ['[data-act=ping]', 'Prüft, ob der Knoten antwortet. Direkt (Zero-Hop) zeigt die Signalstärke zu direkten Nachbarn.'],
  ['[data-act=manage]', 'Repeater verwalten wie in der MeshCore-App: anmelden, Einstellungen lesen/ändern, Advert, Neustart, Terminal. Jeder Schritt geht per Funk und braucht Geduld.'],
  ['#repPw', 'Admin-Passwort des Repeaters (höchstens 15 Zeichen). Ohne Eingabe wird ein gemerktes Passwort benutzt.'],
  ['#repRemember', 'Speichert das Passwort in einer eigenen Datei auf dem Pi. Jeder, der diese Oberfläche erreicht, kann dann den Repeater verwalten.'],
  ['#repReadAll', 'Liest alle Einstellungen nacheinander (eine Funkanfrage je Zeile, mit Pause). Stopp bricht ab.'],
  ['#repCmd', 'Eigener Befehl laut MeshCore-Repeater-CLI, z. B. get name oder ver. Riskante Befehle fragen vorher nach, manche sind gesperrt.'],
  ['[data-act=hist]', 'Verlauf von Akku und Erreichbarkeit der letzten 7 Tage.'],
  // Kanäle
  ['#addBtn', 'Neu anlegen. Der Eintrag wird auch auf den Node geschrieben.'],
  ['#keyBtn', 'Zeigt den Schlüssel dieses Kanals zum Weitergeben. Wer ihn hat, kann mitlesen.'],
  ['#editBtn', 'Name oder Schlüssel des Kanals ändern. Ein neuer Schlüssel leert den Verlauf.'],
  ['#delBtn', 'Löscht den Kanal vom Node und den gespeicherten Verlauf.'],
  ['#detailToggle', 'Zeigt bei diesem Kanal den Funkweg eingehender Nachrichten und die gehörten Wiederholungen eigener Nachrichten. Gespeichert wird immer.'],
  ['#chatText', 'Nachricht eingeben. Doppelklick auf eine fremde Nachricht fügt @[Name] ein; beim Senden kommt die Uhrzeit dieser Nachricht dazu, wenn Platz ist.'],
  ['#chatSend', 'Sendet die Nachricht in den Kanal; alle im Kanal sehen sie.'],
  ['#fName', 'Namen mit # am Anfang (z. B. #test) erzeugen den Schlüssel automatisch aus dem Namen.'],
  ['#fSecret', 'Geheimer Schlüssel (32 Hex-Zeichen). Leer lassen für Hashtag-Kanäle.'],
  ['#fSlot', 'Speicherplatz auf dem Node. Belegte Plätze werden überschrieben.'],
  ['#pushOn', 'Fragt die Erlaubnis für Benachrichtigungen an und meldet dieses Gerät beim Server an. Danach je Kanal „Push“ wählen.'],
  ['#pushMode', 'Push für diesen Kanal auf diesem Gerät: „jede Nachricht“ meldet alles, „einmal, bis gelesen“ meldet nur die erste neue Nachricht. Erst wenn du den Kanal hier angesehen hast, kommt wieder eine Meldung.'],
  ['#chanBell', 'Zahl der ungelesenen Nachrichten in allen Kanälen. Ein Kanal gilt als gelesen, wenn du ihn hier geöffnet hast.'],
  ['#pushTest', 'Schickt sofort eine Test-Meldung an dieses Gerät.'],
  ['#pushOff', 'Meldet dieses Gerät ab. Andere Geräte bleiben angemeldet.'],
  ['#pushBatt', 'Zusätzlich eine Meldung, wenn ein Repeater unter seine Akku-Warnstufe fällt.'],
  // Akku
  ['#slots', 'Bis zu drei Repeater wählen (Repeater 1, 2, 3). Es erscheinen nur Repeater mit aktiver Akku-Überwachung. Der Farbpunkt zeigt die Linie im Diagramm, darunter steht der letzte Wert. Die Auswahl bleibt gespeichert, bis du sie änderst.'],
  ['#ranges button', 'Zeitraum des Verlaufs.'],
  ['#views [data-view=chart]', 'Verlauf als Diagramm. Mit der Maus darüberfahren zeigt die Werte.'],
  ['#views [data-view=table]', 'Alle Messwerte als Tabelle, neueste zuerst.'],
  ['#poll', 'Fragt Akku und Temperatur jetzt ab (zwei Funkanfragen je Repeater). Sind mehrere Repeater gewählt, werden sie nacheinander mit etwa 20 Sekunden Pause abgefragt, damit die Stromversorgung des Pi nicht einbricht. Der neue Wert erscheint unter dem jeweiligen Repeater.'],
  ['#csv', 'Messwerte als CSV-Datei herunterladen (Excel-tauglich, Zeit in UTC).'],
  // Automatik
  ['#pauseBtn', 'Hält die gesamte Automatik an. Bleibt auch nach einem Neustart pausiert.'],
  ['#newKind', 'Art der neuen Regel wählen.'],
  ['#addRule', 'Neue Regel mit der gewählten Art anlegen.'],
  ['[data-act=toggle]', 'Regel ein- oder ausschalten.'],
  ['[data-act=edit]', 'Regel bearbeiten.'],
  ['[data-act=delete]', 'Löschen. Es wird vorher nachgefragt.'],
  ['[name=interval_min]', 'Abstand zwischen zwei Durchläufen. Kurze Abstände belegen Funkzeit, die alle im Netz teilen.'],
  ['[name=low_pct]', 'Unter diesem Ladestand (Prozent, Skala der MeshCore-App: 3000 mV = 0 %, 4200 mV = 100 %) gibt es eine Warnung, einmal pro Unterschreitung. Etwa 40 % ist ein üblicher Wert.'],
  ['[name=temperature]', 'Fragt zusätzlich die Temperatur ab. Kostet eine weitere Funkanfrage.'],
  ['[name=mode]', 'Wie geprüft oder verglichen wird.'],
  ['[name=text]', 'Dieser Text wird unverändert gesendet (ohne „[auto]“), damit Bots darauf reagieren können.'],
  ['[name=match]', 'Auf diesen Text in einer Nachricht antwortet die Regel.'],
  ['[name=reply]', 'Antwort (max. 150 Zeichen). Platzhalter: {name} (wird als Erwähnung @[Name] eingefügt), {time}, {hops}, {path} (Weg der empfangenen Nachricht, z. B. a1,b2,c3 oder „direkt“). Beginnt automatisch mit einer Erwähnung des Absenders („@[Name] …“), die in der MeshCore-App als Erwähnung erscheint und gleichzeitig verhindert, dass zwei solche Regeln sich gegenseitig endlos antworten. Beginnt der Text mit {name}, kommt keine zweite Erwähnung davor.'],
  ['[name=cooldown_min]', 'So lange wird dieselbe Person nach einer Antwort ignoriert.'],
  ['[name=max_per_hour]', 'Obergrenze für Antworten dieser Regel in 60 Minuten, alle Personen zusammen. Schützt Kanäle vor Spam.'],
  ['[name=direct]', 'Auch auf Direktnachrichten antworten.'],
  ['[name=channels]', 'In diesem Kanal antworten. Alle im Kanal sehen die Antwort.'],
  ['#log', 'Protokoll der Automatik: was gesendet, übersprungen oder abgelehnt wurde.'],
];

const KEY = 'mc-tips';
let enabled = false;
try { enabled = localStorage.getItem(KEY) === '1'; } catch { /* Speicher gesperrt */ }

function apply(root = document) {
  for (const [sel, text] of TIPS) {
    for (const n of root.querySelectorAll(sel)) if (!n.dataset.tip) n.dataset.tip = text;
  }
}

export function initTips(button) {
  const tip = document.createElement('div');
  tip.id = 'tip';
  tip.setAttribute('role', 'tooltip');
  const canPopover = typeof tip.showPopover === 'function';
  if (canPopover) tip.setAttribute('popover', 'manual');
  tip.hidden = true;
  document.body.append(tip);

  const hide = () => { tip.hidden = true; if (canPopover) { try { tip.hidePopover(); } catch { /* schon zu */ } } };
  function show(target) {
    if (!enabled || !target?.dataset.tip) return hide();
    tip.textContent = target.dataset.tip;
    tip.hidden = false;
    if (canPopover) { try { tip.showPopover(); } catch { /* schon offen */ } }
    const r = target.getBoundingClientRect(), t = tip.getBoundingClientRect();
    const left = Math.min(Math.max(8, r.left + r.width / 2 - t.width / 2), window.innerWidth - t.width - 8);
    const below = r.bottom + 8 + t.height < window.innerHeight;
    tip.style.left = `${left}px`;
    tip.style.top = `${below ? r.bottom + 8 : Math.max(8, r.top - t.height - 8)}px`;
  }
  const find = e => e.target.closest?.('[data-tip]');
  document.addEventListener('mouseover', e => show(find(e)));
  document.addEventListener('mouseout', e => { if (!e.relatedTarget || !find({target: e.relatedTarget})) hide(); });
  document.addEventListener('focusin', e => show(find(e)));
  document.addEventListener('focusout', hide);
  document.addEventListener('click', hide);
  window.addEventListener('hashchange', hide);

  let queued = false;
  new MutationObserver(() => {
    if (queued) return;
    queued = true;
    queueMicrotask(() => { queued = false; apply(); });
  }).observe(document.body, {childList: true, subtree: true});
  apply();

  function paint() {
    button.setAttribute('aria-pressed', String(enabled));
    button.classList.toggle('on', enabled);
    button.textContent = enabled ? 'Hilfe: an' : 'Hilfe: aus';
    button.dataset.tip = 'Hilfe-Tipps ein- oder ausschalten: Beim Darüberfahren erscheint dann eine Erklärung zu jedem Bedienelement.';
  }
  button.onclick = () => {
    enabled = !enabled;
    try { localStorage.setItem(KEY, enabled ? '1' : '0'); } catch { /* egal */ }
    if (!enabled) hide();
    paint();
  };
  paint();
}
