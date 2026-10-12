// UI-Rauchtest: lädt die Seitenmodule in jsdom gegen den laufenden Server.
import {JSDOM} from 'jsdom';
import fs from 'node:fs';
import {pathToFileURL, fileURLToPath} from 'node:url';

const BASE = process.env.BASE || 'http://127.0.0.1:8767';
const STATIC = fileURLToPath(new URL('../../app/static', import.meta.url));

const dom = new JSDOM('<!doctype html><body><main id="view"></main></body>', {url: BASE + '/', runScripts: 'outside-only', pretendToBeVisual: true});
const {window} = dom;
globalThis.window = window;
globalThis.document = window.document;
globalThis.location = window.location;
globalThis.HTMLElement = window.HTMLElement;
globalThis.FormData = window.FormData;
globalThis.confirm = () => true;
const realFetch = globalThis.fetch;
globalThis.fetch = (u, o) => realFetch(new URL(u, BASE), o);
window.HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
window.HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
window.HTMLFormElement.prototype.reportValidity = () => true;
window.SVGElement.prototype.createSVGRect = () => ({});
window.eval(fs.readFileSync(`${STATIC}/vendor/leaflet/leaflet.js`, 'utf8'));

let failures = 0;
const check = (cond, msg) => { console.log((cond ? 'OK   ' : 'FAIL ') + msg); if (!cond) failures++; };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const imp = p => import(pathToFileURL(`${STATIC}/${p}`).href);
const ctx = (status) => ({
  on: () => () => {}, history: [], refreshStatus: async () => {}, banner: () => {},
  state: {config: {tile_url: 'http://127.0.0.1:1/{z}/{x}/{y}.png'}, status},
});
const fresh = () => { const r = document.getElementById('view'); r.innerHTML = ''; return r; };
const click = el => el.dispatchEvent(new window.MouseEvent('click', {bubbles: true}));
const errors = [];
window.addEventListener('error', e => errors.push(e.message));

const status = await (await fetch('/api/status')).json();

// ---- Dashboard
{
  const root = fresh();
  const un = await (await imp('pages/dashboard.js')).mount(root, ctx(status));
  await sleep(200);
  check(root.querySelector('#status').textContent.includes('SimNode'), 'Dashboard zeigt Node-Status');
  click(root.querySelector('#advert')); await sleep(200);
  check(root.querySelector('#error').textContent === '', 'Advert senden ohne Fehler');
  // USB-Karte: Zustand, Verfügbarkeit, Abziehen/Einstecken (Simulation)
  const usbText = () => root.querySelector('#usbCard').textContent;
  check(!root.querySelector('#usbCard').hidden && root.querySelector('#usbState').textContent.includes('verbunden'), 'USB-Karte zeigt „verbunden“');
  check(usbText().includes('Verfügbarkeit 24 h'), 'USB-Karte zeigt Verfügbarkeit');
  check(!root.querySelector('#usbSim').hidden, 'Simulations-Knöpfe sichtbar');
  click(root.querySelector('[data-sim=unplug]')); await sleep(5500);
  check(root.querySelector('#usbState').textContent.includes('kein USB-Gerät'), 'Abziehen wird erkannt');
  click(root.querySelector('[data-sim=plug]')); await sleep(9000);
  check(root.querySelector('#usbState').textContent.includes('verbunden'), 'Einstecken: Node verbindet sich wieder');
  check(root.querySelector('#usbEvents').textContent.includes('no_device'), 'Verlauf zeigt den Ausfall');
  un();
}

// ---- Kontakte
{
  const root = fresh();
  const un = await (await imp('pages/contacts.js')).mount(root, ctx(status));
  await sleep(300);
  const rows = () => root.querySelectorAll('#rows tr[data-key]');
  check(rows().length === 5, `Kontaktliste hat 5 Zeilen (ist ${rows().length})`);
  check(root.querySelector('#rows').textContent.includes('Repeater Hügel'), 'Repeater-Name sichtbar');
  const rep = [...rows()].find(r => r.textContent.includes('Hügel'));
  check(!rep.querySelector('[data-act=chat]'), 'Repeater hat keinen Nachrichten-Button');
  const alice = [...rows()].find(r => r.textContent.includes('Alice'));
  check(!!alice.querySelector('[data-act=chat]'), 'Client hat Nachrichten-Button');

  // Filter
  click(root.querySelector('#filters [data-f=repeater]'));
  check(rows().length === 2, 'Filter „Repeater“ zeigt 2');
  click(root.querySelector('#filters [data-f=all]'));

  // Hinzufügen
  click(root.querySelector('#addBtn'));
  root.querySelector('#cardText').value = 'meshcore://' + 'ab'.repeat(32) + '00000000';
  click(root.querySelector('#addOk')); await sleep(400);
  check(rows().length === 6, 'Kontakt hinzugefügt (6 Zeilen)');

  // Chat
  click([...rows()].find(r => r.textContent.includes('Alice')).querySelector('[data-act=chat]')); await sleep(300);
  check(!root.querySelector('#chatCard').hidden, 'Chat öffnet');
  root.querySelector('#chatText').value = 'Hallo aus dem Test';
  click(root.querySelector('#chatSend')); await sleep(500);
  check(root.querySelector('#chat').textContent.includes('Hallo aus dem Test'), 'Gesendete Nachricht erscheint im Chat');

  // Löschen
  const neu = [...rows()].find(r => r.dataset.key.startsWith('abab'));
  click(neu.querySelector('[data-act=delete]')); await sleep(400);
  check(rows().length === 5, 'Kontakt gelöscht (wieder 5 Zeilen)');
  un();
}

