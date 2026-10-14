// Mein Node: Einstellungen des eigenen Nodes wie in der MeshCore-App. Felder und Wertebereiche kommen vom Server
// (/api/node/spec); der Server prüft jede Änderung selbst und verlangt bei riskanten Änderungen eine Bestätigung (HTTP 409).
import {get, post, put} from '../lib/api.js';
import {esc, fmtDateTime} from '../lib/util.js';

const RADIO = ['radio_freq', 'radio_bw', 'radio_sf', 'radio_cr'];

function control(f, value) {
  const k = esc(f.key);
  if (f.kind === 'bool') return `<label class="toggle"><input type="checkbox" data-key="${k}" ${value ? 'checked' : ''}> an</label>`;
  if (f.kind === 'choice') {
    // Passt der Wert vom Node zu keiner Auswahl (z. B. Bandbreite 41.667 statt 41.7, oder null), zeigt der Browser sonst
    // stillschweigend die ERSTE Option – und „Speichern“ hätte diesen falschen Wert geschrieben (bei Funkparametern: kein Empfang mehr).
    // Deshalb dann eine eigene Option „unbekannt“ mit leerem Wert; leer gilt beim Speichern nie als Änderung.
    const known = f.choices.some(c => String(c) === String(value));
    const unknown = known ? '' : `<option value="" selected>unbekannt${value === null || value === undefined || value === '' ? '' : ` (${esc(value)})`} – nicht ändern</option>`;
    return `<select data-key="${k}">${unknown}${f.choices.map(c => {
      const lab = f.labels ? `${c} – ${f.labels[c] ?? ''}` : String(c);
      return `<option value="${esc(c)}" ${String(c) === String(value) ? 'selected' : ''}>${esc(lab)}</option>`;
    }).join('')}</select>`;
  }
  const num = f.kind === 'float' || f.kind === 'int';
  return `<input data-key="${k}" ${num ? 'inputmode="decimal"' : 'maxlength="31"'} value="${esc(value ?? '')}" autocomplete="off">`;
}

