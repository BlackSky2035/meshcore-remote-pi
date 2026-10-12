// Push-Benachrichtigungen im Browser: Service Worker anmelden, Abo beim Server eintragen, Einstellungen speichern.
// Jede Funktion wirft nur Error mit lesbarer Meldung; „nicht unterstützt“ ist ein normaler Zustand, kein Fehler.
import {get, post, put} from './api.js';

const isIos = () => /iphone|ipad|ipod/i.test(navigator.userAgent || '') ||
  (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
const standalone = () => !!(navigator.standalone || (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches));

function b64uToBytes(s) {
  const raw = atob(s.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - s.length % 4) % 4));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}
function bytesToB64u(buf) {
  let s = '';
  new Uint8Array(buf).forEach(b => { s += String.fromCharCode(b); });
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function pushSupport() {
  if (!window.isSecureContext) {
    return {ok: false, why: 'Push braucht eine verschlüsselte Verbindung (https). Über Tailscale (https://…ts.net) oder localhost geht es, über http://<IP> im Heimnetz nicht.'};
  }
  if (isIos() && !standalone()) {
    return {ok: false, why: 'Auf dem iPhone/iPad geht Push nur für die App auf dem Home-Bildschirm: Teilen-Symbol → „Zum Home-Bildschirm“, dann von dort öffnen.'};
  }
  if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) {
    return {ok: false, why: 'Dieser Browser unterstützt keine Push-Benachrichtigungen.'};
  }
  return {ok: true, why: ''};
}

async function registration() {
  const reg = await navigator.serviceWorker.register('/sw.js', {scope: '/'});
  await navigator.serviceWorker.ready;
  return reg;
}

export async function pushState() {
  const sup = pushSupport();
  const st = {supported: sup.ok, why: sup.why, permission: sup.ok ? Notification.permission : 'default',
              subscribed: false, endpoint: null, modes: {}, battery: false};
  if (!sup.ok) return st;
  try {
    const reg = await navigator.serviceWorker.getRegistration('/');
    const sub = reg ? await reg.pushManager.getSubscription() : null;
    if (sub) {
      const prefs = await get(`/api/push/prefs?endpoint=${encodeURIComponent(sub.endpoint)}`);
      Object.assign(st, {subscribed: prefs.subscribed, endpoint: sub.endpoint, battery: prefs.battery,
        modes: Object.fromEntries(Object.entries(prefs.modes || {}).map(([k, v]) => [Number(k), v]))});
    }
  } catch { /* Zustand unbekannt: als nicht angemeldet zeigen */ }
  return st;
}

export async function enablePush(modes = {}, battery = false) {
  const sup = pushSupport();
  if (!sup.ok) throw new Error(sup.why);
  const perm = await Notification.requestPermission();
  if (perm !== 'granted') throw new Error('Benachrichtigungen sind nicht erlaubt. In den Browser-/Systemeinstellungen für diese Seite freigeben.');
  const {public_key} = await get('/api/push/config');
  const reg = await registration();
  let sub = await reg.pushManager.getSubscription();
  if (!sub) sub = await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: b64uToBytes(public_key)});
  const json = sub.toJSON();
  await post('/api/push/subscribe', {
    endpoint: sub.endpoint, keys: {p256dh: json.keys?.p256dh || bytesToB64u(sub.getKey('p256dh')), auth: json.keys?.auth || bytesToB64u(sub.getKey('auth'))},
    channels: modes, battery, label: (navigator.userAgent || '').slice(0, 80)});
  return sub.endpoint;
}

export async function disablePush() {
  const reg = await navigator.serviceWorker.getRegistration('/');
  const sub = reg ? await reg.pushManager.getSubscription() : null;
  if (!sub) return;
  try { await post('/api/push/unsubscribe', {endpoint: sub.endpoint}); } catch { /* Server weg: lokal trotzdem abmelden */ }
  await sub.unsubscribe();
}

export const savePrefs = (endpoint, modes, battery) => put('/api/push/prefs', {endpoint, channels: modes, battery});
export const testPush = endpoint => post('/api/push/test', {endpoint});
