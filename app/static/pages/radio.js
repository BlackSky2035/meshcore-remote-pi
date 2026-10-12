// Funk: Zähler des eigenen Nodes als Verlauf (Rauschpegel, Sende- und Empfangszeit, Pakete). Kostet keine Sendezeit.
import {get, post} from '../lib/api.js';
import {esc, fmtDateTime} from '../lib/util.js';
import {lineChart} from '../lib/linechart.js';

const RANGES = [[6, '6 Stunden'], [24, '24 Stunden'], [168, '7 Tage'], [720, '30 Tage']];

export async function mount(root, ctx) {
  root.innerHTML = `
    <section class="card">
      <h2 style="margin-top:0">Funk-Statistik</h2>
      <p class="muted small">Zähler deines eigenen Nodes, automatisch alle 30 Minuten gesichert (nur über das Kabel, ohne Funkzeit).
        Der Sendezeit-Anteil ist eine Richtgröße: Die erlaubte Grenze hängt von Land und Teilband ab (in der EU meist 1 % oder 10 %).</p>
      <p id="error" class="err"></p>
      <div class="row" style="gap:12px;flex-wrap:wrap;align-items:end"><div class="chips" id="ranges"></div>
        <button id="sample" class="small">Jetzt messen</button>
        <button id="autoToggle" class="small secondary"></button></div>
      <p id="current" class="small muted"></p>
    </section>
    <section class="card" id="charts">
      <h3 style="margin-top:0">Rauschpegel (dBm)</h3><div id="chNoise"></div>
      <h3>Sendezeit-Anteil (%)</h3><div id="chTx"></div>
      <h3>Empfangszeit-Anteil (%)</h3><div id="chRx"></div>
      <h3>Pakete pro Stunde: empfangen</h3><div id="chRecv"></div>
      <h3>Pakete pro Stunde: gesendet</h3><div id="chSent"></div>
    </section>`;
  const $ = id => root.querySelector('#' + id);
  let hours = 24, charts = [], alive = true;
  $('ranges').innerHTML = RANGES.map(([h, t]) => `<button data-h="${h}" class="${h === hours ? 'on' : ''}">${t}</button>`).join('');

  function renderToggle(enabled) {
    $('autoToggle').textContent = enabled ? 'Automatische Messung: an' : 'Automatische Messung: aus';
    $('autoToggle').className = `small ${enabled ? 'secondary' : 'danger'}`;
    $('autoToggle').dataset.enabled = enabled ? '1' : '';
  }

  async function show() {
    const d = await get(`/api/stats?hours=${hours}`);
    if (!alive) return;
    renderToggle(d.enabled !== false);
    charts.forEach(c => c.destroy()); charts = [];
    // Feste x-Achse über den gewählten Zeitraum bis jetzt, nicht nur über die Spanne der Messpunkte -
    // sonst zeigt die Achse bei lückenhaften/frischen Daten einen viel kürzeren Zeitraum als ausgewählt.
    const xMax = Date.now() / 1000, xMin = xMax - hours * 3600;
    const pts = k => d.series.filter(r => r[k] != null).map(r => ({x: r.ts, y: r[k]}));
    const mk = (id, k, unit, dec, label) => charts.push(lineChart($(id), pts(k), {unit, decimals: dec, colorVar: '--c-series', label, ariaLabel: label, xMin, xMax}));
    mk('chNoise', 'noise_floor', 'dBm', 0, 'Rauschpegel'); mk('chTx', 'tx_pct', '%', 2, 'Sendezeit-Anteil');
    mk('chRx', 'rx_pct', '%', 1, 'Empfangszeit-Anteil'); mk('chRecv', 'recv_per_h', '/h', 0, 'Pakete empfangen');
    mk('chSent', 'sent_per_h', '/h', 0, 'Pakete gesendet');
    const l = d.latest;
    $('current').textContent = l ? `Letzte Messung ${fmtDateTime(l.ts)} · Rauschpegel ${l.noise_floor ?? '?'} dBm · Betriebszeit ${l.uptime != null ? Math.round(l.uptime / 3600) + ' h' : '?'}`
      + (d.error ? ` · Hinweis: ${d.error}` : '') : (d.error ? `Keine Messwerte: ${d.error}` : 'Noch keine Messwerte. Die erste kommt kurz nach dem Start oder mit „Jetzt messen“; Verläufe brauchen mindestens zwei Messpunkte.');
  }
  const safe = fn => async e => { try { $('error').textContent = ''; await fn(e); } catch (err) { $('error').textContent = err.message; } };
  $('ranges').onclick = safe(async e => {
    const b = e.target.closest('button'); if (!b) return;
    hours = Number(b.dataset.h);
    $('ranges').querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
    await show();
  });
  $('sample').onclick = safe(async () => { await post('/api/stats/sample', {}); await show(); });
  $('autoToggle').onclick = safe(async () => {
    await post('/api/stats/enabled', {enabled: !$('autoToggle').dataset.enabled});
    await show();
  });
  await show().catch(e => { $('error').textContent = e.message; });
  return () => { alive = false; charts.forEach(c => c.destroy()); };
}
