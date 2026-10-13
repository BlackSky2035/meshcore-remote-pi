// Sensoren: Telemetriewerte (Cayenne LPP) von Repeatern, Rooms und Sensoren lesen, speichern und als Verlauf zeigen.
import {get, post} from '../lib/api.js';
import {esc, fmtDateTime, typeLabel} from '../lib/util.js';
import {lineChart} from '../lib/linechart.js';

const RANGES = [[24, '24 Stunden'], [168, '7 Tage'], [720, '30 Tage']];
const LABEL = {temperature: ['Temperatur', '°C'], humidity: ['Luftfeuchte', '%'], voltage: ['Spannung', 'V'], barometer: ['Luftdruck', 'hPa'],
  pressure: ['Luftdruck', 'hPa'], luminosity: ['Helligkeit', 'lx'], current: ['Strom', 'A'], power: ['Leistung', 'W']};
const lab = t => (LABEL[t] || [t, ''])[0];
const unit = t => (LABEL[t] || [t, ''])[1];

export async function mount(root, ctx) {
  root.innerHTML = `
    <section class="card">
      <h2 style="margin-top:0">Sensoren</h2>
      <p class="muted small">Liest die Telemetrie (Sensorwerte) eines Repeaters, Rooms oder Sensors. Jede Abfrage ist eine Funkanfrage.
        Es erscheinen nur Werte, die das Gerät wirklich meldet. Die automatische Akku-Regel mit „Temperatur“ speichert hier mit.</p>
      <p id="error" class="err"></p>
      <div class="row" style="gap:12px;flex-wrap:wrap;align-items:end">
        <label class="field" style="margin:0"><span>Suchen</span>
          <input type="search" id="nodeSearch" placeholder="Name oder Art …" autocomplete="off" style="max-width:170px"></label>
        <label class="field" style="margin:0"><span>Gerät</span>
          <select id="node"></select></label>
        <button id="read" class="small">Jetzt lesen</button>
        <div class="chips" id="ranges"></div>
      </div>
      <div id="latest" class="small" style="margin-top:10px;overflow-x:auto"></div>
    </section>
    <section class="card" id="charts" hidden></section>`;
  const $ = id => root.querySelector('#' + id);
  let alive = true, key = null, hours = 168, charts = [];
  $('ranges').innerHTML = RANGES.map(([h, t]) => `<button data-h="${h}" class="${h === hours ? 'on' : ''}">${t}</button>`).join('');

  let contacts = [];
  try { contacts = (await get('/api/contacts')).filter(c => ['repeater', 'room', 'sensor'].includes(c.type_name)); }
  catch (e) { $('error').textContent = e.message; }
  // Auswahlliste mit Suchfeld: Auf dem Handy gibt es keine brauchbare Vorschlagsliste im Eingabefeld. Die Liste zeigt ALLE bekannten Repeater,
  // Rooms und Sensoren (unabhängig davon, ob es für sie eine Automatik gibt); das Suchfeld filtert nach Name oder Art.
  const label = c => `${c.name} (${typeLabel(c.type_name)})`;
  function fillOptions() {
    const q = $('nodeSearch').value.trim().toLowerCase();
    const list = contacts.filter(c => !q || label(c).toLowerCase().includes(q));
    if (!contacts.length) { $('node').innerHTML = '<option value="">Keine Geräte</option>'; $('node').disabled = true; return false; }
    if (!list.length) { $('node').innerHTML = '<option value="">Keine Treffer</option>'; return false; }
    $('node').innerHTML = list.map(c => `<option value="${esc(c.public_key)}" ${c.public_key === key ? 'selected' : ''}>${esc(label(c))}</option>`).join('');
    if (!list.some(c => c.public_key === key)) { key = list[0].public_key; return true; }     // bisherige Auswahl herausgefiltert: erstes Gerät
    return false;
  }
  key = contacts.length ? contacts[0].public_key : null;
  fillOptions();

  // Ladezähler: eine ältere, später eintreffende Antwort überschreibt nie die neuere Anzeige.
  let seq = 0;
  async function show() {
    const my = ++seq, k = key, h = hours;
    const stale = () => !alive || my !== seq;
    charts.forEach(c => c.destroy()); charts = [];
    if (!k) { $('latest').textContent = ''; $('charts').hidden = true; return; }
    const {latest} = await get(`/api/sensors/${k}`);
    if (stale()) return;
    $('latest').innerHTML = latest.length
      ? `<table><tbody>${latest.map(l => `<tr><td>${esc(lab(l.type))}${l.channel ? ` <span class="muted">(Kanal ${l.channel})</span>` : ''}</td>
          <td><strong>${esc(String(+l.value.toFixed(2)).replace('.', ','))} ${esc(unit(l.type))}</strong></td>
          <td class="muted">${esc(fmtDateTime(l.ts))}</td></tr>`).join('')}</tbody></table>`
      : '<span class="muted">Noch keine Werte gespeichert. „Jetzt lesen“ drücken.</span>';
    const host = $('charts');
    host.hidden = !latest.length;
    host.innerHTML = latest.map((l, i) => `<h3 style="margin-top:${i ? 16 : 0}px">${esc(lab(l.type))}${l.channel ? ` (Kanal ${l.channel})` : ''}</h3><div data-ch="${i}"></div>`).join('');
    // Feste x-Achse über den gewählten Zeitraum bis jetzt, nicht nur über die Spanne der Messpunkte.
    const xMax = Date.now() / 1000, xMin = xMax - h * 3600;
    for (const [i, l] of latest.entries()) {
      const {rows} = await get(`/api/sensors/${k}/history?type=${encodeURIComponent(l.type)}&channel=${l.channel}&hours=${h}`);
      if (stale()) return;
      charts.push(lineChart(host.querySelector(`[data-ch="${i}"]`), rows.map(r => ({x: r.ts, y: r.value})),
        {unit: unit(l.type), decimals: 1, colorVar: '--c-series', label: lab(l.type), ariaLabel: `${lab(l.type)}-Verlauf`, xMin, xMax}));
    }
  }
  // Ereignis weiterreichen (die Zeitraum-Knöpfe brauchen e.target).
  const safe = fn => async e => { try { $('error').textContent = ''; await fn(e); } catch (err) { if (alive) $('error').textContent = err.message; } };
  $('nodeSearch').oninput = safe(async () => { if (fillOptions()) await show(); });
  $('node').onchange = safe(async () => {
    const k = $('node').value;
    if (k && k !== key) { key = k; await show(); }
  });
  $('ranges').onclick = safe(async e => {
    const b = e.target.closest('button'); if (!b) return;
    hours = Number(b.dataset.h);
    $('ranges').querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
    await show();
  });
  $('read').onclick = safe(async () => {
    if (!key) return;
    $('read').disabled = true; $('error').textContent = '';
    try { await post(`/api/sensors/${key}/read`, {}); await show(); } finally { $('read').disabled = false; }
  });
  await show().catch(e => { $('error').textContent = e.message; });
  return () => { alive = false; charts.forEach(c => c.destroy()); };
}
