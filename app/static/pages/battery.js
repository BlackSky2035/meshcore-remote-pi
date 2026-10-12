import {get, post, put} from '../lib/api.js';
import {esc, ago, fmtDateTime, batteryPct, batteryMv} from '../lib/util.js';
import {lineChart, niceTicks, rightMargin} from '../lib/linechart.js';

const RANGES = [[24, '24 Stunden'], [168, '7 Tage'], [720, '30 Tage'], [2160, '90 Tage']];
const TEMP_BASE_LO = 0, TEMP_BASE_HI = 35, TEMP_STEP = 5;

// Skala steht normalerweise fest auf 0-35 °C; liegen Messwerte außerhalb, wächst sie in 5°-Schritten mit.
function tempRange(values) {
  let lo = TEMP_BASE_LO, hi = TEMP_BASE_HI;
  if (!values.length) return [lo, hi];
  const dataLo = Math.min(...values), dataHi = Math.max(...values);
  while (dataLo < lo) lo -= TEMP_STEP;
  while (dataHi > hi) hi += TEMP_STEP;
  return [lo, hi];
}

// niceTicks() kann über den Rand hinausragen (z. B. 40 bei hi=35); hier bleiben Striche innerhalb [lo, hi].
function tempTicks(lo, hi) {
  const ticks = niceTicks(lo, hi).filter(t => t >= lo && t <= hi);
  if (!ticks.length || ticks[0] !== lo) ticks.unshift(lo);
  if (ticks[ticks.length - 1] !== hi) ticks.push(hi);
  return ticks;
}