export async function mount(root, ctx) {
  let alive = true;
  root.innerHTML = `
    <section class="card">
      <h2 style="margin-top:0">Mein Node</h2>
      <p id="mnMsg" class="small"></p>
      <div id="mnBody"><p class="muted">lade …</p></div>
    </section>`;
  const msg = (t, cls = '') => { const el = root.querySelector('#mnMsg'); if (el) { el.className = 'small ' + cls; el.textContent = t || ''; } };

  let spec, data;
  async function load() {
    [spec, data] = await Promise.all([spec || get('/api/node/spec'), get('/api/node/settings')]);
  }
  try { await load(); } catch (e) {
    root.querySelector('#mnBody').innerHTML = `<p class="err">${esc(e.message)}</p><p class="muted small">Ohne Verbindung zum Node gibt es hier nichts zu ändern.</p>`;
    return () => { alive = false; };
  }

  async function withConfirm(send) {
    try { return await send(false); }
    catch (e) {
      if (e.status === 409 && confirm(`${e.message.replace(/^Bestätigung nötig: /, '')}\n\nWirklich ausführen?`)) return send(true);
      throw e;
    }
  }

  function draw() {
    const s = data.settings;
    const groups = [];
    for (const f of spec.fields) {
      let g = groups.find(x => x.name === f.group);
      if (!g) groups.push(g = {name: f.group, fields: []});
      g.fields.push(f);
    }
    const diff = data.time_diff_s;
    const off = diff == null ? '' : Math.abs(diff) < 5 ? 'Uhr stimmt (±5 s).' : `Der Node geht ${Math.abs(diff)} s ${diff > 0 ? 'vor' : 'nach'}.`;
    root.querySelector('#mnBody').innerHTML = `
      <p class="muted small">Firmware ${esc(s.firmware || '?')} · Schlüssel ${esc((s.public_key || '').slice(0, 12))}…
        · max. Sendeleistung ${esc(s.max_tx_power ?? '?')} dBm</p>
      ${groups.map(g => `
        <div class="setgroup">${esc(g.name)}</div>
        <div data-group="${esc(g.name)}">
          ${g.fields.map(f => `<div class="setrow"><div>${esc(f.label)}${f.hint ? ` <span class="muted small">${esc(f.hint)}</span>` : ''}</div>
            <div class="setctl">${control(f, s[f.key])}</div></div>`).join('')}
          ${g.name === 'Standort' ? '<div class="row"><button class="small secondary" id="mnGeo">Standort vom Browser übernehmen</button></div>' : ''}
          ${g.name === 'Funk' ? `<p class="muted small">Die Funkwerte müssen zu deinem Netz passen (Region, Voreinstellung der örtlichen Gruppe).
            Übernimm sie aus der MeshCore-App oder von deiner Gruppe; hier sind keine Voreinstellungen eingebaut.</p>` : ''}
          <div class="row"><button class="small" data-save="${esc(g.name)}">${esc(g.name)} speichern</button></div>
        </div>`).join('')}
      <div class="setgroup">Uhr</div>
      <div class="setrow"><div>Node: ${s.node_time ? esc(fmtDateTime(s.node_time)) : '?'} · Pi/Rechner: ${esc(fmtDateTime(data.pi_time))}
        <span class="muted small">${esc(off)}</span></div>
        <div class="setctl"><button class="small secondary" id="mnTime">Uhr mit diesem Rechner abgleichen</button></div>
        <p class="muted small">Der Pi braucht dafür selbst die richtige Zeit (Internet/NTP; ohne Netz kann sie abweichen).</p></div>
      <div class="setgroup">Sicherung der Einstellungen</div>
      <div class="setrow"><div>Speichert Node-Einstellungen, Kanäle und Automatik-Regeln in einer Datei. Kein Privatschlüssel, keine Passwörter, kein Verlauf.</div>
        <label class="toggle small"><input type="checkbox" id="bkKeys"> Kanalschlüssel mit speichern (Datei dann wie ein Passwort behandeln)</label>
        <div class="setctl"><a id="bkDownload" class="small" href="/api/backup/export" download>Sicherung herunterladen</a></div></div>
      <div class="setrow"><div>Wiederherstellen aus einer Sicherungsdatei</div>
        <div class="setctl"><input type="file" id="bkFile" accept="application/json,.json"></div>
        <div id="bkPlan" class="small"></div></div>
      <div class="setgroup">Neustart</div>
      <div class="setrow"><div class="setctl"><button class="small secondary" id="mnReboot">Node neu starten</button></div>
        <p class="muted small">Die Verbindung wird danach automatisch wieder aufgebaut. Privatschlüssel und Werkseinstellung gibt es hier bewusst nicht.</p></div>`;

    const val = f => {
      const el = root.querySelector(`[data-key="${f.key}"]`);
      return f.kind === 'bool' ? el.checked : el.value;
    };
    root.querySelectorAll('[data-save]').forEach(b => b.onclick = () => {
      const name = b.dataset.save;
      const fields = spec.fields.filter(f => f.group === name);
      const changes = {};
      for (const f of fields) {
        const v = val(f);
        const cur = s[f.key];
        if (v === '' && f.kind !== 'text') continue;          // leer (unbekannt/nicht ausgefüllt) ist nie eine Änderung
        const same = f.kind === 'bool' ? !!cur === v : (f.kind === 'text' ? String(cur ?? '') === v : String(cur) === String(v) || (v !== '' && Number(v.replace(',', '.')) === Number(cur)));
        if (!same) changes[f.key] = v;
      }
      if (name === 'Funk' && RADIO.some(k => k in changes)) for (const k of RADIO) changes[k] = val(spec.fields.find(f => f.key === k));
      if (name === 'Standort' && ('lat' in changes || 'lon' in changes)) for (const k of ['lat', 'lon']) changes[k] = val(spec.fields.find(f => f.key === k));
      if (!Object.keys(changes).length) { msg('Nichts geändert.'); return; }
      run(async () => {
        data = await withConfirm(c => put('/api/node/settings', {changes, confirm: c}));
        msg(`${name}: gespeichert.`, 'ok'); draw();
      }, `${name} wird gespeichert …`);
    });
    const geo = root.querySelector('#mnGeo');
    if (geo) geo.onclick = () => {
      if (!navigator.geolocation) { msg('Der Browser kann keinen Standort liefern (nur über https oder localhost).', 'err'); return; }
      navigator.geolocation.getCurrentPosition(p => {
        root.querySelector('[data-key="lat"]').value = p.coords.latitude.toFixed(5);
        root.querySelector('[data-key="lon"]').value = p.coords.longitude.toFixed(5);
        msg('Standort eingetragen – noch „Standort speichern“ drücken.');
      }, () => msg('Standort nicht verfügbar (Erlaubnis oder https fehlt).', 'err'));
    };
    const keysBox = root.querySelector('#bkKeys');
    keysBox.onchange = () => { root.querySelector('#bkDownload').href = '/api/backup/export' + (keysBox.checked ? '?channel_keys=true' : ''); };
    root.querySelector('#bkFile').onchange = ev => run(async () => {
      const file = ev.target.files[0];
      if (!file) return;
      if (file.size > 1e6) throw new Error('Datei ist zu groß.');
      let data;
      try { data = JSON.parse(await file.text()); } catch { throw new Error('Das ist keine gültige Sicherungsdatei.'); }
      const {plan} = await post('/api/backup/restore', {data, dry_run: true});
      const lines = [];
      if (plan.node) lines.push(`<label class="toggle"><input type="checkbox" data-sec="node" checked> Node-Einstellungen: ${Object.keys(plan.node).length ? esc(Object.keys(plan.node).join(', ')) + ' ändern' : 'schon gleich'}</label>`);
      lines.push(`<label class="toggle"><input type="checkbox" data-sec="channels" checked> Kanäle: ${plan.channels.map(c => `${esc(c.name)} (${{add: 'neu', same: 'gleich', conflict: 'Platz belegt', skip: 'ohne Schlüssel, übersprungen'}[c.action]})`).join(', ') || 'keine'}</label>`);
      lines.push(`<label class="toggle"><input type="checkbox" data-sec="rules" checked> Regeln: ${plan.rules.filter(r => r.action === 'add').length} neu, ${plan.rules.filter(r => r.action === 'same').length} schon vorhanden</label>`);
      lines.push('<label class="toggle"><input type="checkbox" id="bkOver"> belegte Kanalplätze überschreiben</label>');
      root.querySelector('#bkPlan').innerHTML = lines.join('<br>') + '<div class="row"><button class="small" id="bkGo">Jetzt wiederherstellen</button></div>';
      root.querySelector('#bkGo').onclick = () => run(async () => {
        const sections = [...root.querySelectorAll('[data-sec]')].filter(x => x.checked).map(x => x.dataset.sec);
        const res = await withConfirm(c => post('/api/backup/restore', {data, sections, confirm: c, overwrite_channels: root.querySelector('#bkOver').checked}));
        msg(res.report.join(' · '), 'ok');
        data = null; await load(); draw();
      }, 'Wird wiederhergestellt …');
      msg('Prüfung ok. Wähle aus, was zurückgespielt werden soll.');
    }, 'Datei wird geprüft …');
    root.querySelector('#mnTime').onclick = () => run(async () => {
      data = await post('/api/node/time-sync', {}); msg('Uhr abgeglichen.', 'ok'); draw();
    }, 'Uhr wird abgeglichen …');
    root.querySelector('#mnReboot').onclick = () => run(async () => {
      await withConfirm(c => post('/api/node/reboot', {confirm: c}));
      msg('Neustart ausgelöst. Die Verbindung kommt gleich von selbst zurück.', 'ok');
    }, 'Neustart …');
  }

  let busy = false;
  // Nur die Knöpfe DIESER Seite sperren/freigeben: `root` ist der gemeinsame Seitenbereich. Wechselt man während einer
  // laufenden Aktion die Seite, hätte das Freigeben sonst die (gesperrten) Knöpfe der neuen Seite wieder aktiviert.
  const page = root.querySelector('section.card');
  async function run(fn, working) {
    if (busy || !alive) return;
    busy = true; msg(working);
    page.querySelectorAll('button').forEach(b => b.disabled = true);
    try { await fn(); } catch (e) { if (alive) msg(e.message, 'err'); }
    finally { busy = false; if (alive) page.querySelectorAll('button').forEach(b => b.disabled = false); }
  }

  draw();
  return () => { alive = false; };
}
