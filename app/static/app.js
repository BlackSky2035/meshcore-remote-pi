// Einstieg: Hash-Router, WebSocket, gemeinsamer Kontext für alle Seiten.
import {get, post} from './lib/api.js';
import {esc, batteryPct} from './lib/util.js';
import {initTips} from './lib/tips.js';

const PAGES = {
  dashboard: () => import('./pages/dashboard.js'),
  contacts: () => import('./pages/contacts.js'),
  channels: () => import('./pages/channels.js'),
  nodes: () => import('./pages/nodes.js'),
  battery: () => import('./pages/battery.js'),
  map: () => import('./pages/map.js'),
  automation: () => import('./pages/automation.js'),
  room: () => import('./pages/room.js'),
  search: () => import('./pages/search.js'),
  radio: () => import('./pages/radio.js'),
  sensors: () => import('./pages/sensors.js'),
  network: () => import('./pages/network.js'),
  mynode: () => import('./pages/mynode.js'),
};

// ---- Event-Bus (WebSocket-Events an die aktive Seite weiterreichen) ------
const handlers = new Map();   // Typ -> Set(fn); '*' = alle
export const history = [];    // letzte Events (für Seiten, die erst später geöffnet werden)

function on(type, fn) {
  if (!handlers.has(type)) handlers.set(type, new Set());
  handlers.get(type).add(fn);
  return () => handlers.get(type)?.delete(fn);
}

function dispatch(ev) {
  for (const key of [ev.type, '*']) handlers.get(key)?.forEach(fn => {
    try { fn(ev); } catch (e) { console.error(e); }
  });
}

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = m => {
    let d;
    try { d = JSON.parse(m.data); } catch { return; }      // kaputte Nachricht ignorieren
    if (d.kind === 'history') {
      history.length = 0;
      history.push(...d.events);
      dispatch({type: '_history', events: d.events});
    } else if (d.kind === 'event') {
      history.push(d.event);
      if (history.length > 300) history.shift();
      if (d.event.type === 'battery_low') banner(`Akku niedrig: ${d.event.payload.name} (${d.event.payload.mv} mV ≈ ${batteryPct(d.event.payload.mv)} %)`);
      if (d.event.type === 'usb_alert') banner(d.event.payload.text);
      if (['connected', 'disconnected', 'usb_state'].includes(d.event.type)) refreshStatus();
      if (['channel_message', 'channel_read'].includes(d.event.type)) refreshUnread();
      dispatch(d.event);
    }
  };
  ws.onclose = () => setTimeout(connectWS, 2000);
}

// ---- Status-Leiste ---------------------------------------------------------
const state = {config: {tile_url: '', mode: ''}, status: null, unread: {total: 0, channels: {}}};

let lastIssues = '';
async function refreshStatus() {
  try {
    const s = await get('/api/status');
    state.status = s;
    document.getElementById('dot').classList.toggle('on', !!s.connected);
    const issues = [...(s.problems || []), ...(s.node_error ? [`Node-Status: ${s.node_error}`] : [])];
    const text = issues.join(' · ');
    if (text && text !== lastIssues) banner(text);      // nur bei Änderung, sonst käme das Banner alle 15 s zurück
    lastIssues = text;
    const mb = document.getElementById('maintBar');
    if (s.maintenance && s.maintenance.active) {
      mb.textContent = `Wartungsmodus aktiv bis ${new Date(s.maintenance.until * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}: Node-Anschluss ist frei, es wird nicht gesendet und nichts gemeldet. Beenden unter „Übersicht“.`;
      mb.hidden = false;
    } else mb.hidden = true;
    document.getElementById('ver').textContent = `v${s.app_version} · ${s.commit}`;
    dispatch({type: '_status', status: s});
    refreshUnread();
  } catch {
    document.getElementById('dot').classList.remove('on');
  }
}

// ---- Ungelesen (Glocke in der Kopfzeile) -----------------------------------------
async function refreshUnread() {
  try {
    const u = await get('/api/channels/unread');
    state.unread = u;
    const bell = document.getElementById('chanBell');
    bell.hidden = !u.total;
    document.getElementById('chanBellN').textContent = u.total ? ` ${u.total > 99 ? '99+' : u.total}` : '';
    document.title = (u.total ? `(${u.total}) ` : '') + 'MeshCore Remote';
    if (navigator.setAppBadge) {                      // Zahl am App-Symbol (wo unterstützt)
      try { await (u.total ? navigator.setAppBadge(u.total) : navigator.clearAppBadge()); } catch { /* egal */ }
    }
    dispatch({type: '_unread', unread: u});
  } catch { /* Server kurz weg: Anzeige bleibt wie sie war */ }
}

function banner(text) {
  const b = document.getElementById('banner');
  b.innerHTML = `${esc(text)} <button class="small secondary" id="bannerClose">OK</button>`;
  b.hidden = false;
  document.getElementById('bannerClose').onclick = () => { b.hidden = true; };
}

