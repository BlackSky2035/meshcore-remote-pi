// Kleine Hilfsfunktionen. Alles, was in HTML eingesetzt wird, läuft durch esc().

export function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => (
    {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

export function ago(ts) {
  if (!ts) return 'nie';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return 'gerade eben';
  if (s < 3600) return `vor ${Math.round(s / 60)} min`;
  if (s < 86400) return `vor ${Math.round(s / 3600)} h`;
  const d = Math.round(s / 86400);
  return d === 1 ? 'vor 1 Tag' : `vor ${d} Tagen`;
}

export const TYPE_LABEL = {client: 'Client', repeater: 'Repeater', room: 'Room', sensor: 'Sensor', unknown: '?'};
export const TYPE_COLOR = {client: '#1f6feb', repeater: '#d29922', room: '#8957e5', sensor: '#2da44e', unknown: '#6b7686'};

export function typeLabel(name) { return TYPE_LABEL[name] || name; }

export function pathLabel(len) {
  if (len === null || len === undefined) return '?';
  return len < 0 ? 'Flood' : (len === 0 ? 'direkt' : `${len} Hop${len === 1 ? '' : 's'}`);
}

// Einzelner LiPo/Li-Ion-Akku: Ruhespannung (mV) -> Ladestand (%). Einfache lineare Skala
// 3000 mV = 0 %, 4200 mV = 100 % – so wie die offizielle MeshCore-App es anzeigt. Keine echte
// Ladestandsmessung (eine LiPo-Zelle ist über weite Strecken flacher als linear), dient nur zur Anschauung.
const MIN_MV = 3000, MAX_MV = 4200;

export function batteryPct(mv) {
  return Math.round(Math.max(0, Math.min(100, (mv - MIN_MV) / (MAX_MV - MIN_MV) * 100)));
}

// Umkehrung: Prozent -> mV (für die Prozent-Achse im Diagramm)
export function batteryMv(pct) {
  const p = Math.max(0, Math.min(100, pct));
  return Math.round(MIN_MV + p / 100 * (MAX_MV - MIN_MV));
}

// @[Name] im Text als Erwähnungs-Chip darstellen (von Auto-Antworten und beim Doppelklick eingefügt).
export function mentions(text, ownName) {
  return esc(text).replace(/@\[([^\]]+)\]/g, (_all, n) =>
    `<span class="mention ${n === esc(ownName || '') ? 'me' : ''}">@${n}</span>`);
}

// Erwähnung eines Absenders: "@[Name]". Namen dürfen in MeshCore keine eckigen Klammern enthalten (seit App 1.27); kämen sie doch vor
// (ältere Nodes), würden sie die Erwähnung zerreißen, deshalb werden sie entfernt. Leer = keine Erwähnung möglich.
export function mentionTag(name) {
  const n = String(name || '').replace(/[\[\]]/g, '').replace(/\s+/g, ' ').trim();
  return n ? `@[${n}]` : '';
}

// Warnstufe einer Akku-Regel: im Formular in Prozent, gespeichert in mV. Wurde der Prozentwert nicht verändert, bleibt der gespeicherte mV-Wert
// unverändert (sonst würde z. B. 3700 mV ≈ 58 % beim bloßen Speichern zu 3696 mV).
export function lowMvFromPct(pct, origMv) {
  if (pct === '' || pct === null || pct === undefined) return NaN;        // leeres Feld ist ungültig (nicht stillschweigend 0 %)
  const p = Number(pct);
  if (!Number.isFinite(p)) return NaN;
  return origMv && p === batteryPct(origMv) ? Number(origMv) : batteryMv(p);
}

export function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString();
}

export function fmtDateTime(ts) {
  return new Date(ts * 1000).toLocaleString();
}

export function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// Inline-SVG-Linie für Verläufe: points = [{x: ts, y: wert}]
export function sparkline(points, {width = 360, height = 80, color = '#1f6feb', yMin, yMax} = {}) {
  if (points.length < 2) return '<p class="muted">Zu wenige Messwerte für einen Verlauf.</p>';
  const xs = points.map(p => p.x), ys = points.map(p => p.y);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const lo = yMin ?? Math.min(...ys), hi = yMax ?? Math.max(...ys);
  const span = (hi - lo) || 1;
  const pad = 4;
  const sx = x => pad + (x - x0) / ((x1 - x0) || 1) * (width - 2 * pad);
  const sy = y => height - pad - (y - lo) / span * (height - 2 * pad);
  const d = points.map((p, i) => `${i ? 'L' : 'M'}${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join(' ');
  return `<svg viewBox="0 0 ${width} ${height}" width="100%" style="max-width:${width}px" role="img">
    <path d="${d}" fill="none" stroke="${color}" stroke-width="2"/></svg>
    <div class="muted small">${Math.round(lo)} – ${Math.round(hi)} mV · zuletzt ${Math.round(ys[ys.length - 1])} mV ≈ ${batteryPct(ys[ys.length - 1])} %</div>`;
}
