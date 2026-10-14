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
// fetchHook(url): für Wettlauf-Tests Antworten verzögern oder ersetzen (Rückgabe Response = ersetzt, sonst echter Server)
let fetchHook = null;
globalThis.fetch = async (u, o) => {
  if (fetchHook) { const r = await fetchHook(String(u), o); if (r) return r; }
  return realFetch(new URL(u, BASE), o);
};
const fake = (data, ms = 0) => new Promise(r => setTimeout(() => r(new Response(JSON.stringify(data), {status: 200, headers: {'content-type': 'application/json'}})), ms));
const late = ms => new Promise(r => setTimeout(r, ms)).then(() => null);   // nur verzögern, dann echter Server
window.HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
window.HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
window.HTMLFormElement.prototype.reportValidity = () => true;
window.SVGElement.prototype.createSVGRect = () => ({});
window.SVGElement.prototype.createSVGPoint = () => ({x: 0, y: 0, matrixTransform() { return this; }});
window.SVGElement.prototype.getScreenCTM = () => null;
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
process.on('unhandledRejection', e => errors.push('unhandled: ' + (e?.message || e)));

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
  // Enter zweimal schnell (Senden läuft noch) = nur eine Funknachricht; Enter während der Wortwahl (IME) sendet nicht
  const enter = (el, extra = {}) => el.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'Enter', bubbles: true, ...extra}));
  const countSent = async (peer, text) => (await (await fetch(`/api/messages?peer=${peer}&limit=100`)).json()).filter(m => m.direction === 'out' && m.text === text).length;
  root.querySelector('#chatText').value = 'Doppel-Enter Kontakt';
  enter(root.querySelector('#chatText')); enter(root.querySelector('#chatText')); await sleep(600);
  check(await countSent('a1'.repeat(6), 'Doppel-Enter Kontakt') === 1, 'Kontakt-Chat: zweimal Enter während des Sendens sendet nur einmal');
  root.querySelector('#chatText').value = 'IME Kontakt';
  enter(root.querySelector('#chatText'), {isComposing: true}); await sleep(400);
  check(await countSent('a1'.repeat(6), 'IME Kontakt') === 0 && root.querySelector('#chatText').value === 'IME Kontakt', 'Kontakt-Chat: Enter während der Wortwahl (IME) sendet nicht');
  root.querySelector('#chatText').value = '';
  // Schneller Wechsel: die verspätete Antwort für Alice darf nicht unter Bobs Titel erscheinen
  fetchHook = u => u.includes('peer=' + 'a1'.repeat(6)) ? fake([{direction: 'in', text: 'VERALTET-ALICE', ts: Date.now() / 1000}], 500) : null;
  click([...rows()].find(r => r.textContent.includes('Alice')).querySelector('[data-act=chat]'));
  await sleep(50);
  click([...rows()].find(r => r.textContent.includes('Bob')).querySelector('[data-act=chat]'));
  await sleep(800); fetchHook = null;
  check(root.querySelector('#chatTitle').textContent.includes('Bob') && !root.querySelector('#chat').textContent.includes('VERALTET-ALICE'), 'Kontakt-Chat: schneller Wechsel zeigt keinen fremden Verlauf');

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
  await sleep(2300);                                                  // Tempolimit gilt auch für Anmeldungen (ein Funkpaket je 2 s)
  rep.querySelector('#repPw').value = 'sim';
  rep.querySelector('#repLoginForm').dispatchEvent(new window.Event('submit', {cancelable: true})); await sleep(700);
  check(!rep.querySelector('#repBody').hidden && rep.querySelector('#repWho').textContent.includes('Admin'), 'Anmeldung klappt (Admin)');
  check(rep.querySelectorAll('#repSettings input, #repSettings select').length >= 15, 'Einstellungsliste da');
  click(rep.querySelector('[data-quick="0"]')); await sleep(2600);    // direkt nach der Anmeldung wartet der Server die Restzeit (2 s) ab
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
  // Panel schließen, während die Anmeldung (Funk) noch läuft: kein Fehler, kein Fehlerbanner
  await sleep(2300);
  click([...rows()].find(r => r.textContent.includes('Dach')).querySelector('[data-act=manage]')); await sleep(500);
  fetchHook = u => u.includes('/login') ? late(400) : null;
  rep.querySelector('#repPw').value = 'sim';
  rep.querySelector('#repLoginForm').dispatchEvent(new window.Event('submit', {cancelable: true}));
  await sleep(50);
  rep.querySelector('#repClose').click();
  await sleep(1200); fetchHook = null;
  check(rep.hidden && rep.innerHTML === '' && root.querySelector('#error').textContent === '' && errors.length === 0,
        `Verwalten: Schließen während einer Funkanfrage wirft nicht ${errors.length ? JSON.stringify(errors) : ''}`);
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
  // Verbindungen: an und gleich wieder aus, bevor /api/network antwortet -> keine Linien; schnelles An/An -> nur eine Ebene
  const shapes = () => root.querySelectorAll('.leaflet-interactive').length;
  const base = shapes(), links = root.querySelector('#showLinks');
  const toggle = on => { links.checked = on; links.dispatchEvent(new window.Event('change')); };
  toggle(true); await sleep(400);
  const withLinks = shapes();
  check(withLinks > base, `Karte: Verbindungen werden gezeichnet (${base} -> ${withLinks})`);
  toggle(false); await sleep(100);
  fetchHook = u => u.includes('/api/network') ? late(400) : null;
  toggle(true); toggle(false); await sleep(700);
  check(shapes() === base, `Karte: an/aus vor der Antwort lässt keine Linien zurück (${shapes()} statt ${base})`);
  toggle(true); toggle(false); toggle(true); await sleep(700); fetchHook = null;
  check(shapes() === withLinks, `Karte: schnelles Umschalten ergibt genau eine Linien-Ebene (${shapes()} statt ${withLinks})`);
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
  window.history.pushState({mcChat: 1}, '');                          // Rest eines Chats, der über das Menü verlassen wurde
  const un = await (await imp('pages/channels.js')).mount(root, ctx(status));
  await sleep(300);
  check(!window.history.state?.mcChat, 'Kanäle: verwaister Chat-Verlaufseintrag wird beim Öffnen der Seite ersetzt');
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
  // Enter zweimal schnell = nur eine Funknachricht; Enter während der Wortwahl (IME) sendet nicht
  {
    const chIdx = Number([...items()].find(i => i.textContent.includes('UI-Test')).dataset.idx);
    const enter = (extra = {}) => root.querySelector('#chatText').dispatchEvent(new window.KeyboardEvent('keydown', {key: 'Enter', bubbles: true, ...extra}));
    const countSent = async text => (await (await fetch(`/api/channels/${chIdx}/messages?limit=200`)).json()).filter(m => m.direction === 'out' && m.text === text).length;
    root.querySelector('#chatText').value = 'Doppel-Enter Kanal';
    enter(); enter(); await sleep(600);
    check(await countSent('Doppel-Enter Kanal') === 1, 'Kanal-Chat: zweimal Enter während des Sendens sendet nur einmal');
    root.querySelector('#chatText').value = 'IME Kanal';
    enter({isComposing: true}); await sleep(400);
    check(await countSent('IME Kanal') === 0 && root.querySelector('#chatText').value === 'IME Kanal', 'Kanal-Chat: Enter während der Wortwahl (IME) sendet nicht');
    root.querySelector('#chatText').value = '';
  }

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

  // Wischen: nach rechts = zurück zur Liste, nach unten/zu kurz/links = bleibt im Chat
  const touch = (type, x, y) => { const ev = new window.Event(type, {bubbles: true}); const t = [{clientX: x, clientY: y}];
    Object.defineProperty(ev, type === 'touchend' ? 'changedTouches' : 'touches', {value: t}); if (type === 'touchend') Object.defineProperty(ev, 'touches', {value: []});
    root.querySelector('#chatCard').dispatchEvent(ev); };
  const swipeBy = (dx, dy) => { touch('touchstart', 100, 300); touch('touchend', 100 + dx, 300 + dy); };
  swipeBy(40, 0); swipeBy(-150, 0); swipeBy(120, 150);
  check(root.querySelector('#chatTitle').textContent === 'UI-Test', 'Wischen: zu kurz, nach links oder schräg nach unten bleibt der Chat offen');
  swipeBy(150, 10); await sleep(100);
  check(root.querySelector('#chatTitle').textContent === 'Kein Kanal gewählt' && root.querySelector('#chatBack').hidden, 'Wischen nach rechts schließt den Chat (Kanalliste)');
  // Browser-„Zurück“ (iOS-Randwisch) schließt den Chat statt die Seite zu verlassen
  [...items()].find(i => i.textContent.includes('UI-Test')).click(); await sleep(300);
  check(window.history.state?.mcChat === 1, 'Offener Chat hat einen Verlaufseintrag');
  window.history.back(); await sleep(300);
  check(root.querySelector('#chatTitle').textContent === 'Kein Kanal gewählt' && !window.history.state?.mcChat, 'Browser-Zurück schließt den Chat');
  swipeBy(150, 0);                                                    // ohne offenen Chat: nichts passiert
  [...items()].find(i => i.textContent.includes('UI-Test')).click(); await sleep(400);
  // mit Mitziehen (touchmove): langsam und zu kurz -> springt zurück; weit genug -> gleitet weg und schließt
  const drag = async (dx, dt) => { touch('touchstart', 100, 300); touch('touchmove', 100 + dx / 2, 302); touch('touchmove', 100 + dx, 304);
    await sleep(dt); touch('touchend', 100 + dx, 304); await sleep(350); };
  await drag(40, 400);
  check(root.querySelector('#chatTitle').textContent === 'UI-Test' && root.querySelector('#chatCard').style.transform === '', 'Mitziehen: zu kurz, springt zurück');
  [...items()].find(i => i.textContent.includes('UI-Test')).click(); await sleep(200);
  await drag(200, 400);
  check(root.querySelector('#chatTitle').textContent === 'Kein Kanal gewählt' && root.querySelector('#chatCard').style.transform === '', 'Mitziehen: weit genug, Chat gleitet weg und schließt');
  [...items()].find(i => i.textContent.includes('UI-Test')).click(); await sleep(400);
  check(root.querySelector('#chatTitle').textContent === 'UI-Test', 'Kanal lässt sich nach dem Wischen wieder öffnen');
  // Wischen nur in der einspaltigen Handy-Ansicht (max. 640 px) und nicht im offenen Menü
  window.matchMedia = q => ({matches: false, media: q});
  swipeBy(150, 10); await sleep(100);
  check(root.querySelector('#chatTitle').textContent === 'UI-Test', 'Wischen: in der zweispaltigen Ansicht (Tablet/PC) passiert nichts');
  window.matchMedia = q => ({matches: /max-width:\s*640px/.test(q), media: q});
  const nav = document.createElement('nav'); nav.id = 'nav'; nav.innerHTML = '<a>Menü</a>'; document.body.appendChild(nav);
  const navTouch = (type, x, y) => { const ev = new window.Event(type, {bubbles: true}); const t = [{clientX: x, clientY: y}];
    Object.defineProperty(ev, type === 'touchend' ? 'changedTouches' : 'touches', {value: t}); if (type === 'touchend') Object.defineProperty(ev, 'touches', {value: []});
    nav.querySelector('a').dispatchEvent(ev); };
  navTouch('touchstart', 100, 300); navTouch('touchend', 250, 305); await sleep(100);
  check(root.querySelector('#chatTitle').textContent === 'UI-Test', 'Wischen im offenen Menü schließt den Chat nicht');
  nav.remove();
  swipeBy(150, 10); await sleep(100);
  check(root.querySelector('#chatTitle').textContent === 'Kein Kanal gewählt', 'Wischen: in der Handy-Ansicht (max. 640 px) schließt es den Chat');
  delete window.matchMedia;
  [...items()].find(i => i.textContent.includes('UI-Test')).click(); await sleep(400);
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
  // Doppeltippen am Handy (ohne dblclick): zweimal kurz tippen erwähnt, Wischen/Scrollen oder langsames Tippen nicht
  const tapEv = (type, el, x, y) => { const ev = new window.Event(type, {bubbles: true}); Object.defineProperty(ev, 'touches', {value: type === 'touchend' ? [] : [{clientX: x, clientY: y}]});
    Object.defineProperty(ev, 'changedTouches', {value: [{clientX: x, clientY: y}]}); el.dispatchEvent(ev); };
  const tap = (el, x = 50, y = 50, dx = 0) => { tapEv('touchstart', el, x, y); tapEv('touchend', el, x + dx, y); };
  const bub = root.querySelector('#chat .bubble[data-name]');
  tap(bub); tap(bub, 50, 50, 40); tap(bub);                          // Tippen, Wisch (unterbricht), Tippen: kein Doppeltippen
  check(root.querySelector('#chatText').value === '', 'Doppeltippen: Wischen dazwischen zählt nicht');
  await sleep(500); tap(bub); await sleep(500); tap(bub);
  check(root.querySelector('#chatText').value === '', 'Doppeltippen: zu langsam zählt nicht');
  tap(bub); tap(bub);
  check(root.querySelector('#chatText').value === '@[Alice (Sim)] ', 'Doppeltippen setzt @[Name] ins Eingabefeld');
  root.querySelector('#chatText').value = '';

  // Erwähnung ohne Uhrzeit: gesendet wird genau der Text im Eingabefeld
  const U = await imp('lib/util.js');
  check(U.mentionTag('Alice [Sim]') === '@[Alice Sim]' && U.mentionTag('  ') === '' && U.mentionTag(null) === '', 'Erwähnung: eckige Klammern im Namen werden entfernt, leerer Name ergibt keine Erwähnung');
  check(U.withReceiptTime === undefined, 'Uhrzeit-Einfügen ist entfernt');
  root.querySelector('#chat').insertAdjacentHTML('beforeend', `<div class="bubble" data-name="Alice (Sim)">Frage</div>`);
  [...root.querySelectorAll('#chat .bubble[data-name]')].pop().dispatchEvent(new window.MouseEvent('dblclick', {bubbles: true}));
  root.querySelector('#chatText').value = '@[Alice (Sim)] ja, passt';
  root.querySelector('#chatText').dispatchEvent(new window.Event('input'));
  check(root.querySelector('#chatCount').textContent.trim() === `${'@[Alice (Sim)] ja, passt'.length} / 139`, 'Zähler zählt genau den eingegebenen Text');
  click(root.querySelector('#chatSend')); await sleep(700);
  const lastOut = [...root.querySelectorAll('#chat .bubble.out')].pop();
  check(lastOut && lastOut.textContent.includes('ja, passt') && !/\(\d{2}:\d{2}\)\s*ja, passt/.test(lastOut.textContent), 'Gesendete Erwähnung enthält keine Uhrzeit');
  root.querySelector('#chatText').value = '';
  root.querySelector('#chatText').dispatchEvent(new window.Event('input'));

  // Kanalwechsel: Erwähnung und Entwurf bleiben nicht im Eingabefeld hängen
  {
    const other = () => [...items()].find(i => i.dataset.idx === '0');
    const uiTest = () => [...items()].find(i => i.textContent.includes('UI-Test'));
    root.querySelector('#chat').insertAdjacentHTML('beforeend', `<div class="bubble" data-name="Alice (Sim)">Wechsel</div>`);
    [...root.querySelectorAll('#chat .bubble[data-name]')].pop().dispatchEvent(new window.MouseEvent('dblclick', {bubbles: true}));
    check(root.querySelector('#chatText').value === '@[Alice (Sim)] ', 'Erwähnung vor dem Kanalwechsel gesetzt');
    other().click(); await sleep(400);
    check(root.querySelector('#chatText').value === '', 'Kanalwechsel: Erwähnung aus dem vorigen Kanal wandert nicht mit');
    root.querySelector('#chatText').value = '';
    // Schneller Wechsel: die verspätete Antwort von Kanal 0 darf nicht unter dem Titel von UI-Test erscheinen
    fetchHook = u => u.startsWith('/api/channels/0/messages') ? fake([{direction: 'in', name: 'X', text: 'VERALTET-0', ts: Date.now() / 1000}], 500) : null;
    other().click(); await sleep(50);
    uiTest().click(); await sleep(800); fetchHook = null;
    check(root.querySelector('#chatTitle').textContent === 'UI-Test' && !root.querySelector('#chat').textContent.includes('VERALTET-0'), 'Kanäle: schneller Wechsel zeigt keinen fremden Verlauf');
  }

  click(root.querySelector('#keyBtn')); await sleep(300);
  check(/^[0-9a-f]{32}$/.test(root.querySelector('#keyOut').value), 'Schlüssel wird angezeigt (32 Hex-Zeichen)');
  click(root.querySelector('#keyClose'));
  check(root.querySelector('#keyOut').value === '', 'Schlüssel wird beim Schließen geleert');

  click(root.querySelector('#delBtn')); await sleep(400);
  check(items().length === 2, 'Kanal wurde gelöscht');
  check(root.querySelector('#chatText').disabled, 'Nach dem Löschen ist der Chat gesperrt');
  un();
}