// ---- Knoten
{
  const root = fresh();
  const un = await (await imp('pages/nodes.js')).mount(root, ctx(status));
  await sleep(300);
  const rows = () => root.querySelectorAll('#rows tr[data-key]');
  check(rows().length === 3, `Knotenliste: 2 Repeater + 1 Room (ist ${rows().length})`);
  const low = [...rows()].find(r => r.textContent.includes('Dach'));
  click(low.querySelector('[data-act=poll]')); await sleep(600);
  const lowAfter = [...rows()].find(r => r.textContent.includes('Dach'));
  check(/36\d\d mV/.test(lowAfter.textContent), 'Akku nach „Abfragen“ sichtbar');
  check(lowAfter.querySelector('.bar') !== null && lowAfter.querySelector('.bar.low') === null, 'Akku 3620 mV: Balken sichtbar, noch nicht rot (Schwelle 3500)');
  const res = root.querySelector('#result');
  check(!res.hidden && res.textContent.includes('Abfrage Repeater Dach') && res.textContent.includes('Antwort erhalten'), 'Abfragen: Rückmeldung „Antwort erhalten“ über der Tabelle');
  click(lowAfter.querySelector('[data-act=ping]')); await sleep(200);
  check(!res.hidden && res.textContent.includes('Ping Repeater Dach'), 'Ping: Rückmeldung erscheint sofort (Warte-Hinweis oder Ergebnis)');
  await sleep(1500);
  check(/Ping Repeater Dach[^\n]*(Antwort erhalten|keine Antwort)/.test(res.textContent) && !res.textContent.includes('warte'), 'Ping: Ergebnis „Antwort erhalten“ oder „keine Antwort“ sichtbar');
  click([...rows()].find(r => r.textContent.includes('Dach')).querySelector('[data-act=hist]')); await sleep(400);
  check(!root.querySelector('#histCard').hidden, 'Verlauf öffnet');
  check(root.querySelector('#histPing').textContent.includes('2 von 2'), 'Ping-Statistik 2 von 2 (Abfrage + Ping)');
  // Repeater verwalten (Simulator-Passwort „sim“; pro Repeater höchstens ein Befehl alle 2 s)
  const dach = [...rows()].find(r => r.textContent.includes('Dach'));
  click(dach.querySelector('[data-act=manage]')); await sleep(500);
  const rep = root.querySelector('#repCard');
  check(!rep.hidden && rep.querySelector('#repPw') !== null, 'Verwalten öffnet mit Anmeldung');
  rep.querySelector('#repPw').value = 'falsch';
  rep.querySelector('#repLoginForm').dispatchEvent(new window.Event('submit', {cancelable: true})); await sleep(700);
  check(rep.querySelector('#repMsg').textContent.includes('Anmeldebestätigung'), 'Falsches Passwort wird gemeldet');
  rep.querySelector('#repPw').value = 'sim';
  rep.querySelector('#repLoginForm').dispatchEvent(new window.Event('submit', {cancelable: true})); await sleep(700);
  check(!rep.querySelector('#repBody').hidden && rep.querySelector('#repWho').textContent.includes('Admin'), 'Anmeldung klappt (Admin)');
  check(rep.querySelectorAll('#repSettings input, #repSettings select').length >= 15, 'Einstellungsliste da');
  click(rep.querySelector('[data-quick="0"]')); await sleep(600);
  check(rep.querySelector('#repTerm').textContent.includes('MeshCore'), 'Schnellaktion „Version“ zeigt Antwort');
  await sleep(2300);
  click(rep.querySelector('[data-read=name]')); await sleep(600);
  check(rep.querySelector('[data-set=name]').value.includes('Dach'), 'Einstellung „Name“ lesen füllt das Feld');
  await sleep(2300);
  rep.querySelector('[data-set=tx]').value = '99';
  click(rep.querySelector('[data-write=tx]')); await sleep(600);
  check(rep.querySelector('#repMsg').textContent.includes('erlaubt sind'), 'Ungültiger Wert wird vom Server abgelehnt');
  await sleep(2300);
  click(rep.querySelector('#repNb')); await sleep(600);
  check(rep.querySelector('#repLists').textContent.includes('Alice'), 'Nachbarn mit Namen');
  await sleep(2300);
  click(rep.querySelector('[data-quick="8"]')); await sleep(800);       // Neustart (confirm = ja)
  check(rep.querySelector('#repTerm').textContent.includes('keine Antwort'), 'Neustart gesendet');
  check(rep.querySelector('#repPw') !== null, 'Nach Neustart ist man abgemeldet');
  rep.querySelector('#repClose').click();
  check(rep.hidden, 'Panel schließt');
  root.querySelector('#showClients').checked = true;
  root.querySelector('#showClients').dispatchEvent(new window.Event('change'));
  check(rows().length === 5, 'Mit Clients: 5 Zeilen');
  un();
}