export async function mount(root, ctx) {
  root.innerHTML = `
    <section class="card">
      <div class="row between">
        <h2 style="margin:0">Akku &amp; Temperatur</h2>
      </div>
      <p class="muted small">Verlauf der Repeater, für die eine aktive Regel „Akku-Überwachung“ läuft.
        Werte werden bei jeder Abfrage gespeichert.</p>
      <p id="error" class="err"></p>
      <div id="empty" class="muted" hidden>Keine Repeater überwacht. Unter „Automatik“ eine Regel
        „Akku-Überwachung“ anlegen und aktivieren.</div>
      <div id="controls" class="row" hidden style="gap:12px;flex-wrap:wrap;align-items:end">
        <label class="field" style="margin:0"><span>Repeater</span><select id="node"></select></label>
        <button id="setDefault" class="small secondary" title="Diesen Repeater beim Öffnen der Seite immer zuerst zeigen">Als Standard</button>
        <label class="field" style="margin:0"><span>Zum Vergleich</span><select id="node2"></select></label>
        <div class="chips" id="ranges"></div>
        <div class="chips" id="views">
          <button data-view="chart" class="on">Diagramm</button><button data-view="table">Tabelle</button>
        </div>
        <button id="poll" class="small">Jetzt abfragen</button>
        <a id="csv" class="small" download>CSV</a>
      </div>
      <p id="current" class="muted small"></p>
    </section>
    <section id="chartView" class="card" hidden>
      <h3 style="margin-top:0">Akku</h3><div id="chBat"></div>
      <h3>Temperatur</h3><div id="chTemp"></div>
    </section>
    <section id="tableView" class="card" hidden>
      <div class="lc-table"><table><thead><tr><th>Zeit</th><th>Akku (mV)</th><th>Akku (≈ %)</th><th>Temperatur (°C)</th></tr></thead>
      <tbody id="rows"></tbody></table></div>
    </section>`;

  const $ = id => root.querySelector('#' + id);
  let nodes = [], key = null, key2 = null, hours = 24, view = 'chart', rows = [], rows2 = [], charts = [];
  let defaultKey = null, alive = true;

  $('ranges').innerHTML = RANGES.map(([h, t]) =>
    `<button data-h="${h}" class="${h === hours ? 'on' : ''}">${t}</button>`).join('');

  const current = () => nodes.find(n => n.public_key === key);

  function drawCharts() {
    charts.forEach(c => c.destroy());
    charts = [];
    const n = current(), n2 = nodes.find(x => x.public_key === key2);
    const bat = rows.filter(r => r.mv != null).map(r => ({x: r.ts, y: r.mv}));
    const tmp = rows.filter(r => r.temp != null).map(r => ({x: r.ts, y: r.temp}));
    const bat2 = key2 ? rows2.filter(r => r.mv != null).map(r => ({x: r.ts, y: r.mv})) : [];
    const tmp2 = key2 ? rows2.filter(r => r.temp != null).map(r => ({x: r.ts, y: r.temp})) : [];
    // Feste x-Achse über den gewählten Zeitraum bis jetzt, nicht nur über die Spanne der Messpunkte.
    const xMax = Date.now() / 1000, xMin = xMax - hours * 3600;
    // Links runde Prozentwerte (0/20/.../100 %), rechts die zugehörigen, nicht runden mV-Werte dazu -
    // die Linie selbst bleibt in mV (das liefert der Node), nur die Beschriftung ist umgerechnet.
    const pct = [0, 20, 40, 60, 80, 100], mvTicks = pct.map(batteryMv);
    const mvLabels = mvTicks.map(mv => ({y: mv, label: `${Math.round(mv)} mV`}));
    charts.push(lineChart($('chBat'), bat, {unit: 'mV', colorVar: '--c-batt', label: n ? n.name : 'Akku in Prozent',
      rightAxis: mvLabels, yMin: 3000, yMax: 4200, yTicks: mvTicks, xMin, xMax,
      yLabels: pct.map(p => `${p} %`), extra: mv => `≈ ${batteryPct(mv)} %`,
      ariaLabel: `Akku-Verlauf ${n ? n.name : ''}`, extraSeries: n2 ? [{points: bat2, colorVar: '--c-batt2', label: n2.name}] : [],
      threshold: n ? {y: n.low_mv, label: `Warnschwelle ${batteryPct(n.low_mv)} % (≈ ${n.low_mv} mV)`} : null}));
    const [tLo, tHi] = tempRange([...tmp, ...tmp2].map(p => p.y));
    charts.push(lineChart($('chTemp'), tmp, {unit: '°C', decimals: 1, colorVar: '--c-temp', label: n ? n.name : 'Temperatur',
      ariaLabel: `Temperatur-Verlauf ${n ? n.name : ''}`,
      yMin: tLo, yMax: tHi, yTicks: tempTicks(tLo, tHi), xMin, xMax,
      rightPad: rightMargin(mvLabels),     // gleicher Rand wie beim Akku-Diagramm -> x-Achsen deckungsgleich
      extraSeries: n2 ? [{points: tmp2, colorVar: '--c-temp2', label: n2.name}] : []}));
  }

  function drawTable() {
    $('rows').innerHTML = rows.length ? [...rows].reverse().map(r => `<tr>
      <td>${esc(fmtDateTime(r.ts))}</td>
      <td>${r.mv == null ? '<span class="muted">–</span>' : r.mv}</td>
      <td>${r.mv == null ? '<span class="muted">–</span>' : batteryPct(r.mv) + ' %'}</td>
      <td>${r.temp == null ? '<span class="muted">–</span>' : r.temp.toFixed(1).replace('.', ',')}</td></tr>`).join('')
      : '<tr><td colspan="4" class="muted">Noch keine Messwerte in diesem Zeitraum.</td></tr>';
  }

  function showView() {
    $('chartView').hidden = view !== 'chart' || !key;
    $('tableView').hidden = view !== 'table' || !key;
    root.querySelectorAll('#views button').forEach(b => b.classList.toggle('on', b.dataset.view === view));
    if (view === 'chart') drawCharts(); else drawTable();
  }

  function describe() {
    const n = current();
    if (!n) { $('current').textContent = ''; return; }
    const b = n.battery, t = n.temperature;
    $('current').textContent = [
      b ? `Zuletzt ${b.mv} mV ≈ ${batteryPct(b.mv)} % (${ago(b.ts)})` : 'Noch kein Akkuwert',
      t ? `${t.celsius.toFixed(1).replace('.', ',')} °C (${ago(t.ts)})` : 'keine Temperatur gespeichert',
    ].join(' · ');
  }

  async function loadData() {
    if (!key) return;
    try {
      rows = (await get(`/api/battery/${key}?hours=${hours}`)).rows;
      rows2 = key2 ? (await get(`/api/battery/${key2}?hours=${hours}`)).rows : [];
      $('error').textContent = '';
    } catch (e) { $('error').textContent = e.message; rows = []; rows2 = []; }
    $('csv').href = `/api/battery/${key}/export.csv?hours=${hours}`;
    showView();
  }

  function renderSelects() {
    $('node').innerHTML = nodes.map(n =>
      `<option value="${esc(n.public_key)}" ${n.public_key === key ? 'selected' : ''}>${esc(n.name)}</option>`).join('');
    $('node2').innerHTML = '<option value="">— keiner —</option>' + nodes.filter(n => n.public_key !== key).map(n =>
      `<option value="${esc(n.public_key)}" ${n.public_key === key2 ? 'selected' : ''}>${esc(n.name)}</option>`).join('');
  }

  async function loadNodes() {
    try {
      const d = await get('/api/battery/nodes');
      nodes = d.nodes; defaultKey = d.default;
      $('error').textContent = '';
    } catch (e) { $('error').textContent = e.message; return; }
    $('empty').hidden = nodes.length > 0;
    $('controls').hidden = nodes.length === 0;
    if (!nodes.some(n => n.public_key === key)) key = (nodes.some(n => n.public_key === defaultKey) ? defaultKey : nodes[0]?.public_key) || null;
    if (!nodes.some(n => n.public_key === key2)) key2 = null;
    renderSelects();
    describe();
    if (!key) { showView(); return; }
    await loadData();
  }

  $('node').onchange = async e => { key = e.target.value; if (key2 === key) key2 = null; renderSelects(); describe(); await loadData(); };
  $('node2').onchange = async e => { key2 = e.target.value || null; describe(); await loadData(); };
  $('setDefault').onclick = async () => {
    if (!key) return;
    try { await put('/api/battery/default', {public_key: key}); defaultKey = key; $('current').textContent += ' · als Standard gespeichert.'; }
    catch (e) { $('error').textContent = e.message; }
  };
  $('ranges').onclick = async e => {
    const b = e.target.closest('button[data-h]');
    if (!b) return;
    hours = Number(b.dataset.h);
    root.querySelectorAll('#ranges button').forEach(x => x.classList.toggle('on', x === b));
    await loadData();
  };
  $('views').onclick = e => {
    const b = e.target.closest('button[data-view]');
    if (b) { view = b.dataset.view; showView(); }
  };
  $('poll').onclick = async () => {
    const btn = $('poll');
    btn.disabled = true; btn.textContent = 'frage ab …';
    try { await post(`/api/nodes/${key}/poll`, {temperature: true}); await loadNodes(); }
    catch (e) { $('error').textContent = e.message; }
    btn.disabled = false; btn.textContent = 'Jetzt abfragen';
  };

  const off = ctx.on('node_update', async ev => {
    if (!alive || ev.payload?.public_key !== key) return;
    await loadNodes();
  });
  await loadNodes();

  return () => { alive = false; charts.forEach(c => c.destroy()); if (typeof off === 'function') off(); };
}