// ---- Kanäle: Sprung aus einer Push-Meldung (#/channels?c=0), Kontakte kommen erst nach dem Verlauf
{
  const now = Math.floor(Date.now() / 1000);
  const msgs = [{direction: 'in', name: 'A', text: 'alt', ts: now - 300}, {direction: 'out', text: 'ich', ts: now - 200},
    {direction: 'in', name: 'B', text: 'neu 1', ts: now - 100}, {direction: 'in', name: 'C', text: 'neu 2', ts: now - 50}];
  fetchHook = u => u === '/api/channels' ? fake({channels: [{idx: 0, name: 'Öffentlich', unread: 2, hashtag: false, show_details: false, last_ts: now - 50}], max_channels: 40})
    : u.startsWith('/api/channels/0/messages') ? fake(msgs)
    : u.startsWith('/api/channels/0/read') ? fake({ok: true})
    : u === '/api/contacts' ? late(500) : null;
  window.location.hash = '#/channels?c=0';
  const root = fresh();
  const un = await (await imp('pages/channels.js')).mount(root, ctx(status));
  await sleep(150);
  const sepBefore = root.querySelector('#chat .unread-sep');
  await sleep(700); fetchHook = null;
  const sep = root.querySelector('#chat .unread-sep');
  check(!!sepBefore && !!sep && sep.nextElementSibling?.textContent.includes('neu 1'), 'Push-Sprung: „Ungelesene Nachrichten“ bleibt, wenn die Kontakte später kommen');
  un();
  window.location.hash = '';
  if (window.history.state?.mcChat) window.history.back();
  await sleep(100);
}