// ---- Karte
{
  const root = fresh();
  let un = () => {};
  try { un = await (await imp('pages/map.js')).mount(root, ctx(status)); }
  catch (e) { console.log('FEHLER in Karte:', String(e.message).slice(0, 300), String(e.stack).split('\n').slice(1, 6).join(' | ').slice(0, 500)); failures++; }
  await sleep(300);
  check(root.querySelectorAll('.leaflet-interactive').length >= 4, `Karte zeigt Marker (${root.querySelectorAll('.leaflet-interactive').length})`);
  check(root.querySelector('#noPosCard').hidden === false && root.querySelector('#noPos').textContent.includes('Raum'), 'Knoten ohne Position gelistet');
  un();
  // Seitenwechsel, während die Karte noch lädt: darf nicht werfen („Map container not found“)
  const r2 = fresh();
  const pending = (await imp('pages/map.js')).mount(r2, ctx(status));
  r2.innerHTML = '<p>andere Seite</p>';
  let cleanup = null, mapErr = null;
  try { cleanup = await pending; } catch (e) { mapErr = e; }
  check(mapErr === null && typeof cleanup === 'function', `Karte: Seitenwechsel beim Laden wirft nicht (${mapErr ? mapErr.message : 'ok'})`);
  check(r2.textContent === 'andere Seite', 'Karte: überschreibt die neue Seite nicht');
}