// ---- Handy-Menü (Hamburger, nur auf schmalen Bildschirmen aktiv) ------------
const navEl = document.getElementById('nav');
const navToggle = document.getElementById('navToggle');
const navBackdrop = document.getElementById('navBackdrop');
function closeNav() {
  navEl?.classList.remove('open');
  if (navBackdrop) navBackdrop.hidden = true;
  navToggle?.setAttribute('aria-expanded', 'false');
}
function openNav() {
  navEl?.classList.add('open');
  if (navBackdrop) navBackdrop.hidden = false;
  navToggle?.setAttribute('aria-expanded', 'true');
}
navToggle?.addEventListener('click', () => (navEl.classList.contains('open') ? closeNav() : openNav()));
navBackdrop?.addEventListener('click', closeNav);
// Tippen auf die bereits aktive Seite ändert den Hash nicht, darum kommt kein "hashchange" und route() liefe sonst
// nicht: Menü schließen und die Seite von Hand neu laden, wie bei jeder anderen Navigation auch.
navEl?.addEventListener('click', e => {
  const a = e.target.closest('a[data-page]');
  if (!a || a.dataset.page !== document.querySelector('#nav a.active')?.dataset.page) return;
  e.preventDefault();
  closeNav();
  route();
});

// ---- Router ----------------------------------------------------------------
let unmount = null;
let token = 0;

async function route() {
  const page = (location.hash.replace(/^#\//, '') || 'dashboard').split('?')[0];
  const name = PAGES[page] ? page : 'dashboard';
  const my = ++token;
  if (unmount) { try { unmount(); } catch (e) { console.error(e); } unmount = null; }
  document.querySelectorAll('#nav a').forEach(a => a.classList.toggle('active', a.dataset.page === name));
  try { document.querySelector('#nav a.active')?.scrollIntoView({inline: 'center', block: 'nearest'}); } catch { /* ältere Browser */ }
  closeNav();
  const root = document.getElementById('view');
  root.innerHTML = '<p class="muted">lade …</p>';
  try {
    const mod = await PAGES[name]();
    if (my !== token) return;          // inzwischen woanders hingeklickt
    root.innerHTML = '';
    const mounted = await mod.mount(root, {on, state, history, refreshStatus, refreshUnread, banner});
    if (my !== token) {                // während mount() erneut navigiert: sofort wieder abbauen, nichts anzeigen
      try { mounted?.(); } catch (e) { console.error(e); }
      return;
    }
    unmount = mounted;
  } catch (e) {
    console.error(e);
    if (my !== token) return;          // inzwischen woanders hingeklickt: den Fehler nicht über die neue Seite schreiben
    root.innerHTML = `<div class="card err">Seite konnte nicht geladen werden: ${esc(e.message)}</div>`;
  }
}

// Anmeldung (nur wenn der Server ein Passwort verlangt)
async function ensureLogin() {
  let st;
  try { st = await get('/api/auth/status'); } catch { return; }
  if (st.required) showLogout();
  if (!st.required || st.logged_in) return;
  await new Promise(resolve => {
    document.body.insertAdjacentHTML('beforeend', `
      <div id="loginBox" style="position:fixed;inset:0;background:var(--bg,#171e28);z-index:100;display:flex;align-items:center;justify-content:center;padding:16px">
        <form id="loginForm" class="card" style="max-width:320px;width:100%">
          <h2 style="margin-top:0">Anmelden</h2>
          <input id="loginPw" type="password" placeholder="Passwort" autocomplete="current-password" autofocus style="width:100%;box-sizing:border-box">
          <p id="loginErr" class="err small"></p>
          <button type="submit">Anmelden</button>
        </form></div>`);
    document.getElementById('loginForm').onsubmit = async e => {
      e.preventDefault();
      try {
        await post('/api/auth/login', {password: document.getElementById('loginPw').value});
        document.getElementById('loginBox').remove();
        resolve();
      } catch (err) { document.getElementById('loginErr').textContent = err.message; }
    };
  });
}
function showLogout() {
  const b = document.getElementById('tipBtn');
  if (!b || document.getElementById('logoutBtn')) return;
  b.insertAdjacentHTML('beforebegin', '<button id="logoutBtn" class="small secondary" title="Abmelden">Abmelden</button>');
  document.getElementById('logoutBtn').onclick = async () => { await post('/api/auth/logout', {}); location.reload(); };
}
on('maintenance', () => refreshStatus());
globalThis.onLoginRequired = () => { if (!document.getElementById('loginBox')) location.reload(); };

async function init() {
  await ensureLogin();
  try { state.config = await get('/api/config'); } catch { /* Standardwerte */ }
  const wd = document.getElementById('sysLink');
  initTips(document.getElementById('tipBtn'));
  if (state.config.watchdog_port) {
    if (state.config.mode === 'sim') wd.dataset.tip = 'Demo: Zuerst im Watchdog-Ordner „python -m watchdog --demo“ starten, dann öffnet dieser Link den simulierten Watchdog.';
    wd.href = `${location.protocol}//${location.hostname}:${state.config.watchdog_port}/`;
    wd.hidden = false;
  }
  window.addEventListener('hashchange', route);
  connectWS();
  await refreshStatus();
  setInterval(refreshStatus, 15000);
  route();
}

init();

// Fehler im Browser-Code oder in Anfragen nie still verschlucken, aber auch nicht die Seite zerstören
let lastErr = '';
function showFault(msg) {
  if (msg === lastErr) return;
  lastErr = msg;
  setTimeout(() => { lastErr = ''; }, 5000);
  banner(`Fehler: ${msg}`);
}
// Harmlose Browser-Warnung, keine echte Fehlfunktion (tritt bei Diagrammen/Karte gelegentlich auf)
const BENIGN = /ResizeObserver loop/;
window.addEventListener('error', e => { if (!BENIGN.test(e.message || '')) showFault(e.message || 'unbekannter Fehler'); });
window.addEventListener('unhandledrejection', e => showFault((e.reason && e.reason.message) || String(e.reason)));