// ---- Kanäle: veralteter Zähler in der Liste – die Markierung kommt vom Server (read_upto), nicht aus dem Zähler
{
  const now = Math.floor(Date.now() / 1000);
  const msgs = [{id: 1, direction: 'in', name: 'A', text: 'alt 1', ts: now - 400}, {id: 2, direction: 'in', name: 'A', text: 'alt 2', ts: now - 300},
    {id: 3, direction: 'out', text: 'ich', ts: now - 200}, {id: 4, direction: 'in', name: 'B', text: 'gelesen', ts: now - 100},
    {id: 5, direction: 'in', name: 'C', text: 'wirklich neu', ts: now - 50}];
  fetchHook = u => u === '/api/channels' ? fake({channels: [{idx: 0, name: 'Öffentlich', unread: 4, hashtag: false, show_details: false, last_ts: now - 50}], max_channels: 40})
    : u.startsWith('/api/channels/0/messages') ? fake(msgs)
    : u.startsWith('/api/channels/0/read') ? fake({ok: true, read_upto: 4})
    : u === '/api/contacts' ? fake([]) : null;
  const root = fresh();
  const un = await (await imp('pages/channels.js')).mount(root, ctx(status));
  await sleep(150);
  click(root.querySelector('.chanitem')); await sleep(300);
  const sep = root.querySelector('#chat .unread-sep');
  check(!!sep && sep.nextElementSibling?.textContent.includes('wirklich neu'), 'Ungelesen-Markierung nach Server-Markierung, nicht nach veraltetem Zähler');
  check(!root.querySelector('.chanitem .bell'), 'Glocke verschwindet sofort beim Öffnen');
  fetchHook = null;
  un();
  if (window.history.state?.mcChat) window.history.back();
  await sleep(100);
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
  click(root.querySelector('#save')); click(root.querySelector('#save'));      // Doppelklick
  await sleep(500);
  check(root.querySelector('#rules').textContent.includes('status?'), 'Regel in Liste');
  check((await (await fetch('/api/rules')).json()).filter(r => r.config.match === 'status?').length === 1, 'Doppelklick auf „Speichern“ legt die Regel nur einmal an');

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
  // Zwei aktive Kanal-Pings in denselben Kanal: Hinweis auf doppeltes Senden
  const mk = name => fetch('/api/rules', {method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify({kind: 'channel_ping', name, enabled: true, config: {channel: 0, interval_min: 60, text: 'ping'}})}).then(r => r.json());
  const p1 = await mk('Ping A'), p2 = await mk('Ping B');
  un();
  const un2 = await (await imp('pages/automation.js')).mount(root, ctx(status)); await sleep(500);
  check(/pingt denselben Kanal/.test(root.querySelector('#rules').textContent), 'Kanal-Ping: Hinweis bei zwei Regeln für denselben Kanal');
  await fetch(`/api/rules/${p2.id}`, {method: 'DELETE'});
  un2();
  const un3 = await (await imp('pages/automation.js')).mount(root, ctx(status)); await sleep(500);
  check(!/pingt denselben Kanal/.test(root.querySelector('#rules').textContent), 'Kanal-Ping: kein Hinweis bei nur einer Regel');
  await fetch(`/api/rules/${p1.id}`, {method: 'DELETE'});
  un3();
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
  // Verspätete ältere Antwort (7 Tage, hier leer) überschreibt die neuere (24 h) nicht
  fetchHook = u => u.startsWith(`/api/battery/${REP}?hours=168`) ? fake({rows: []}, 500) : null;
  click(root.querySelector('#ranges [data-h="168"]')); await sleep(50);
  click(root.querySelector('#ranges [data-h="24"]')); await sleep(900); fetchHook = null;
  check(root.querySelectorAll('#rows tr').length >= 3 && root.querySelector('#csv').href.includes('hours=24'), 'Akku: verspätete ältere Antwort überschreibt die neuere nicht');
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
    // Tabelle: je Repeater ein Reiter, Umschalten zeigt den anderen Repeater (und die CSV gehört zum gewählten)
    click(root.querySelector('#views button[data-view="table"]')); await sleep(200);
    const tabs = [...root.querySelectorAll('#tableTabs button[data-tab]')];
    check(tabs.length === 2 && tabs[0].classList.contains('on'), `Akku-Tabelle: zwei Reiter, der erste ist gewählt (${tabs.length})`);
    const firstFor = root.querySelector('#tableFor').textContent;
    click(tabs[1]); await sleep(150);
    check(root.querySelector('#tableFor').textContent !== firstFor && root.querySelector('#tableTabs button[data-tab].on') === root.querySelectorAll('#tableTabs button[data-tab]')[1],
      `Akku-Tabelle: zweiter Reiter zeigt den anderen Repeater (${root.querySelector('#tableFor').textContent})`);
    check(root.querySelector('#csv').href.includes(REP2), 'Akku-Tabelle: CSV gehört zum gewählten Reiter');
    click(root.querySelector('#views button[data-view="chart"]')); await sleep(200);
    // Jeder Repeater hat in Akku- und Temperatur-Diagramm dieselbe Linienform, die Repeater untereinander verschiedene.
    // Alle Linien durchgezogen; je Repeater eine eigene Farbe, in Akku- und Temperatur-Diagramm gleich
    const strokes = id => [...root.querySelectorAll(`#${id} path.lc-line`)].map(p => p.getAttribute('style'));
    const dashed = id => root.querySelectorAll(`#${id} path[stroke-dasharray], #${id} line[stroke-dasharray]`).length;
    const fb = new Set(strokes('chBat')), ft = new Set(strokes('chTemp'));
    check(dashed('chBat') === 0 && dashed('chTemp') === 0, 'Akku-/Temperatur-Diagramm: alle Linien durchgezogen');
    check(fb.size >= 2 && [...ft].every(x => fb.has(x)), `Diagramme: verschiedene Farben je Repeater, in beiden Diagrammen gleich (${[...fb].join(' | ')})`);
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

// ---- Mein Node: Wert vom Node passt zu keiner Auswahl → nie stillschweigend die erste Option speichern
{
  const real = await (await fetch('/api/node/settings')).json();
  const odd = {...real, settings: {...real.settings, radio_bw: 41.667}};
  let putBody = null;
  fetchHook = (u, o) => u.startsWith('/api/node/settings') && (!o || !o.method || o.method === 'GET') ? fake(odd)
    : u.startsWith('/api/node/settings') && o && o.method === 'PUT' ? (putBody = JSON.parse(o.body), fake(odd)) : null;
  const root = fresh();
  const un = await (await imp('pages/mynode.js')).mount(root, ctx(status));
  await sleep(400);
  const bw = root.querySelector('[data-key="radio_bw"]');
  check(bw.value === '' && bw.selectedOptions[0].textContent.includes('unbekannt (41.667)'), 'Unbekannte Bandbreite wird als „unbekannt“ gezeigt, nicht als erste Option');
  root.querySelector('[data-key="tx_power"]').value = '15';
  click(root.querySelector('[data-save="Funk"]')); await sleep(300);
  check(putBody && putBody.changes.tx_power === '15' && !('radio_bw' in putBody.changes) && !('radio_freq' in putBody.changes),
        'Nur die Sendeleistung wird geschrieben, die unbekannte Bandbreite bleibt unangetastet');
  fetchHook = null;
  un();
}

// ---- Zähler unter dem Eingabefeld zeigt bei Umlauten/Emojis die Byte-Zahl
{
  const U = await imp('lib/util.js');
  check(U.countLabel('hallo', 139).text === '5 / 139', 'Zähler: reiner ASCII-Text ohne Byte-Angabe');
  check(U.countLabel('Grüße 👍', 139).text === '8 / 139 · 12 Byte', 'Zähler: Umlaute und Emojis zeigen die Byte-Zahl');
  check(U.countLabel('ü'.repeat(100), 139).warn === true, 'Zähler warnt, wenn die Byte-Zahl die Grenze überschreitet');
  let calls = 0;
  const btn = window.document.createElement('button');
  const f = U.locked(btn, async () => { calls++; await sleep(50); });
  f(); f(); await sleep(80); f(); await sleep(80);
  check(calls === 2 && !btn.disabled, 'Doppelklick-Schutz: zweiter Klick während des Laufs wird ignoriert');
}

// ---- Netz, Sensoren, Funk
{
  const REP = 'c3'.repeat(32);
  let root = fresh();
  let un = await (await imp('pages/network.js')).mount(root, ctx(status));
  await sleep(400);
  check(root.querySelector('#netGraph svg') !== null && root.querySelectorAll('#netGraph circle').length >= 5, 'Netz: Grafik gezeichnet');
  check(/über Repeater Hügel/.test(root.querySelector('#netRoutes').textContent), 'Netz: Weg mit Zwischenstation benannt');
  // Antippen eines Punkts zeigt seinen Namen (Zeiger wird erst beim Ziehen festgehalten); nach dem Ziehen kein „Antippen“
  {
    const PE = window.PointerEvent || window.MouseEvent;
    const ptr = (el, type, x, y) => el.dispatchEvent(new PE(type, {bubbles: true, cancelable: true, clientX: x, clientY: y, pointerId: 1, pointerType: 'mouse'}));
    const node = root.querySelector('#netGraph g.net-node:not([data-id="me"])'), circle = node.querySelector('circle'), label = node.querySelector('.net-label');
    ptr(circle, 'pointerdown', 50, 50); ptr(circle, 'pointerup', 51, 50); click(circle);
    check(!label.hasAttribute('visibility'), 'Netz: Antippen eines Punkts zeigt seinen Namen');
    click(root.querySelector('#netSvg'));
    check(label.getAttribute('visibility') === 'hidden', 'Netz: Antippen daneben versteckt den Namen wieder');
    ptr(circle, 'pointerdown', 50, 50); ptr(circle, 'pointermove', 90, 60); ptr(circle, 'pointerup', 90, 60); click(circle);
    check(label.getAttribute('visibility') === 'hidden', 'Netz: Ziehen (Verschieben) zählt nicht als Antippen');
  }
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
  // Zeitraum-Knöpfe bekommen das Ereignis (früher: „Cannot read properties of undefined (reading 'target')“)
  click(root.querySelector('#ranges [data-h="24"]')); await sleep(400);
  check(root.querySelector('#error').textContent === '' && root.querySelector('#ranges [data-h="24"]').classList.contains('on'), `Sensoren: Zeitraum umschaltbar ohne Fehler (${root.querySelector('#error').textContent})`);
  // Verspätete ältere Antwort (30 Tage) überschreibt die neuere (7 Tage) nicht
  let first = true;
  fetchHook = u => u === `/api/sensors/${REP}` && first ? (first = false, fake({latest: []}, 500)) : null;
  click(root.querySelector('#ranges [data-h="720"]')); await sleep(50);
  click(root.querySelector('#ranges [data-h="168"]')); await sleep(900); fetchHook = null;
  check(/Temperatur/.test(root.querySelector('#latest').textContent) && root.querySelectorAll('#charts [data-ch]').length >= 3, 'Sensoren: verspätete ältere Antwort überschreibt die neuere nicht');
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
  // Hervorhebung auf dem Rohtext: „amp“/„39“ zerstören keine Entities (&amp; &#39;)
  const RAW = "Tom & Jerry's <amp> 39 AMP";
  fetchHook = u => u.startsWith('/api/search') ? fake({results: [{ts: Date.now() / 1000, kind: 'channel', where: 'Public', direction: 'in', name: 'N', text: RAW}]}) : null;
  for (const q of ['amp', '39', 'lt', '&']) {
    root.querySelector('#q').value = q;
    root.querySelector('#sf').dispatchEvent(new window.Event('submit', {cancelable: true})); await sleep(150);
    const body = root.querySelector('#res .setrow > div:last-child');
    const marks = [...body.querySelectorAll('mark')].map(m => m.textContent.toLowerCase());
    check(body.textContent === RAW && marks.length === (RAW.toLowerCase().includes(q) ? (q === 'amp' ? 2 : 1) : 0) && marks.every(m => m === q.toLowerCase()), `Suche „${q}“: Text unverändert, Treffer markiert (${body.textContent} | ${marks.join(',')})`);
  }
  fetchHook = null;
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