// ---- Kanäle
{
  const root = fresh();
  const un = await (await imp('pages/channels.js')).mount(root, ctx(status));
  await sleep(300);
  const items = () => root.querySelectorAll('.chanitem');
  check(items().length === 2, `Kanalliste hat 2 Einträge (ist ${items().length})`);
  check(root.querySelector('#slots').textContent.includes('2 von 40'), 'Belegte Plätze werden angezeigt');
  check(root.querySelector('#chatText').disabled, 'Ohne Auswahl ist das Eingabefeld gesperrt');
  check(root.querySelectorAll('#pushMode option').length === 3, 'Push-Art hat 3 Auswahlmöglichkeiten');
  const unr = await (await fetch('/api/channels/unread')).json();
  check(typeof unr.total === 'number' && typeof unr.channels === 'object', 'Ungelesen-Schnittstelle liefert Summe und Kanäle');
  check(root.querySelector('#pushInfo').textContent.length > 10 && root.querySelector('#pushBtns').hidden, 'Push: ohne Browser-Unterstützung steht eine Erklärung statt Buttons');

  click(root.querySelector('#addBtn')); await sleep(100);
  check(root.querySelector('#dlg').hasAttribute('open'), 'Anlegen-Dialog öffnet sich');
  root.querySelector('#fName').value = 'UI-Test';
  click(root.querySelector('#dlgOk')); await sleep(500);
  check(items().length === 3, 'Neuer Kanal erscheint in der Liste');
  check(root.querySelector('#chatTitle').textContent === 'UI-Test', 'Neuer Kanal ist geöffnet');
  check(!root.querySelector('#chatText').disabled, 'Eingabefeld ist freigeschaltet');

  root.querySelector('#chatText').value = 'Hallo Kanal';
  click(root.querySelector('#chatSend')); await sleep(400);
  check(root.querySelector('#chat').textContent.includes('Hallo Kanal'), 'Gesendete Nachricht erscheint im Chat');
  check(root.querySelector('#chat .bubble.out .who')?.textContent === 'Ich', 'Eigene Nachricht ist als „Ich“ markiert');

  root.querySelector('#fName').value = '';
  click(root.querySelector('#addBtn')); await sleep(100);
  root.querySelector('#fName').value = 'bad';
  root.querySelector('#fSecret').value = 'zz';
  click(root.querySelector('#dlgOk')); await sleep(300);
  check(root.querySelector('#dlgError').textContent.includes('Hex'), 'Ungültiger Schlüssel wird abgelehnt');
  click(root.querySelector('#dlgCancel'));

  // Pfade/Repeats: gespeichert wird immer, die Anzeige ist pro Kanal schaltbar
  check(!root.querySelector('#chat .detail'), 'Pfad-/Repeat-Zeilen sind standardmäßig ausgeblendet');
  click(root.querySelector('#detailToggle')); await sleep(400);
  check(root.querySelector('#chat .bubble.out .detail')?.textContent.includes('Repeat'), 'Nach dem Einschalten steht bei eigenen Nachrichten die Repeat-Zeile');
  const st = await (await fetch('/api/channels')).json();
  check(st.channels.find(c => c.name === 'UI-Test')?.show_details === true, 'Einstellung wurde serverseitig gespeichert');
  check(st.channels.filter(c => c.name !== 'UI-Test').every(c => c.show_details === false), 'Andere Kanäle bleiben ausgeblendet');
  click(root.querySelector('#detailToggle')); await sleep(300);
  check(!root.querySelector('#chat .detail'), 'Ausschalten blendet die Zeilen wieder aus');

  // Doppelklick auf eine fremde Nachricht erwähnt den Absender (eine solche Blase wird direkt eingefügt)
  root.querySelector('#chat').insertAdjacentHTML('beforeend', '<div class="bubble" data-name="Alice (Sim)">Hallo</div>');
  root.querySelector('#chat .bubble[data-name]').dispatchEvent(new window.MouseEvent('dblclick', {bubbles: true}));
  check(root.querySelector('#chatText').value === '@[Alice (Sim)] ', 'Doppelklick setzt @[Name] ins Eingabefeld');
  root.querySelector('#chat .bubble[data-name]').dispatchEvent(new window.MouseEvent('dblclick', {bubbles: true}));
  check(root.querySelector('#chatText').value === '@[Alice (Sim)] ', 'Zweiter Doppelklick verdoppelt die Erwähnung nicht');
  root.querySelector('#chatText').value = '';
  root.querySelector('#chat .bubble.out').dispatchEvent(new window.MouseEvent('dblclick', {bubbles: true}));
  check(root.querySelector('#chatText').value === '', 'Doppelklick auf eigene Nachricht erwähnt niemanden');

  // Erwähnung mit Uhrzeit der empfangenen Nachricht (nur wenn Platz ist)
  const U = await imp('lib/util.js');
  const hhmm = ts => new Date(ts * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false});
  const T0 = Math.floor(Date.now() / 1000) - 3600;
  check(U.mentionTag('Alice [Sim]') === '@[Alice Sim]' && U.mentionTag('  ') === '' && U.mentionTag(null) === '', 'Erwähnung: eckige Klammern im Namen werden entfernt, leerer Name ergibt keine Erwähnung');
  check(U.withReceiptTime('@[Bob] hi', 'Bob', T0) === `@[Bob] (${hhmm(T0)}) hi`, 'Uhrzeit wird hinter die Erwähnung eingefügt');
  check(U.withReceiptTime('@[Bob] ' + 'x'.repeat(132), 'Bob', T0) === '@[Bob] ' + 'x'.repeat(132), 'Zu lang: Uhrzeit entfällt, Text bleibt unverändert');
  check(U.withReceiptTime('hi @[Bob]', 'Bob', T0) === 'hi @[Bob]' && U.withReceiptTime('@[Bob] hi', 'Bob', 0) === '@[Bob] hi', 'Ohne Erwähnung am Anfang oder ohne Zeit: unverändert');
  check(U.withReceiptTime(`@[Bob] (${hhmm(T0)}) hi`, 'Bob', T0) === `@[Bob] (${hhmm(T0)}) hi`, 'Uhrzeit wird nicht doppelt eingefügt');
  root.querySelector('#chat').insertAdjacentHTML('beforeend', `<div class="bubble" data-name="Alice (Sim)" data-ts="${T0}">Frage</div>`);
  [...root.querySelectorAll('#chat .bubble[data-name]')].pop().dispatchEvent(new window.MouseEvent('dblclick', {bubbles: true}));
  root.querySelector('#chatText').value = '@[Alice (Sim)] ja, passt';
  root.querySelector('#chatText').dispatchEvent(new window.Event('input'));
  check(root.querySelector('#chatCount').textContent.includes('Uhrzeit'), 'Zähler zeigt, dass die Uhrzeit mitgeschickt wird');
  click(root.querySelector('#chatSend')); await sleep(700);
  const lastOut = [...root.querySelectorAll('#chat .bubble.out')].pop();
  check(lastOut && lastOut.textContent.includes(`(${hhmm(T0)}) ja, passt`), 'Gesendete Nachricht enthält die Uhrzeit der empfangenen Nachricht');
  root.querySelector('#chat').insertAdjacentHTML('beforeend', `<div class="bubble" data-name="Alice (Sim)" data-ts="${T0}">Noch eine Frage</div>`);   // der Chat wurde nach dem Senden neu geladen
  [...root.querySelectorAll('#chat .bubble[data-name]')].pop().dispatchEvent(new window.MouseEvent('dblclick', {bubbles: true}));
  root.querySelector('#chatText').value = '@[Alice (Sim)] ' + 'y'.repeat(120);
  root.querySelector('#chatText').dispatchEvent(new window.Event('input'));
  check(!root.querySelector('#chatCount').textContent.includes('Uhrzeit'), 'Zu lange Nachricht: keine Uhrzeit, der Zähler sagt es nicht');
  root.querySelector('#chatText').value = '';
  root.querySelector('#chatText').dispatchEvent(new window.Event('input'));

  click(root.querySelector('#keyBtn')); await sleep(300);
  check(/^[0-9a-f]{32}$/.test(root.querySelector('#keyOut').value), 'Schlüssel wird angezeigt (32 Hex-Zeichen)');
  click(root.querySelector('#keyClose'));
  check(root.querySelector('#keyOut').value === '', 'Schlüssel wird beim Schließen geleert');

  click(root.querySelector('#delBtn')); await sleep(400);
  check(items().length === 2, 'Kanal wurde gelöscht');
  check(root.querySelector('#chatText').disabled, 'Nach dem Löschen ist der Chat gesperrt');
  un();
}

