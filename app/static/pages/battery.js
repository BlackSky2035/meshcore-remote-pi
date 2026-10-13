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
      <div id="controls" hidden>
        <div class="slots" id="slots"></div>
        <div class="row" style="gap:12px;flex-wrap:wrap;align-items:end">
          <div class="chips" id="ranges"></div>
          <div class="chips" id="views">
            <button data-view="chart" class="on">Diagramm</button><button data-view="table">Tabelle</button>
          </div>
          <button id="poll" class="small">Jetzt abfragen</button>
          <a id="csv" class="small" download>CSV</a>
        </div>
      </div>
    </section>
    <section id="chartView" class="card" hidden>
      <h3 style="margin-top:0">Akku</h3><div id="chBat"></div>
      <h3>Temperatur</h3><div id="chTemp"></div>
    </section>
    <section id="tableView" class="card" hidden>
      <div class="chips" id="tableTabs" style="margin-bottom:8px"></div>
      <p id="tableFor" class="muted small" style="margin-top:0"></p>
      <div class="lc-table"><table><thead><tr><th>Zeit</th><th>Akku (mV)</th><th>Akku (≈ %)</th><th>Temperatur (°C)</th></tr></thead>
      <tbody id="rows"></tbody></table></div>
    </section>`;

  const $ = id => root.querySelector('#' + id);
  const SLOTS = 3;                                                  // bis zu drei Repeater nebeneinander im Diagramm
  const COLORS = ['--c-batt', '--c-batt2', '--c-batt3'];             // je Platz fest: gleiche Farbe, Strichart und Markierung in Akku- UND Temperatur-Diagramm
  const SHAPES = [{dash: null, shape: 'circle'}, {dash: null, shape: 'circle'}, {dash: null, shape: 'circle'}];   // alle durchgezogen, Unterschied nur über die Farbe
  const sampleSvg = i => {
    const c = `var(${COLORS[i]})`, d = SHAPES[i].dash ? ` stroke-dasharray="${SHAPES[i].dash}"` : '';
    const m = SHAPES[i].shape === 'square' ? `<rect x="11" y="6" width="6" height="6" fill="${c}"/>`
      : SHAPES[i].shape === 'triangle' ? `<polygon points="14,5 18,12 10,12" fill="${c}"/>` : `<circle cx="14" cy="9" r="3.5" fill="${c}"/>`;
    return `<svg class="swatch" width="28" height="18" viewBox="0 0 28 18" aria-hidden="true"><line x1="0" y1="9" x2="28" y2="9" stroke="${c}" stroke-width="2"${d}/>${m}</svg>`;
  };
  let nodes = [], sel = [null, null, null], hours = 24, view = 'chart', rowsBy = {}, charts = [];
  let tableSlot = null;                                              // welcher Repeater in der Tabelle steht (Reiter)
  let alive = true, selectionRestored = false, pollGapS = 20;
  const busy = [null, null, null];                                  // Status je Platz während „Jetzt abfragen“ (wartet / fragt ab / Fehler)

  $('ranges').innerHTML = RANGES.map(([h, t]) =>
    `<button data-h="${h}" class="${h === hours ? 'on' : ''}">${t}</button>`).join('');

  const nodeOf = k => nodes.find(n => n.public_key === k);
  const active = () => sel.map((k, i) => k && nodeOf(k) ? {k, i, n: nodeOf(k)} : null).filter(Boolean);     // gewählte Plätze in Reihenfolge
  const points = (k, f) => (rowsBy[k] || []).filter(r => r[f] != null).map(r => ({x: r.ts, y: r[f]}));

  function drawCharts() {
    charts.forEach(c => c.destroy());
    charts = [];
    const act = active();
    const main = act[0], extra = act.slice(1);
    const bat = main ? points(main.k, 'mv') : [], tmp = main ? points(main.k, 'temp') : [];
    // Feste x-Achse über den gewählten Zeitraum bis jetzt, nicht nur über die Spanne der Messpunkte.
    const xMax = Date.now() / 1000, xMin = xMax - hours * 3600;
    // Links runde Prozentwerte (0/20/.../100 %), rechts die zugehörigen, nicht runden mV-Werte dazu -
    // die Linie selbst bleibt in mV (das liefert der Node), nur die Beschriftung ist umgerechnet.
    const pct = [0, 20, 40, 60, 80, 100], mvTicks = pct.map(batteryMv);
    const mvLabels = mvTicks.map(mv => ({y: mv, label: `${Math.round(mv)} mV`}));
    // Jeder Platz behält seine Farbe (1 = Hauptfarbe), damit der Farbpunkt über dem Auswahlfeld zur Linie passt.
    charts.push(lineChart($('chBat'), bat, {unit: 'mV', colorVar: COLORS[main ? main.i : 0], dash: main ? SHAPES[main.i].dash : null, shape: main ? SHAPES[main.i].shape : 'circle',
      label: main ? main.n.name : 'Akku in Prozent',
      rightAxis: mvLabels, yMin: 3000, yMax: 4200, yTicks: mvTicks, xMin, xMax,
      yLabels: pct.map(p => `${p} %`), extra: mv => `≈ ${batteryPct(mv)} %`,
      ariaLabel: `Akku-Verlauf ${act.map(a => a.n.name).join(', ')}`,
      extraSeries: extra.map(a => ({points: points(a.k, 'mv'), colorVar: COLORS[a.i], dash: SHAPES[a.i].dash, shape: SHAPES[a.i].shape, label: a.n.name})),
      threshold: main ? {y: main.n.low_mv, label: `Warnschwelle ${batteryPct(main.n.low_mv)} % (≈ ${main.n.low_mv} mV)`} : null}));
    const allTemp = act.flatMap(a => points(a.k, 'temp').map(p => p.y));
    const [tLo, tHi] = tempRange(allTemp);
    charts.push(lineChart($('chTemp'), tmp, {unit: '°C', decimals: 1, colorVar: COLORS[main ? main.i : 0], dash: main ? SHAPES[main.i].dash : null, shape: main ? SHAPES[main.i].shape : 'circle',
      label: main ? main.n.name : 'Temperatur',
      ariaLabel: `Temperatur-Verlauf ${act.map(a => a.n.name).join(', ')}`,
      yMin: tLo, yMax: tHi, yTicks: tempTicks(tLo, tHi), xMin, xMax,
      rightPad: rightMargin(mvLabels),     // gleicher Rand wie beim Akku-Diagramm -> x-Achsen deckungsgleich
      extraSeries: extra.map(a => ({points: points(a.k, 'temp'), colorVar: COLORS[a.i], dash: SHAPES[a.i].dash, shape: SHAPES[a.i].shape, label: a.n.name}))}));
  }

  // Tabelle: bei mehreren Repeatern je einer über Reiter (mit demselben Linienmuster wie im Diagramm), nicht alle gemischt.
  const tableMain = () => { const act = active(); return act.find(a => a.i === tableSlot) || act[0]; };
  function drawTable() {
    const act = active(), main = tableMain(), rows = main ? (rowsBy[main.k] || []) : [];
    $('tableTabs').innerHTML = act.length > 1 ? act.map(a =>
      `<button type="button" data-tab="${a.i}" class="${a.i === main.i ? 'on' : ''}">${sampleSvg(a.i)} ${esc(a.n.name)}</button>`).join('') : '';
    $('tableFor').textContent = main ? `Messwerte von ${main.n.name}` : '';
    updateCsv();
    $('rows').innerHTML = rows.length ? [...rows].reverse().map(r => `<tr>
      <td>${esc(fmtDateTime(r.ts))}</td>
      <td>${r.mv == null ? '<span class="muted">–</span>' : r.mv}</td>
      <td>${r.mv == null ? '<span class="muted">–</span>' : batteryPct(r.mv) + ' %'}</td>
      <td>${r.temp == null ? '<span class="muted">–</span>' : r.temp.toFixed(1).replace('.', ',')}</td></tr>`).join('')
      : '<tr><td colspan="4" class="muted">Noch keine Messwerte in diesem Zeitraum.</td></tr>';
  }

  // CSV: in der Tabelle der gewählte Reiter, sonst der erste Repeater
  function updateCsv() {
    const m = view === 'table' ? tableMain() : active()[0];
    $('csv').href = m ? `/api/battery/${m.k}/export.csv?hours=${hours}` : '#';
  }
  $('tableTabs').onclick = e => {
    const b = e.target.closest('button[data-tab]');
    if (!b) return;
    tableSlot = Number(b.dataset.tab);
    drawTable();
  };

  function showView() {
    updateCsv();
    const any = active().length > 0;
    $('chartView').hidden = view !== 'chart' || !any;
    $('tableView').hidden = view !== 'table' || !any;
    root.querySelectorAll('#views button').forEach(b => b.classList.toggle('on', b.dataset.view === view));
    if (view === 'chart') drawCharts(); else drawTable();
  }

  // Unter jedem Auswahlfeld: letzter Wert dieses Repeaters (oder Status/Fehler der laufenden Abfrage).
  function describeSlots() {
    for (let i = 0; i < SLOTS; i++) {
      const el = root.querySelector(`[data-val="${i}"]`);
      if (!el) continue;
      const n = sel[i] ? nodeOf(sel[i]) : null;
      el.classList.toggle('err', !!(busy[i] && busy[i].err));
      if (busy[i]) { el.textContent = busy[i].text; continue; }
      if (!n) { el.textContent = ''; continue; }
      const b = n.battery, t = n.temperature;
      el.textContent = [
        b ? `${b.mv} mV ≈ ${batteryPct(b.mv)} % (${ago(b.ts)})` : 'Noch kein Akkuwert',
        t ? `${t.celsius.toFixed(1).replace('.', ',')} °C (${ago(t.ts)})` : 'keine Temperatur gespeichert',
      ].join(' · ');
    }
  }

  function renderSlots() {
    const host = $('slots');
    if (!host.children.length) {
      host.innerHTML = Array.from({length: SLOTS}, (_, i) => `
        <div class="slot">
          <label class="field" style="margin-bottom:2px"><span>${sampleSvg(i)} Repeater ${i + 1}</span>
            <select data-slot="${i}"></select></label>
          <p class="slot-val small muted" data-val="${i}"></p>
        </div>`).join('');
    }
    for (let i = 0; i < SLOTS; i++) {
      host.querySelector(`select[data-slot="${i}"]`).innerHTML = '<option value="">— keiner —</option>' + nodes.map(n =>
        `<option value="${esc(n.public_key)}" ${n.public_key === sel[i] ? 'selected' : ''}>${esc(n.name)}</option>`).join('');
    }
  }

  // Ladezähler: eine ältere, später eintreffende Antwort (anderer Zeitraum/Repeater) überschreibt nie die neuere.
  let loadSeq = 0;
  async function loadData() {
    const act = active(), my = ++loadSeq, h = hours;
    const stale = () => !alive || my !== loadSeq;
    try {
      const fresh = {};
      for (const a of act) { fresh[a.k] = (await get(`/api/battery/${a.k}?hours=${h}`)).rows; if (stale()) return; }
      rowsBy = fresh;
      $('error').textContent = '';
    } catch (e) { if (stale()) return; $('error').textContent = e.message; rowsBy = {}; }
    showView();
  }

  async function loadNodes() {
    try {
      const d = await get('/api/battery/nodes');
      if (!alive) return;
      nodes = d.nodes;
      if (typeof d.poll_gap_s === 'number') pollGapS = d.poll_gap_s;
      if (!selectionRestored) {          // gespeicherte Auswahl wiederherstellen (bleibt, bis sie geändert wird)
        selectionRestored = true;
        sel = Array.from({length: SLOTS}, (_, i) => (d.selection || [])[i] || null);
      }
      $('error').textContent = '';
    } catch (e) { if (alive) $('error').textContent = e.message; return; }
    $('empty').hidden = nodes.length > 0;
    $('controls').hidden = nodes.length === 0;
    // Nur noch überwachte Repeater, jeder höchstens einmal; ohne Auswahl wird der erste Repeater vorgewählt.
    sel = sel.map((k, i) => k && nodeOf(k) && sel.indexOf(k) === i ? k : null);
    if (!sel.some(Boolean) && nodes.length) sel[0] = nodes[0].public_key;
    renderSlots();
    describeSlots();
    await loadData();
  }

  // Die Auswahl wird beim Ändern sofort auf dem Server gespeichert und gilt dann auf allen Geräten, bis sie wieder geändert wird.
  async function saveSelection() {
    try { await put('/api/battery/selection', {keys: sel}); }
    catch (e) { $('error').textContent = `Auswahl konnte nicht gespeichert werden: ${e.message}`; }
  }
  $('slots').onchange = async e => {
    const el = e.target.closest('select[data-slot]');
    if (!el) return;
    const i = Number(el.dataset.slot), v = el.value || null;
    sel[i] = v;
    if (v) sel = sel.map((k, j) => (j !== i && k === v ? null : k));      // derselbe Repeater nur auf einem Platz
    busy.fill(null);
    renderSlots(); describeSlots();
    saveSelection();
    await loadData();
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

  const sleep = ms => new Promise(r => setTimeout(r, ms));
  // „Jetzt abfragen“: alle gewählten Repeater nacheinander, mit Pause dazwischen (Standard 20 s). Mehrere Funkanfragen
  // direkt hintereinander belasten die Stromversorgung des Pi (Spannungsabfall beim Senden). Der Wert jedes Repeaters
  // erscheint unter seinem Auswahlfeld, sobald er abgefragt ist.
  $('poll').onclick = async () => {
    const btn = $('poll');
    const act = active();
    if (!act.length) return;
    btn.disabled = true;
    busy.fill(null);
    act.forEach((a, n) => { busy[a.i] = {text: n === 0 ? 'wird abgefragt …' : 'wartet auf die Abfrage …'}; });
    describeSlots();
    for (let n = 0; n < act.length && alive; n++) {
      const a = act[n];
      if (n > 0) {
        for (let left = Math.ceil(pollGapS); left > 0 && alive; left--) {
          btn.textContent = `warte ${left} s …`;
          busy[a.i] = {text: `wird in ${left} s abgefragt …`}; describeSlots();
          await sleep(1000);
        }
        if (!alive) break;
      }
      btn.textContent = act.length > 1 ? `frage ${a.n.name} ab …` : 'frage ab …';
      busy[a.i] = {text: 'wird abgefragt …'}; describeSlots();
      try { await post(`/api/nodes/${a.k}/poll`, {temperature: true}); busy[a.i] = null; }
      catch (e) { busy[a.i] = {text: `Abfrage fehlgeschlagen: ${e.message}`, err: true}; }
      if (!alive) break;
      await loadNodes();                         // neuen Wert dieses Repeaters sofort unter seinem Auswahlfeld zeigen
    }
    if (!alive) return;
    btn.disabled = false; btn.textContent = 'Jetzt abfragen';
  };

  const off = ctx.on('node_update', async ev => {
    if (!alive || !sel.includes(ev.payload?.public_key) || $('poll').disabled) return;       // während „Jetzt abfragen“ aktualisiert die Schleife selbst
    await loadNodes();
  });
  await loadNodes();

  return () => { alive = false; charts.forEach(c => c.destroy()); if (typeof off === 'function') off(); };
}
