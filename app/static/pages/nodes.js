import {get, post} from '../lib/api.js';
import {mountRepeaterPanel} from './repeater_admin.js';
import {esc, ago, typeLabel, TYPE_COLOR, pathLabel, batteryPct, sparkline, fmtDateTime, debounce, matchesNameOrKey} from '../lib/util.js';

const LOW_MV = 3500;   // reine Anzeige; die Warnschwelle der Automatik steht in der Regel

export async function mount(root, ctx) {
  root.innerHTML = `
    <section class="card">
      <div class="row between">
        <h2 style="margin:0">Knoten <span id="count" class="muted"></span></h2>
        <label class="toggle small"><input type="checkbox" id="showClients"> auch Clients zeigen</label>
      </div>
      <p class="muted small">Repeater, Rooms und Sensoren mit Akkustand und Erreichbarkeit.
        „Abfragen“ holt den aktuellen Status und sendet dafür eine Funkanfrage.</p>
      <div class="row" style="margin:10px 0"><input id="search" type="search" placeholder="Name oder Hex-Kennung …"></div>
      <p id="error" class="err"></p>
      <p id="result" role="status" aria-live="polite" hidden></p>
      <div style="overflow-x:auto"><table>
        <thead><tr><th>Name</th><th>Typ</th><th>Akku</th><th class="hide-sm">Erreichbarkeit</th><th class="hide-sm">Pfad</th><th></th></tr></thead>
        <tbody id="rows"><tr><td colspan="6" class="muted">lade …</td></tr></tbody>
      </table></div>
    </section>
    <section class="card" id="repCard" hidden></section>
    <section class="card" id="histCard" hidden>
      <div class="row between"><h2 id="histTitle" style="margin:0"></h2><button id="histClose" class="small secondary">Schließen</button></div>
      <h3>Akku (7 Tage)</h3><div id="histBat"></div>
      <h3>Erreichbarkeit</h3><div id="histPing"></div>
    </section>`;

  const $ = id => root.querySelector('#' + id);
  let nodes = [];
  let showClients = false;
  let histKey = null;
  let closePanel = null;
  let alive = true;

  function batteryCell(n) {
    if (!n.battery) return '<span class="muted">–</span>';
    const mv = n.battery.mv, low = mv < LOW_MV;
    return `<span class="bar ${low ? 'low' : ''}" title="${mv} mV ≈ ${batteryPct(mv)} %"><span style="width:${batteryPct(mv)}%"></span></span>
      <span class="${low ? 'err' : ''}">${mv} mV · ≈ ${batteryPct(mv)} %</span><br><span class="muted small">${esc(ago(n.battery.ts))}</span>`;
  }

  function pingCell(n) {
    const p = n.last_ping;
    if (!p) return '<span class="muted">noch nicht geprüft</span>';
    const rtt = p.ok && p.rtt_ms != null ? ` · ${(p.rtt_ms / 1000).toFixed(1)} s` : '';
    const snr = p.ok && p.snr_out != null && p.snr_in != null
      ? `<br><span class="small">SNR hin ${p.snr_out} dB · zurück ${p.snr_in} dB</span>` : '';
    return `<span class="${p.ok ? 'ok' : 'err'}">${p.ok ? 'erreichbar' : 'keine Antwort'}${rtt}</span>${snr}
      <br><span class="muted small">${esc(ago(p.ts))}</span>`;
  }

  // Ergebniszeile über der Tabelle (auch auf dem Handy sichtbar, die Spalte „Erreichbarkeit“ ist dort ausgeblendet).
  function showResult(kind, text) {
    const r = $('result');
    r.className = kind === 'ok' ? 'ok' : kind === 'bad' ? 'err' : 'muted';
    r.textContent = text;
    r.hidden = !text;
  }
  const secs = ms => `${(ms / 1000).toFixed(1).replace('.', ',')} s`;
  function pingText(name, p) {
    if (!p.ok) return `✗ Ping ${name}: keine Antwort`;
    const snr = p.snr_out != null && p.snr_in != null ? ` · SNR hin ${p.snr_out} dB / zurück ${p.snr_in} dB` : '';
    return `✓ Ping ${name}: Antwort erhalten${p.rtt_ms != null ? ` nach ${secs(p.rtt_ms)}` : ''}${snr}`;
  }

  function render() {
    const q = $('search').value.trim().toLowerCase();
    const list = nodes.filter(n => (showClients || n.type_name !== 'client')
      && matchesNameOrKey(n, q));
    $('count').textContent = `(${list.length})`;
    $('rows').innerHTML = list.length ? list.map(n => {
      const monitorable = ['repeater', 'room'].includes(n.type_name);
      return `<tr data-key="${esc(n.public_key)}">
        <td><strong>${esc(n.name)}</strong><br><span class="muted small">${esc(n.public_key.slice(0, 12))}…</span></td>
        <td><span class="chip" style="background:${TYPE_COLOR[n.type_name] || '#6b7686'}">${esc(typeLabel(n.type_name))}</span></td>
        <td>${batteryCell(n)}</td>
        <td class="hide-sm">${pingCell(n)}</td>
        <td class="hide-sm">${esc(pathLabel(n.path_len))}</td>
        <td><div class="actions">
          ${monitorable ? '<button class="small" data-act="poll">Abfragen</button>' : ''}
          <button class="small secondary" data-act="ping">Ping</button>
          <button class="small secondary" data-act="hist">Verlauf</button>
          ${monitorable ? '<button class="small secondary" data-act="manage">Verwalten</button>' : ''}
        </div></td></tr>`;
    }).join('') : '<tr><td colspan="6" class="muted">Keine Knoten bekannt. Sobald Repeater einen Advert senden, erscheinen sie hier.</td></tr>';
  }

  let loadSeq = 0;
  async function load() {
    const my = ++loadSeq;                     // nur die neueste Antwort zählt (ältere, langsamere Abfrage nie darüberschreiben)
    let list, err = '';
    try { list = await get('/api/nodes'); } catch (e) { list = []; err = e.message; }
    if (!alive || my !== loadSeq) return;
    nodes = list;
    $('error').textContent = err;
    render();
  }

  async function showHistory(key) {
    histKey = key;
    const n = nodes.find(x => x.public_key === key);
    $('histCard').hidden = false;
    $('histTitle').textContent = `Verlauf: ${n ? n.name : key.slice(0, 8)}`;
    let h;
    try { h = await get(`/api/nodes/${key}/history?hours=168`); }
    catch (e) { if (alive && histKey === key) $('histPing').textContent = e.message; return; }
    if (!alive || histKey !== key) return;     // inzwischen anderen Knoten gewählt oder geschlossen: veraltete Antwort verwerfen
    $('histBat').innerHTML = sparkline(h.battery.map(b => ({x: b.ts, y: b.mv})), {yMin: 3000, yMax: 4200});
    const ok = h.pings.filter(p => p.ok).length;
    const last = h.pings.slice(-10).reverse().map(p =>
      `<div class="small"><span class="${p.ok ? 'ok' : 'err'}">${p.ok ? '✓' : '✗'}</span> ${esc(fmtDateTime(p.ts))}` +
      `${p.ok && p.rtt_ms != null ? ` · ${(p.rtt_ms / 1000).toFixed(1)} s` : ''}` +
      `${p.ok && p.snr_out != null ? ` · SNR hin ${p.snr_out} / zurück ${p.snr_in} dB` : ''}</div>`).join('');
    $('histPing').innerHTML = h.pings.length
      ? `<p>${ok} von ${h.pings.length} Prüfungen erfolgreich (${Math.round(ok / h.pings.length * 100)} %)</p>${last}`
      : '<p class="muted">Noch keine Prüfungen.</p>';
  }

  $('showClients').onchange = e => { showClients = e.target.checked; render(); };
  $('search').oninput = debounce(render, 150);
  $('histClose').onclick = () => { histKey = null; $('histCard').hidden = true; };

  $('rows').onclick = async e => {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    const key = btn.closest('tr').dataset.key;
    const label = btn.textContent;
    const act = btn.dataset.act;
    const node = nodes.find(x => x.public_key === key);
    const name = node ? node.name : key.slice(0, 8);
    btn.disabled = true;
    btn.textContent = '…';
    if (act === 'ping') showResult('wait', `Ping ${name}: warte auf Antwort … (kann einige Sekunden dauern)`);
    if (act === 'poll') showResult('wait', `Abfrage ${name}: warte auf Antwort … (kann einige Sekunden dauern)`);
    try {
      if (act === 'poll') {
        const st = await post(`/api/nodes/${key}/poll`);
        const mv = st && typeof st.bat === 'number' && st.bat > 0 ? ` · Akku ${st.bat} mV ≈ ${batteryPct(st.bat)} %` : '';
        showResult('ok', `✓ Abfrage ${name}: Antwort erhalten${mv}`);
      }
      if (act === 'ping') {
        const mode = node && ['repeater', 'room'].includes(node.type_name) ? 'trace' : 'message';
        const res = await post(`/api/nodes/${key}/ping`, {mode});
        showResult(res.ok ? 'ok' : 'bad', pingText(name, res));
      }
      if (act === 'hist') await showHistory(key);
      if (act === 'manage') {
        if (closePanel) closePanel();
        const n = nodes.find(x => x.public_key === key);
        closePanel = await mountRepeaterPanel($('repCard'), n, () => { closePanel = null; });
        if (!alive) return;
        $('repCard').scrollIntoView({behavior: 'smooth', block: 'start'});
      }
      if (!alive) return;
      $('error').textContent = '';
      if (!['hist', 'manage'].includes(act)) { await load(); if (histKey === key) await showHistory(key); }
    } catch (err) {
      if (!alive) return;
      if (act === 'ping' || act === 'poll') {
        $('error').textContent = '';
        showResult('bad', `✗ ${act === 'ping' ? 'Ping' : 'Abfrage'} ${name}: ${err.message}`);
      } else $('error').textContent = err.message;
      await load();
    } finally { if (alive) { btn.disabled = false; btn.textContent = label; } }
  };

  await load();
  const offs = [ctx.on('node_update', () => load()), ctx.on('new_contact', load)];
  return () => { alive = false; offs.forEach(off => off()); if (closePanel) closePanel(); };
}