// ---- Automatik
{
  const root = fresh();
  const un = await (await imp('pages/automation.js')).mount(root, ctx(status));
  await sleep(300);
  check(root.querySelector('#pauseInfo').textContent.includes('Läuft'), 'Automatik zeigt „Läuft“');

  // Auto-Antwort anlegen
  root.querySelector('#newKind').value = 'auto_reply';
  click(root.querySelector('#addRule'));
  check(root.querySelector('#form [name=match]') !== null, 'Formular für Auto-Antwort geöffnet');
  check(['cooldown_min', 'max_per_hour', 'direct'].every(n => root.querySelector(`#form [name=${n}]`)), 'Auto-Antwort hat Pause- und Mengenlimit');
  check(root.querySelectorAll('#form [name=channels]').length >= 1, 'Auto-Antwort bietet Kanäle an');
  root.querySelector('#form [name=match]').value = 'status?';
  root.querySelector('#form [name=reply]').value = 'alles ok bei {name}';
  click(root.querySelector('#save')); await sleep(500);
  check(root.querySelector('#rules').textContent.includes('status?'), 'Regel in Liste');

  // Akku-Regel mit Ziel „Alle Repeater“ und zu kurzem Intervall → Serverfehler wird angezeigt
  root.querySelector('#newKind').value = 'battery_poll';
  click(root.querySelector('#addRule'));
  check(root.querySelector('#form [name=temperature]')?.checked === true, 'Temperatur-Option standardmäßig an');
  check(!root.querySelector('#form [name=low_mv]'), 'Warnstufe wird nicht mehr in mV eingegeben');
  const pctIn = root.querySelector('#form [name=low_pct]');
  check(pctIn && pctIn.value === '58' && pctIn.min === '1' && pctIn.max === '100', `Warnstufe in Prozent, standardmäßig 58 % (3700 mV) (ist ${pctIn && pctIn.value})`);
  check(root.querySelector('#lowMv').textContent.includes('3700 mV'), `Prozent-Feld zeigt die mV daneben: ${root.querySelector('#lowMv').textContent}`);
  pctIn.value = '40'; pctIn.dispatchEvent(new window.Event('input'));
  check(root.querySelector('#lowMv').textContent.includes('3480 mV'), `40 % zeigen ≈ 3480 mV: ${root.querySelector('#lowMv').textContent}`);
  const U2 = await imp('lib/util.js');
  check(U2.lowMvFromPct(58, 3700) === 3700 && U2.lowMvFromPct(40, 3700) === 3480 && U2.lowMvFromPct(58, 3696) === 3696 && U2.lowMvFromPct(57, 3700) === 3684 && Number.isNaN(U2.lowMvFromPct('', 3700)) && Number.isNaN(U2.lowMvFromPct('abc', 3700)),
        'Unveränderter Prozentwert behält den gespeicherten mV-Wert, geänderter rechnet um');
  root.querySelector('#form [name=interval_min]').value = '1';
  click(root.querySelector('#save')); await sleep(400);
  check(root.querySelector('#formError').textContent.length > 0, `Zu kurzes Intervall wird abgelehnt: „${root.querySelector('#formError').textContent}“`);
  root.querySelector('#form [name=interval_min]').value = '30';
  // „Alle Repeater“ gibt es im Formular nicht mehr: mindestens ein Ziel muss angekreuzt sein
  check(root.querySelectorAll('#form [name=targets]').length >= 1, 'Akku-Regel bietet Ziele zum Ankreuzen an');
  const hill = root.querySelector(`#form [name=targets][value="${'c3'.repeat(32)}"]`);   // Repeater „Hügel“, den die Akku-Seite unten erwartet
  check(hill !== null, 'Repeater „Hügel“ ist als Ziel wählbar');
  if (hill) hill.checked = true;
  click(root.querySelector('#save')); await sleep(500);
  check(root.querySelectorAll('#rules tr[data-id]').length === 2, '2 Regeln gespeichert');
  const savedRules = await (await fetch('/api/rules')).json();
  const battRule = (savedRules.rules || savedRules).find(r => r.kind === 'battery_poll');
  check(battRule && battRule.config.low_mv === 3480, `Warnstufe 40 % wird als 3480 mV gespeichert (ist ${battRule && battRule.config.low_mv})`);
  check(/Warnung unter 40 % \(≈ 3480 mV\)/.test(root.querySelector('#rules').textContent), 'Regelliste nennt zuerst die Prozent');

  // Pause
  click(root.querySelector('#pauseBtn')); await sleep(400);
  check(root.querySelector('#pauseInfo').textContent.includes('Pausiert'), 'Pause aktiv');
  click(root.querySelector('#pauseBtn')); await sleep(400);
  check(root.querySelector('#pauseInfo').textContent.includes('Läuft'), 'Fortsetzen funktioniert');

  // Regel löschen
  click(root.querySelector('#rules [data-act=delete]')); await sleep(400);
  check(root.querySelectorAll('#rules tr[data-id]').length === 1, 'Regel gelöscht');
  un();
}

