import {get, post, put, del} from '../lib/api.js';
import {esc, fmtTime, typeLabel, batteryPct, batteryMv, lowMvFromPct, locked} from '../lib/util.js';

const KINDS = {
  battery_poll: 'Akku-Überwachung',
  ping: 'Automatischer Ping',
  channel_ping: 'Kanal-Ping',
  auto_reply: 'Auto-Antwort',
  advert: 'Advert senden',
};

const DEFAULTS = {
  battery_poll: {targets: [], interval_min: 60, low_mv: 3700, temperature: true},
  ping: {targets: [], interval_min: 60, mode: 'trace', text: 'ping'},
  channel_ping: {channel: 0, interval_min: 60, text: 'ping'},
  advert: {interval_min: 360, flood: false},
  auto_reply: {match: 'ping', mode: 'equals', reply: 'pong', cooldown_s: 1800, max_per_hour: 10, direct: true, channels: [], only_from: null},
};

export async function mount(root, ctx) {
  root.innerHTML = `
    <section class="card" id="pauseCard">
      <div class="row between">
        <div><h2 style="margin:0 0 4px">Automatik</h2><div id="pauseInfo"></div></div>
        <button id="pauseBtn"></button>
      </div>
      <p class="muted small">Automatische Sendungen teilen sich das Funkmedium mit allen anderen.
        Darum gibt es ein Stundenlimit, Mindestabstände, Abkühlzeiten und eine Pause-Taste.
        Auto-Antworten beginnen immer mit einer Erwähnung des Absenders („@[Name] …“) und werden nie automatisch beantwortet.
        Pings werden dagegen unverändert gesendet, damit Bots im Netz darauf reagieren.</p>
    </section>
    <section class="card">
      <div class="row between"><h2 style="margin:0">Regeln</h2>
        <div class="row">
          <select id="newKind" aria-label="Regelart">
            <option value="battery_poll">Akku-Überwachung</option>
            <option value="ping">Automatischer Ping</option>
            <option value="channel_ping">Kanal-Ping</option>
            <option value="advert">Advert senden</option>
            <option value="auto_reply">Auto-Antwort</option>
          </select>
          <button id="addRule">Regel hinzufügen</button>
        </div></div>
      <p id="error" class="err"></p>
      <div style="overflow-x:auto"><table><tbody id="rules"><tr><td class="muted">lade …</td></tr></tbody></table></div>
    </section>
    <section class="card"><h2>Protokoll</h2><div id="log" class="log"></div></section>
    <dialog id="dlg">
      <h2 id="dlgTitle"></h2>
      <form id="form" method="dialog"></form>
      <p id="formError" class="err"></p>
      <div class="row"><button id="save">Speichern</button><button id="cancel" class="secondary">Abbrechen</button></div>
    </dialog>`;

  const $ = id => root.querySelector('#' + id);
  let rules = [];
  let contacts = [];
  let channels = [];
  let editing = null;   // {id|null, kind}
  let alive = true;

  // ---- Pause und Budget ---------------------------------------------------
  async function loadState() {
    const s = await get('/api/automation');
    if (!alive) return;
    const b = s.budget;
    $('pauseInfo').innerHTML = s.paused
      ? '<span class="warn"><strong>Pausiert</strong> – es wird nichts automatisch gesendet.</span>'
      : `<span class="ok"><strong>Läuft</strong></span> <span class="muted small">· ${b.used_last_hour} von ${b.max_per_hour} Sendungen in der letzten Stunde · mind. ${b.min_gap_s} s Abstand</span>`;
    $('pauseBtn').textContent = s.paused ? 'Fortsetzen' : 'Pausieren';
    $('pauseBtn').className = s.paused ? '' : 'danger';
    $('pauseBtn').dataset.paused = s.paused ? '1' : '';
    $('log').innerHTML = '';
    s.log.forEach(logLine);
  }
  $('pauseBtn').onclick = locked($('pauseBtn'), async () => {
    try {
      await post('/api/automation/pause', {paused: !$('pauseBtn').dataset.paused});
      await loadState();
    } catch (e) { if (alive) $('error').textContent = e.message; }
  });

  function logLine(e) {
    const el = $('log');
    const ts = e.ts ?? Date.now() / 1000;
    const cls = e.level === 'error' ? 'err' : e.level === 'warn' ? 'warn' : '';
    const div = document.createElement('div');
    div.innerHTML = `<span class="t">${fmtTime(ts)}</span> <span class="${cls}">${esc(e.text)}</span>`;
    el.appendChild(div);
    el.scrollTop = el.scrollHeight;
  }

  // ---- Regelliste -----------------------------------------------------------
  const MODE_LABEL = {trace: 'Direkt-Ping (Zero-Hop)', status: 'Statusabfrage', message: 'Nachricht'};
  const channelName = idx => channels.find(c => c.idx === idx)?.name || `Platz ${idx}`;
  const nameOf = key => contacts.find(c => c.public_key === key)?.name || key.slice(0, 8);

  function summary(r) {
    const c = r.config;
    if (r.kind === 'battery_poll') {
      const t = c.targets === 'all_repeaters' ? 'alle Repeater' : c.targets.map(nameOf).join(', ');
      return `${t} · alle ${c.interval_min} min · Warnung unter ${batteryPct(c.low_mv)} % (≈ ${c.low_mv} mV)${c.temperature ? ' · mit Temperatur' : ''}`;
    }
    if (r.kind === 'ping') {
      return `${c.targets.map(nameOf).join(', ')} · alle ${c.interval_min} min · ${MODE_LABEL[c.mode] || c.mode}`;
    }
    if (r.kind === 'advert') return `alle ${c.interval_min} min · ${c.flood ? 'netzweit (Flood)' : 'nur direkte Nachbarn'}`;
    if (r.kind === 'channel_ping') {
      // Zwei aktive Kanal-Pings in denselben Kanal senden doppelt – darauf hinweisen (nicht verbieten)
      const twins = rules.filter(o => o.id !== r.id && o.enabled && r.enabled && o.kind === 'channel_ping' && o.config.channel === c.channel);
      return `Kanal „${channelName(c.channel)}“ · alle ${c.interval_min} min · Text „${c.text}“` +
        (twins.length ? ` · Achtung: Regel „${twins.map(o => o.name).join('“, „')}“ pingt denselben Kanal, es wird doppelt gesendet` : '');
    }
    const where = [c.direct !== false ? 'Direktnachrichten' : null,
      ...(c.channels || []).map(i => `Kanal „${channelName(i)}“`)].filter(Boolean).join(', ');
    const pause = c.cooldown_s >= 60 ? `${+(c.cooldown_s / 60).toFixed(1)} min` : `${c.cooldown_s} s`;
    return `${where}: bei „${c.match}“ (${c.mode === 'equals' ? 'genau' : 'enthält'}) antworten: „${c.reply}“ · ` +
      `Pause ${pause} je Person · höchstens ${c.max_per_hour ?? 10} Antworten pro 60 min` +
      (c.only_from ? ` · Direktnachrichten nur von ${c.only_from.map(nameOf).join(', ')}` : '');
  }

  function renderRules() {
    $('rules').innerHTML = rules.length ? rules.map(r => `
      <tr data-id="${r.id}">
        <td><strong>${esc(r.name)}</strong> <span class="pill">${esc(KINDS[r.kind])}</span>
          <br><span class="muted small">${esc(summary(r))}</span></td>
        <td><label class="toggle small"><input type="checkbox" data-act="toggle" ${r.enabled ? 'checked' : ''}> aktiv</label></td>
        <td><div class="actions">
          <button class="small secondary" data-act="edit">Bearbeiten</button>
          <button class="small danger" data-act="delete">Löschen</button></div></td>
      </tr>`).join('') : '<tr><td class="muted">Noch keine Regeln. Mit „Regel hinzufügen“ anlegen.</td></tr>';
  }

  async function loadRules() {
    try {
      [rules, contacts, channels] = await Promise.all([
        get('/api/rules'), get('/api/contacts').catch(() => contacts),
        get('/api/channels').then(d => d.channels).catch(() => channels)]);
      if (!alive) return;
      $('error').textContent = '';
    } catch (e) { if (!alive) return; $('error').textContent = e.message; }
    renderRules();
  }

  $('rules').onchange = async e => {
    if (e.target.dataset.act !== 'toggle') return;
    const r = rules.find(x => x.id === Number(e.target.closest('tr').dataset.id));
    if (!r) return;
    e.target.disabled = true;                                   // bis die Antwort da ist: kein zweites Umschalten
    try { await put(`/api/rules/${r.id}`, {...r, enabled: e.target.checked}); await loadRules(); }
    catch (err) { if (alive) $('error').textContent = err.message; await loadRules(); }
  };
  $('rules').onclick = async e => {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    const r = rules.find(x => x.id === Number(btn.closest('tr').dataset.id));
    if (btn.dataset.act === 'edit') openForm(r.kind, r);
    if (btn.dataset.act === 'delete' && confirm(`Regel „${r.name}“ löschen?`)) {
      try { await del(`/api/rules/${r.id}`); await loadRules(); }
      catch (err) { if (alive) $('error').textContent = err.message; }
    }
  };

  // ---- Formular -------------------------------------------------------------
  const checks = (name, list, selected) => {
    if (!list.length) return '<span class="muted small">Keine passenden Kontakte bekannt.</span>';
    const items = list.map(c => `
      <label class="toggle small pick-item" data-q="${esc(c.name.toLowerCase())}">
        <input type="checkbox" name="${name}" value="${esc(c.public_key)}" ${selected.includes(c.public_key) ? 'checked' : ''}>
        ${esc(c.name)} <span class="muted">(${esc(typeLabel(c.type_name))})</span></label>`).join('');
    const search = list.length > 8
      ? `<input type="search" class="pick-search" placeholder="Suchen …" data-for="${name}">` : '';
    return `${search}<div class="pick-list" data-list="${name}">${items}</div>`;
  };

  function formHtml(kind, rule) {
    const c = {...DEFAULTS[kind], ...(rule ? rule.config : {})};
    const nameField = `<label class="field"><span>Name</span>
      <input type="text" name="name" maxlength="60" required value="${esc(rule ? rule.name : KINDS[kind])}"></label>`;
    if (kind === 'battery_poll') {
      const sel = Array.isArray(c.targets) ? c.targets : [];
      return nameField + `
        <div class="field"><span class="muted small">Ziele (bis zu 30; „alle Repeater“ gibt es bewusst nicht mehr – fragt sonst
          auch längst abgeschaltete/nicht erreichbare Repeater ab und blockiert die Regel unnötig lange)</span>
          ${checks('targets', contacts.filter(x => ['repeater', 'room'].includes(x.type_name)), sel)}</div>
        <label class="field"><span>Intervall in Minuten (mindestens 5)</span>
          <input type="number" name="interval_min" min="5" max="1440" value="${c.interval_min}"></label>
        <label class="field"><span>Warnung unter (Prozent) – <strong id="lowMv"></strong></span>
          <input type="number" name="low_pct" min="1" max="100" step="1" value="${batteryPct(c.low_mv)}" data-orig-mv="${Number(c.low_mv) || ''}"></label>
        <p class="muted small" style="margin:-4px 0 10px">Jede Regel hat ihre eigene Warnstufe. Die Prozent folgen der Skala der MeshCore-App (3000 mV = 0 %, 4200 mV = 100 %),
          eine Näherung und keine echte Ladestandsmessung. 40 % entsprechen etwa 3480 mV, 20 % nur noch 3240 mV: dann bleibt kaum Zeit zum Handeln.</p>
        <label class="toggle"><input type="checkbox" name="temperature" ${c.temperature !== false ? 'checked' : ''}> Temperatur ebenfalls abfragen (zusätzliche Funk-Anfrage; Repeater ohne Sensor werden danach seltener gefragt)</label>`;
    }
    if (kind === 'ping') {
      return nameField + `
        <div class="field"><span class="muted small">Ziele</span>${checks('targets', contacts, c.targets)}</div>
        <label class="field"><span>Intervall in Minuten (mindestens 5)</span>
          <input type="number" name="interval_min" min="5" max="1440" value="${c.interval_min}"></label>
        <label class="field"><span>Art</span><select name="mode">
          <option value="trace" ${c.mode === 'trace' ? 'selected' : ''}>Direkt-Ping / Zero-Hop (für Repeater/Rooms in direkter Funkreichweite, zeigt Signalstärke)</option>
          <option value="status" ${c.mode === 'status' ? 'selected' : ''}>Statusabfrage (Repeater/Rooms, auch über mehrere Hops)</option>
          <option value="message" ${c.mode === 'message' ? 'selected' : ''}>Nachricht senden (nur für Clients, sichtbar; Repeater und Rooms nehmen keine Textnachrichten an)</option></select></label>
        <label class="field"><span>Text bei „Nachricht senden“ (wird unverändert gesendet)</span>
          <input type="text" name="text" maxlength="60" value="${esc(c.text)}"></label>`;
    }
    if (kind === 'advert') {
      return nameField + `
        <label class="field"><span>Intervall in Minuten (mindestens 60; netzweit mindestens 360)</span>
          <input type="number" name="interval_min" min="60" max="10080" value="${c.interval_min}"></label>
        <label class="toggle"><input type="checkbox" name="flood" ${c.flood ? 'checked' : ''}> netzweit senden (belegt mehr Funkzeit; sonst nur direkte Nachbarn)</label>
        <p class="muted small">Sendet automatisch deine Visitenkarte, damit andere dich weiter sehen. Zählt zum Sendebudget.</p>`;
    }
    if (kind === 'channel_ping') {
      const opts = channels.map(ch => `<option value="${ch.idx}" ${ch.idx === c.channel ? 'selected' : ''}>${esc(ch.name)} (Platz ${ch.idx})</option>`).join('');
      return nameField + `
        <label class="field"><span>Kanal</span><select name="channel" required>${opts || '<option value="">Keine Kanäle auf dem Node</option>'}</select></label>
        <label class="field"><span>Intervall in Minuten (mindestens 15, weil alle im Kanal es empfangen)</span>
          <input type="number" name="interval_min" min="15" max="1440" value="${c.interval_min}"></label>
        <label class="field"><span>Text (wird unverändert in den Kanal gesendet)</span>
          <input type="text" name="text" maxlength="60" value="${esc(c.text)}"></label>
        <p class="muted small">Antworten anderer Teilnehmer erscheinen im Chat der Seite „Kanäle“.</p>`;
    }
    const clients = contacts.filter(x => x.type_name === 'client');
    return nameField + `
      <label class="field"><span>Auslöser-Text</span><input type="text" name="match" maxlength="100" required value="${esc(c.match)}"></label>
      <label class="field"><span>Vergleich</span><select name="mode">
        <option value="equals" ${c.mode === 'equals' ? 'selected' : ''}>Nachricht ist genau dieser Text</option>
        <option value="contains" ${c.mode === 'contains' ? 'selected' : ''}>Nachricht enthält diesen Text</option></select></label>
      <label class="field"><span>Antwort (max. 150 Zeichen; beginnt automatisch mit @[Name]; Platzhalter {name} (als Erwähnung) {time} {hops} {path})</span>
        <input type="text" name="reply" maxlength="150" required value="${esc(c.reply)}"></label>
      <div class="field"><span class="muted small">Wo antworten?</span>
        <label class="toggle small"><input type="checkbox" name="direct" ${c.direct !== false ? 'checked' : ''}> auf Direktnachrichten</label><br>
        ${channels.length ? channels.map(ch => `<label class="toggle small"><input type="checkbox" name="channels" value="${ch.idx}"
          ${(c.channels || []).includes(ch.idx) ? 'checked' : ''}> im Kanal ${esc(ch.name)} <span class="muted">(Platz ${ch.idx})</span></label>`).join('<br>')
          : '<span class="muted small">Keine Kanäle auf dem Node.</span>'}
        <p class="muted small" style="margin:4px 0 0">Antworten in einem Kanal sieht jeder im Kanal. Darum unten die Begrenzungen setzen.</p></div>
      <label class="field"><span>Pause je Person nach einer Antwort (Minuten, mindestens 0,5). Bei Kanälen zählt der Name des Absenders.</span>
        <input type="number" name="cooldown_min" min="0.5" max="1440" step="0.5" value="${+(c.cooldown_s / 60).toFixed(2)}"></label>
      <label class="field"><span>Höchstens so viele Antworten dieser Regel in 60 Minuten (alle Personen zusammen)</span>
        <input type="number" name="max_per_hour" min="1" max="60" value="${c.max_per_hour ?? 10}"></label>
      <div class="field"><span class="muted small">Direktnachrichten nur beantworten von (leer = alle bekannten Kontakte)</span>
        ${checks('only_from', clients, c.only_from || [])}</div>`;
  }

  function openForm(kind, rule) {
    editing = {id: rule ? rule.id : null, kind, enabled: rule ? rule.enabled : true};
    $('dlgTitle').textContent = `${rule ? 'Bearbeiten' : 'Neu'}: ${KINDS[kind]}`;
    $('form').innerHTML = formHtml(kind, rule);
    $('formError').textContent = '';
    const low = $('form').querySelector('[name=low_pct]'), lowMv = $('form').querySelector('#lowMv');
    if (low && lowMv) {
      const upd = () => { lowMv.textContent = `≈ ${lowMvFromPct(low.value, low.dataset.origMv)} mV`; };
      low.addEventListener('input', upd); upd();
    }
    $('form').querySelectorAll('.pick-search').forEach(s => {
      const list = $('form').querySelector(`.pick-list[data-list="${s.dataset.for}"]`);
      s.addEventListener('input', () => {
        const q = s.value.trim().toLowerCase();
        list.querySelectorAll('.pick-item').forEach(el => { el.style.display = el.dataset.q.includes(q) ? '' : 'none'; });
      });
    });
    $('dlg').showModal();
  }

  function readForm() {
    const f = new FormData($('form'));
    const picked = name => f.getAll(name);
    const kind = editing.kind;
    let config;
    if (kind === 'battery_poll') {
      config = {
        targets: picked('targets'),
        interval_min: Number(f.get('interval_min')),
        low_mv: lowMvFromPct(f.get('low_pct'), $('form').querySelector('[name=low_pct]').dataset.origMv),
        temperature: f.get('temperature') !== null,
      };
    } else if (kind === 'advert') {
      config = {interval_min: Number(f.get('interval_min')), flood: f.get('flood') !== null};
    } else if (kind === 'channel_ping') {
      config = {channel: f.get('channel') ? Number(f.get('channel')) : null, interval_min: Number(f.get('interval_min')), text: f.get('text')};
    } else if (kind === 'ping') {
      config = {targets: picked('targets'), interval_min: Number(f.get('interval_min')),
                mode: f.get('mode'), text: f.get('text')};
    } else {
      const only = picked('only_from');
      config = {match: f.get('match'), mode: f.get('mode'), reply: f.get('reply'),
                cooldown_s: Math.round(Number(f.get('cooldown_min')) * 60), max_per_hour: Number(f.get('max_per_hour')),
                direct: f.get('direct') !== null, channels: picked('channels').map(Number),
                only_from: only.length ? only : null};
    }
    return {kind, name: f.get('name'), enabled: editing.enabled, config};
  }

  $('save').onclick = e => { e.preventDefault(); saveRule(); };
  const saveRule = locked($('save'), async () => {
    if (!$('form').reportValidity()) return;
    try {
      const body = readForm();
      if (editing.id) await put(`/api/rules/${editing.id}`, body);
      else await post('/api/rules', body);
      if (!alive) return;
      $('dlg').close();
      await loadRules();
    } catch (err) { if (alive) $('formError').textContent = err.message; }
  });
  $('cancel').onclick = e => { e.preventDefault(); $('dlg').close(); };

  $('addRule').onclick = () => openForm($('newKind').value, null);

  await Promise.all([loadState(), loadRules()]);
  const offs = [
    ctx.on('automation', ev => { logLine({...ev.payload, ts: ev.ts}); loadState().catch(() => {}); }),
  ];
  return () => { alive = false; offs.forEach(off => off()); };
}
