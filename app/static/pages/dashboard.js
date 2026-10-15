import {get, post, del} from '../lib/api.js';
import {esc, fmtTime, batteryPct} from '../lib/util.js';

export async function mount(root, ctx) {
  root.innerHTML = `
    <section class="card">
      <h2>Node</h2>
      <dl id="status"><dt>Status</dt><dd>lade …</dd></dl>
      <p id="error" class="err"></p>
      <div class="row">
        <button id="advert">Advert senden</button>
        <button id="advertFlood" class="secondary">Advert (Flood)</button>
      </div>
      <p id="advertResult" class="small muted" role="status" aria-live="polite"></p>
    </section>
    <section class="card" id="usbCard" hidden>
      <h2>USB-Node</h2>
      <p id="usbState"></p>
      <dl id="usbInfo"></dl>
      <p id="usbHint" class="err"></p>
      <div class="row" id="usbSim" hidden>
        <button class="secondary" data-sim="unplug">Kabel abziehen</button>
        <button class="secondary" data-sim="plug">Kabel einstecken</button>
        <button class="secondary" data-sim="hang">Node hängt</button>
        <button class="secondary" data-sim="unhang">Node antwortet wieder</button>
      </div>
      <details><summary>Verlauf</summary><div id="usbEvents" class="muted small"></div></details>
    </section>
    <section class="card" id="maintCard">
      <h2>Wartung</h2>
      <p id="maintState" class="small muted"></p>
      <div class="row" id="maintBtns">
        <button class="secondary" data-m="60">1 Stunde</button><button class="secondary" data-m="120">2 Stunden</button>
        <button class="secondary" data-m="240">4 Stunden</button><button id="maintEnd" class="secondary" hidden>Wartung beenden</button>
      </div>
      <p class="muted small">Gibt den USB-Anschluss frei (z. B. um den Node in der MeshCore-App zu benutzen), pausiert Senden, Raum und Automatik und schweigt bei Ausfällen.
        Endet nach Ablauf von selbst, höchstens 24 Stunden.</p>
    </section>
    <section class="card" id="peerCard" hidden>
      <h2>Andere Stationen</h2>
      <div id="peerList" class="small"></div>
    </section>
    <section class="card">
      <h2>Live-Log</h2>
      <div id="log" class="log"></div>
    </section>`;

  const $ = id => root.querySelector('#' + id);
  let alive = true;

  function renderStatus(s) {
    if (!s || !alive) return;
    const rows = [
      ['Verbindung', s.connected ? 'verbunden' : 'getrennt'],
      ['Typ', s.kind], ['Name', s.name], ['Modell', s.model], ['Firmware', s.firmware],
      ['Batterie', s.battery_mv != null ? `${s.battery_mv} mV · ≈ ${batteryPct(s.battery_mv)} %` : null],
      ['Public Key', s.public_key],
    ].filter(r => r[1] != null && r[1] !== '');
    $('status').innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
    $('error').textContent = s.last_error || '';
  }

  const STATE_ICON = {ok: '●', no_device: '○', no_response: '◐', no_permission: '⚠', unknown: '·'};
  function pct(a) { return a ? `${a.percent.toFixed(1)} % (${a.outages} Ausfälle)` : 'noch keine Daten'; }
  async function renderUsb() {
    let u;
    try { u = await get('/api/usb'); } catch { return; }
    if (!alive) return;
    $('usbCard').hidden = !u.enabled;
    if (!u.enabled) return;
    $('usbState').innerHTML = `<strong class="${u.state === 'ok' ? 'ok' : 'err'}">${STATE_ICON[u.state] || ''} ${esc(u.label)}</strong>`;
    const rows = [
      ['Anschluss', u.port || (u.ports[0] && u.ports[0].device) || '–'],
      ['Seit', u.since ? fmtTime(u.since) : '–'],
      ['Verfügbarkeit 24 h', pct(u.availability['24h'])],
      ['Verfügbarkeit 7 Tage', pct(u.availability['7d'])],
      ['Gefundene Geräte', u.ports.length ? u.ports.map(p => `${p.device}${p.description ? ' (' + p.description + ')' : ''}`).join(', ') : 'keine'],
      ['Meldung bei Ausfall', `nach ${u.alert_after_s} s`],
    ];
    $('usbInfo').innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
    $('usbHint').textContent = u.hint || (u.state === 'ok' ? '' : u.detail || '');
    $('usbSim').hidden = u.mode !== 'sim';
    $('usbEvents').innerHTML = u.events.length
      ? u.events.map(e => `<div>${esc(fmtTime(e.ts))} – ${esc(e.up ? 'ok' : e.state)}: ${esc(e.text)}</div>`).join('')
      : 'Noch keine Wechsel aufgezeichnet.';
  }
  root.querySelectorAll('#usbSim button').forEach(b => action(b, async () => {
    const body = {unplug: {plugged: false}, plug: {plugged: true, hung: false}, hang: {hung: true}, unhang: {hung: false}}[b.dataset.sim];
    await post('/api/sim/usb', body);
    setTimeout(renderUsb, 3000);
  }));

  function logLine(ev) {
    const el = $('log');
    if (!el) return;
    const p = ev.payload || {};
    const text = p.text ? `${p.name || p.pubkey_prefix || ''}: ${p.text}` : JSON.stringify(p);
    const div = document.createElement('div');
    div.innerHTML = `<span class="t">${fmtTime(ev.ts)}</span> <span class="k">${esc(ev.type)}</span> ${esc(text)}`;
    el.appendChild(div);
    while (el.children.length > 300) el.removeChild(el.firstChild);
    el.scrollTop = el.scrollHeight;
  }

  // Rückmeldung unter den Knöpfen: „sende …“, dann Erfolg mit Uhrzeit (vorher kam bei Erfolg gar nichts, nur Fehler wurden gezeigt)
  function action(btn, fn, okText) {
    btn.addEventListener('click', async () => {
      const out = $('advertResult');
      btn.disabled = true;
      out.className = 'small muted'; out.textContent = 'sende …';
      try {
        await fn();
        if (!alive) return;
        $('error').textContent = '';
        out.className = 'small ok';
        out.textContent = `${okText} (${new Date().toLocaleTimeString('de-DE', {hour: '2-digit', minute: '2-digit', second: '2-digit'})})`;
      } catch (e) {
        if (!alive) return;
        out.textContent = '';
        $('error').textContent = e.message;
      } finally { if (alive) btn.disabled = false; }
    });
  }
  action($('advert'), () => post('/api/advert', {flood: false}), 'Advert gesendet (direkte Nachbarn)');
  action($('advertFlood'), () => post('/api/advert', {flood: true}), 'Advert gesendet (Flood, netzweit)');

  async function renderMaint() {
    try {
      const m = await get('/api/maintenance');
      if (!alive) return;
      $('maintState').textContent = m.active ? `Wartung läuft noch ${Math.ceil(m.remaining_s / 60)} min.` : 'Keine Wartung aktiv.';
      $('maintEnd').hidden = !m.active;
    } catch (e) { if (alive) $('maintState').textContent = e.message; }
  }
  let maintBusy = false;                                       // Doppelklick: nicht zweimal schalten
  $('maintBtns').onclick = async e => {
    const b = e.target.closest('button'); if (!b || maintBusy) return;
    maintBusy = true;
    try { if (b.id === 'maintEnd') await del('/api/maintenance'); else await post('/api/maintenance', {minutes: Number(b.dataset.m)}); } catch (err) { if (alive) $('error').textContent = err.message; }
    finally { maintBusy = false; }
    if (!alive) return;
    renderMaint(); ctx.refreshStatus();
  };
  async function renderPeers() {
    try {
      const {peers} = await get('/api/peers');
      if (!alive) return;
      $('peerCard').hidden = !peers.length;
      $('peerList').innerHTML = peers.map(p => {
        const sm = p.summary || {};
        const state = p.ok == null ? 'noch nicht abgefragt' : p.ok ? (sm.node_connected ? 'erreichbar, Node verbunden' : 'erreichbar, aber Node nicht verbunden') : `nicht erreichbar (${esc(p.error)})`;
        const warn = p.ok === false || (p.ok && !sm.node_connected);
        return `<div class="setrow"><a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(sm.station || p.name)}</a>
          <span class="${warn ? 'err' : 'muted'}"> · ${state}</span>${sm.usb && sm.usb.enabled && sm.usb.state !== 'ok' ? ` <span class="err">· USB: ${esc(sm.usb.label)}</span>` : ''}</div>`;
      }).join('');
    } catch { /* ohne Stationen oder nicht erreichbar: Karte bleibt aus */ }
  }
  const maintTimer = setInterval(renderMaint, 30000);
  renderMaint();
  const peerTimer = setInterval(renderPeers, 30000);
  renderPeers();
  const usbTimer = setInterval(renderUsb, 10000);
  renderUsb();
  ctx.history.forEach(logLine);
  renderStatus(ctx.state.status);
  const offs = [
    ctx.on('*', ev => { if (!ev.type.startsWith('_')) logLine(ev); }),
    ctx.on('_status', ev => renderStatus(ev.status)),
    ctx.on('usb_state', () => renderUsb()),
    () => clearInterval(usbTimer),
    () => clearInterval(peerTimer),
    () => clearInterval(maintTimer),
    ctx.on('maintenance', () => renderMaint()),
  ];
  get('/api/status').then(renderStatus).catch(e => { if (alive) $('error').textContent = e.message; });

  return () => { alive = false; offs.forEach(off => off()); };
}