// ---- Akku-Seite (die Akku-Regel aus dem Automatik-Test ist noch aktiv)
{
  const REP = 'c3'.repeat(32);
  for (let i = 0; i < 3; i++) await fetch(`/api/nodes/${REP}/poll`, {method: 'POST', headers: {'content-type': 'application/json'}, body: '{"temperature": true}'});
  const root = fresh();
  const un = await (await imp('pages/battery.js')).mount(root, ctx(status));
  await sleep(500);
  const opts = [...root.querySelectorAll('select[data-slot="0"] option')];
  check(opts.length >= 1 && opts.some(o => o.textContent.includes('Hügel')), `Dropdown listet überwachte Repeater (${opts.length})`);
  root.querySelector('select[data-slot="0"]').value = REP;
  root.querySelector('select[data-slot="0"]').dispatchEvent(new window.Event('change', {bubbles: true})); await sleep(400);
  check(root.querySelector('#chBat svg') !== null, 'Akku-Diagramm gezeichnet');
  check(root.querySelector('#chBat svg path.lc-line') !== null, 'Akku-Linie vorhanden');
  check([...root.querySelectorAll('#chBat svg text')].some(t => /^\d+ %$/.test(t.textContent)), 'Prozent-Skala rechts vorhanden');
  check(root.querySelector('#chTemp svg path.lc-line') !== null, 'Temperatur-Linie vorhanden');
  click(root.querySelector('#views [data-view=table]')); await sleep(100);
  check(root.querySelector('#tableView').hidden === false && root.querySelectorAll('#rows tr').length >= 3, 'Tabellenansicht zeigt Messwerte');
  check(/\d{4}/.test(root.querySelector('#rows').textContent), 'Tabelle enthält mV-Werte');
  click(root.querySelector('#ranges [data-h="24"]')); await sleep(300);
  check(root.querySelectorAll('#rows tr').length >= 3, 'Zeitraum 24 h umschaltbar');
  un();
}

// ---- Akku-Seite: bis zu drei Repeater, Auswahl bleibt gespeichert, „Jetzt abfragen“ geht nacheinander, Werte unter jedem Feld
{
  const REP = 'c3'.repeat(32), REP2 = 'd4'.repeat(32);
  const made = await (await fetch('/api/rules', {method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify({kind: 'battery_poll', name: 'Akku 2', enabled: true, config: {targets: [REP2], interval_min: 60, low_mv: 3500, temperature: false}})})).json();
  const pick = (root, i, v) => { const el = root.querySelector(`select[data-slot="${i}"]`); el.value = v; el.dispatchEvent(new window.Event('change', {bubbles: true})); };
  let root = fresh();
  let un = await (await imp('pages/battery.js')).mount(root, ctx(status));
  await sleep(500);
  const labels = [...root.querySelectorAll('.slot label span')].map(x => x.textContent.trim());
  check(labels.length === 3 && labels.join('|') === 'Repeater 1|Repeater 2|Repeater 3', `Akku-Seite: drei Plätze „Repeater 1, 2, 3“ (${labels.join(', ')})`);
  check(!root.textContent.includes('Zum Vergleich'), 'Akku-Seite: keine Beschriftung „Zum Vergleich“ mehr');
  check(root.querySelectorAll('.slot-val').length === 3, 'Akku-Seite: unter jedem Auswahlfeld ist Platz für den letzten Wert');
  pick(root, 0, REP); await sleep(400);
  pick(root, 1, REP2); await sleep(400);
  pick(root, 2, REP); await sleep(400);                             // derselbe Repeater wie Platz 1: Platz 1 wird frei, er steht nur noch einmal da
  check(root.querySelector('select[data-slot="0"]').value === '' && root.querySelector('select[data-slot="2"]').value === REP, 'Akku-Seite: derselbe Repeater steht nur auf einem Platz');
  pick(root, 0, REP); await sleep(400);
  un();
  root = fresh();                                                   // Seite „neu geöffnet“
  un = await (await imp('pages/battery.js')).mount(root, ctx(status));
  await sleep(700);
  check(root.querySelector('select[data-slot="0"]').value === REP, 'Akku-Seite: Repeater 1 bleibt nach dem Neuöffnen gewählt');
  check(root.querySelector('select[data-slot="1"]').value === REP2, 'Akku-Seite: Repeater 2 bleibt nach dem Neuöffnen gewählt');
  const count = async k => (await (await fetch(`/api/battery/${k}?hours=24`)).json()).rows.length;
  const before = await count(REP), before2 = await count(REP2);
  const gap = (await (await fetch('/api/battery/nodes')).json()).poll_gap_s;
  click(root.querySelector('#poll')); await sleep(150);
  check(root.querySelector('#poll').disabled === true && /warte|frage/.test(root.querySelector('#poll').textContent), 'Jetzt abfragen: läuft nacheinander (Fortschritt sichtbar, Knopf gesperrt)');
  check(/abgefragt|wartet/.test(root.querySelector('[data-val="1"]').textContent), 'Jetzt abfragen: unter Repeater 2 steht, dass er an der Reihe ist');
  await sleep(700);
  check(await count(REP) > before, 'Jetzt abfragen: erster Repeater wurde sofort abgefragt');
  check(/\d{4} mV/.test(root.querySelector('[data-val="0"]').textContent), 'Jetzt abfragen: Wert von Repeater 1 steht unter seinem Auswahlfeld');
  if (gap <= 3) {                                                    // nur bei kurzer Testpause abwarten (Standard 20 s)
    await sleep(gap * 1000 + 2500);
    check(await count(REP2) > before2, 'Jetzt abfragen: zweiter Repeater wird nach der Pause abgefragt');
    check(/\d{4} mV/.test(root.querySelector('[data-val="1"]').textContent), 'Jetzt abfragen: Wert von Repeater 2 steht unter seinem Auswahlfeld');
    check(root.querySelector('#poll').disabled === false && root.querySelector('#poll').textContent === 'Jetzt abfragen', 'Jetzt abfragen: Knopf danach wieder frei');
    const names = [...root.querySelectorAll('#chBat svg text')].map(t => t.textContent);
    check(names.some(t => t.includes('Hügel')) && names.some(t => t.includes('Dach')), 'Akku-Diagramm: Legende nennt beide Repeater');
    // Jeder Repeater hat in Akku- und Temperatur-Diagramm dieselbe Linienform, die Repeater untereinander verschiedene.
    const forms = id => [...root.querySelectorAll(`#${id} path.lc-line, #${id} path[stroke-dasharray]`)].map(p => p.getAttribute('stroke-dasharray') || 'solid');
    const fb = new Set(forms('chBat')), ft = new Set(forms('chTemp'));
    check(fb.has('solid') && fb.has('7 4'), `Akku-Diagramm: beide Repeater mit verschiedener Linienart (${[...fb].join(', ')})`);
    check([...ft].every(x => fb.has(x)), `Temperatur-Diagramm: gleiche Linienarten wie im Akku-Diagramm (${[...ft].join(', ')})`);
  }
  un();
  await fetch('/api/battery/selection', {method: 'PUT', headers: {'content-type': 'application/json'}, body: '{"keys": []}'});
  await fetch(`/api/rules/${made.id}`, {method: 'DELETE'});
}

// ---- Mein Node (eigene Einstellungen)
{
  const root = fresh();
  const un = await (await imp('pages/mynode.js')).mount(root, ctx(status));
  await sleep(400);
  const f = k => root.querySelector(`[data-key="${k}"]`);
  check(f('name').value === 'SimNode' && f('radio_freq').value === '869.525', 'Mein Node zeigt aktuelle Werte');
  check(/Der Node geht \d+ s nach/.test(root.textContent), 'Uhrabweichung wird gezeigt');
  f('name').value = 'UI-Node';
  click(root.querySelector('[data-save="Allgemein"]')); await sleep(400);
  check(root.querySelector('#mnMsg').textContent.includes('gespeichert') && f('name').value === 'UI-Node', 'Name gespeichert');
  f('tx_power').value = '99';
  click(root.querySelector('[data-save="Funk"]')); await sleep(300);
  check(root.querySelector('#mnMsg').className.includes('err'), 'Ungültige Sendeleistung wird abgelehnt');
  f('tx_power').value = '14'; f('radio_sf').value = '9';
  globalThis.confirm = () => false;
  click(root.querySelector('[data-save="Funk"]')); await sleep(300);
  check((await (await fetch('/api/node/settings')).json()).settings.radio_sf === 11, 'Funkänderung ohne Bestätigung ändert nichts');
  globalThis.confirm = () => true;
  click(root.querySelector('[data-save="Funk"]')); await sleep(400);
  check((await (await fetch('/api/node/settings')).json()).settings.radio_sf === 9, 'Funkänderung mit Bestätigung gespeichert');
  click(root.querySelector('#mnTime')); await sleep(400);
  check(root.textContent.includes('Uhr stimmt'), 'Uhrabgleich funktioniert');
  globalThis.confirm = () => false;
  click(root.querySelector('#mnReboot')); await sleep(300);
  globalThis.confirm = () => true;
  click(root.querySelector('#mnReboot')); await sleep(300);
  check(root.querySelector('#mnMsg').textContent.includes('Neustart ausgelöst'), 'Neustart nach Bestätigung');
  un();
}

// ---- Netz, Sensoren, Funk
{
  const REP = 'c3'.repeat(32);
  let root = fresh();
  let un = await (await imp('pages/network.js')).mount(root, ctx(status));
  await sleep(400);
  check(root.querySelector('#netGraph svg') !== null && root.querySelectorAll('#netGraph circle').length >= 5, 'Netz: Grafik gezeichnet');
  check(/über Repeater Hügel/.test(root.querySelector('#netRoutes').textContent), 'Netz: Weg mit Zwischenstation benannt');
  un();

  root = fresh();
  un = await (await imp('pages/sensors.js')).mount(root, ctx(status));
  await sleep(300);
  // Auswahlliste (kein Eingabefeld zum Tippen): jedes Gerät ist eine Auswahlmöglichkeit
  const sel = root.querySelector('#node');
  check(sel.tagName === 'SELECT' && sel.options.length >= 3, `Sensoren: Auswahlliste mit Geräten (${sel.tagName}, ${sel.options.length})`);
  check([...sel.options].some(o => o.textContent.includes('Hügel') && o.textContent.includes('Repeater')), 'Sensoren: Eintrag zeigt Name und Art');
  const search = root.querySelector('#nodeSearch');
  search.value = 'hügel'; search.dispatchEvent(new window.Event('input')); await sleep(300);
  check(sel.options.length === 1 && sel.value === REP, `Sensoren: Suche „hügel“ lässt nur den Treffer übrig (${sel.options.length})`);
  search.value = 'gibtesnicht'; search.dispatchEvent(new window.Event('input')); await sleep(200);
  check(sel.options.length === 1 && sel.value === '' && sel.options[0].textContent.includes('Keine Treffer'), 'Sensoren: ohne Treffer steht „Keine Treffer“');
  search.value = 'repeater'; search.dispatchEvent(new window.Event('input')); await sleep(300);
  check(sel.options.length >= 2 && [...sel.options].every(o => o.textContent.toLowerCase().includes('repeater')), 'Sensoren: Suche nach der Art filtert');
  search.value = ''; search.dispatchEvent(new window.Event('input')); await sleep(200);
  check(sel.options.length >= 3, 'Sensoren: leere Suche zeigt wieder alle Geräte');
  sel.value = REP;
  sel.dispatchEvent(new window.Event('change')); await sleep(300);
  click(root.querySelector('#read')); await sleep(600);
  check(/Luftfeuchte/.test(root.querySelector('#latest').textContent) && /Temperatur/.test(root.querySelector('#latest').textContent), 'Sensoren: Werte gelesen');
  check(root.querySelectorAll('#charts [data-ch]').length >= 3, 'Sensoren: Verläufe angelegt');
  un();

  await fetch('/api/stats/sample', {method: 'POST'}); await sleep(50);
  root = fresh();
  un = await (await imp('pages/radio.js')).mount(root, ctx(status));
  await sleep(400);
  check(/Letzte Messung/.test(root.querySelector('#current').textContent), 'Funk: Messwert angezeigt');
  click(root.querySelector('#sample')); await sleep(300);
  check(root.querySelector('#error').textContent === '', 'Funk: „Jetzt messen“ ohne Fehler');
  un();

  const bk = await (await fetch('/api/backup/export')).json();
  const dry = await (await fetch('/api/backup/restore', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({data: bk, dry_run: true})})).json();
  check(dry.plan && Array.isArray(dry.plan.channels), 'Sicherung: Trockenlauf liefert Plan');
}

// ---- Raum, Suche
{
  let root = fresh();
  let un = await (await imp('pages/room.js')).mount(root, ctx(status));
  await sleep(300);
  root.querySelector('#en').checked = true;
  click(root.querySelector('#save')); await sleep(600);
  root.querySelector('#ptext').value = 'Hallo Raum-Test';
  root.querySelector('#pf').dispatchEvent(new window.Event('submit', {cancelable: true})); await sleep(400);
  check(root.querySelector('#posts').textContent.includes('Hallo Raum-Test'), 'Raum: Beitrag erscheint');
  check(root.querySelector('#msg').className.indexOf('err') < 0, 'Raum: ohne Fehler');
  check(root.querySelector('#ptext').value === '', 'Raum: Textfeld nach dem Senden leer');
  un();

  root = fresh();
  un = await (await imp('pages/search.js')).mount(root, ctx(status));
  await sleep(300);
  root.querySelector('#kw').value = 'Notfall, CQ';
  click(root.querySelector('#kwSave')); await sleep(300);
  check(root.querySelector('#kwMsg').textContent === 'Gespeichert.', 'Suche: Stichwörter gespeichert');
  await fetch('/api/channels/0/messages');
  root.querySelector('#q').value = 'Hallo';
  root.querySelector('#sf').dispatchEvent(new window.Event('submit', {cancelable: true})); await sleep(400);
  check(root.querySelector('#err').textContent === '', 'Suche: ohne Fehler');
  un();
}

// ---- Wartung
{
  const root = fresh();
  const un = await (await imp('pages/dashboard.js')).mount(root, ctx(status));
  await sleep(400);
  click(root.querySelector('#maintBtns [data-m="60"]')); await sleep(500);
  check(/Wartung läuft noch/.test(root.querySelector('#maintState').textContent) && !root.querySelector('#maintEnd').hidden, 'Wartung: gestartet');
  check((await (await fetch('/api/status')).json()).maintenance.active, 'Wartung: Status meldet aktiv');
  click(root.querySelector('#maintEnd')); await sleep(500);
  check(/Keine Wartung/.test(root.querySelector('#maintState').textContent), 'Wartung: beendet');
  un();
}

check(errors.length === 0, `Keine JS-Fehler im Fenster ${errors.length ? JSON.stringify(errors) : ''}`);
console.log(failures ? `\n${failures} FEHLER` : '\nAlle UI-Prüfungen bestanden');
process.exit(failures ? 1 : 0);
