// Repeater verwalten: Anmelden, Schnellaktionen, Einstellungen lesen/setzen, Nachbarn, Zugriffsliste, Terminal.
// Alle Regeln (gesperrte/riskante Befehle, Wertebereiche, Tempo) prüft der Server; hier wird nur bedient und bestätigt.
import {get, post, del} from '../lib/api.js';
import {esc, fmtDateTime} from '../lib/util.js';

const sleep = ms => new Promise(r => setTimeout(r, ms));

export async function mountRepeaterPanel(host, node, onClose) {
  const key = node.public_key;
  const base = `/api/repeater/${key}`;
  const cat = await get('/api/repeater/catalog');
  let st = await get(base);
  if (!host.isConnected) return () => {};             // Seite schon verlassen, während der Katalog lud
  let alive = true;
  let busy = false;
  let abort = false;

  host.hidden = false;
  host.innerHTML = `
    <div class="row between">
      <h2 style="margin:0">Verwalten: ${esc(node.name)}</h2>
      <button id="repClose" class="small secondary">Schließen</button>
    </div>
    <p id="repMsg" class="small"></p>
    <div id="repLogin"></div>
    <div id="repBody" hidden>
      <div class="row between"><span id="repWho" class="muted small"></span><button id="repLogout" class="small secondary">Abmelden</button></div>
      <h3>Schnellaktionen</h3>
      <div class="row" id="repQuick">${cat.quick.map((q, i) =>
        `<button class="small ${q.kind === 'read' ? 'secondary' : ''}" data-quick="${i}">${esc(q.label)}</button>`).join('')}</div>
      <h3>Einstellungen</h3>
      <div class="row"><button class="small secondary" id="repReadAll">Alle lesen</button><button class="small secondary" id="repStop" hidden>Stopp</button>
        <span class="muted small" id="repProg"></span></div>
      <div id="repSettings"></div>
      <h3>Nachbarn und Zugriff</h3>
      <div class="row"><button class="small secondary" id="repNb">Nachbarn anzeigen</button><button class="small secondary" id="repAcl">Zugriffsliste anzeigen</button></div>
      <div id="repLists" class="small" style="overflow-x:auto"></div>
      <h3>Terminal</h3>
      <form id="repForm" class="row"><input id="repCmd" maxlength="150" placeholder="Befehl, z. B. get name" autocomplete="off" style="flex:1">
        <button class="small" type="submit">Senden</button></form>
      <div id="repTerm" class="log" style="max-height:260px"></div>
    </div>`;
  const $ = id => host.querySelector('#' + id);

  // Panel geschlossen oder Seite verlassen, während eine Funkanfrage lief: nichts mehr anzeigen (Elemente sind weg).
  const msg = (text, cls = '') => { if (!alive) return; $('repMsg').className = 'small ' + cls; $('repMsg').textContent = text || ''; };

  function renderLogin() {
    if (!alive) return;
    $('repLogin').hidden = st.logged_in;
    $('repBody').hidden = !st.logged_in;
    if (st.logged_in) { $('repWho').textContent = `angemeldet als ${st.admin ? 'Admin' : 'Gast'}`; return; }
    $('repLogin').innerHTML = `
      <form id="repLoginForm" class="row">
        <input id="repPw" type="password" maxlength="15" placeholder="${st.saved_password ? 'gespeichertes Passwort nutzen (leer lassen)' : 'Admin-Passwort des Repeaters'}" autocomplete="off">
        <button type="submit" class="small">Anmelden</button>
        ${st.saved_password ? '<button type="button" class="small secondary" id="repForget">Gespeichertes Passwort löschen</button>' : ''}
      </form>
      <label class="toggle small"><input type="checkbox" id="repRemember"> Passwort auf diesem Gerät speichern</label>
      <p class="muted small">Das Passwort geht verschlüsselt per Funk an den Repeater. Gespeichert wird es nur auf Wunsch, in einer eigenen
        Datei auf dem Pi (nicht in der Datenbank). Es gibt keine Benutzeranmeldung in diesem Programm: Wer die Oberfläche erreicht, kann mit
        gespeichertem Passwort den Repeater verwalten.</p>`;
    $('repLoginForm').onsubmit = async e => {
      e.preventDefault();
      await run(async () => {
        const body = {password: $('repPw').value || null, remember: $('repRemember').checked};
        st = await post(`${base}/login`, body);
        if (st.warning) msg(st.warning, 'err'); else msg('Angemeldet.', 'ok');
        renderLogin(); await loadHistory();
      }, 'Anmelden … (Funk, kann bis zu 20 s dauern)');
    };
    const f = $('repForget');
    if (f) f.onclick = () => run(async () => { st = await del(`${base}/password`); renderLogin(); });
  }

  async function run(fn, working) {
    if (busy || !alive) return;
    busy = true;
    host.querySelectorAll('button').forEach(b => { if (b.id !== 'repClose' && b.id !== 'repStop') b.disabled = true; });
    if (working) msg(working);
    try { await fn(); }
    catch (e) {
      if (!alive) return;                                  // Fehler nach dem Schließen (z. B. fehlende Elemente) nicht melden
      msg(e.message, 'err');
      if (/Nicht angemeldet/.test(e.message)) { st = await get(base).catch(() => st); renderLogin(); }
    }
    finally {
      busy = false;
      if (alive) host.querySelectorAll('button').forEach(b => { b.disabled = false; });
    }
  }

  // Riskante Befehle: der Server verlangt eine Bestätigung (409); hier wird sie eingeholt und erneut gesendet.
  async function withConfirm(send) {
    try { return await send(false); }
    catch (e) {
      if (e.status === 409 && confirm(`${e.message.replace(/^Bestätigung nötig: /, '')}\n\nWirklich ausführen?`)) return send(true);
      throw e;
    }
  }

  function term(cmd, text, ok) {
    const el = $('repTerm');
    if (!el) return;
    const d = document.createElement('div');
    d.innerHTML = `<span class="k">&gt; ${esc(cmd)}</span><br><span class="${ok ? '' : 'err'}" style="white-space:pre-wrap">${esc(text)}</span>`;
    el.appendChild(d);
    el.scrollTop = el.scrollHeight;
  }

  async function sendCommand(command) {
    const res = await withConfirm(c => post(`${base}/command`, {command, confirm: c}));
    if (!alive) return res;
    term(res.command, res.text, res.ok);
    msg(res.ok ? '' : 'Keine Antwort vom Repeater.', res.ok ? '' : 'err');
    if (res.no_reply) { st = await get(base); renderLogin(); }
    return res;
  }

  async function loadHistory() {
    if (!alive) return;
    $('repTerm').innerHTML = '';
    try { const h = await get(`${base}/history`); if (alive) h.forEach(x => term(x.command, x.reply, x.ok)); } catch { /* egal */ }
  }

  function settingRow(s) {
    let input;
    if (s.kind === 'onoff') input = `<select data-set="${esc(s.key)}"><option value="">–</option><option>on</option><option>off</option></select>`;
    else if (s.kind === 'choice') input = `<select data-set="${esc(s.key)}"><option value="">–</option>${s.choices.map(c => `<option>${esc(c)}</option>`).join('')}</select>`;
    else if (s.kind === 'secret') input = `<input data-set="${esc(s.key)}" type="password" maxlength="${s.max_bytes}" autocomplete="off" placeholder="neu setzen">`;
    else input = `<input data-set="${esc(s.key)}" ${s.kind === 'text' || s.kind === 'radio' ? '' : 'inputmode="decimal"'} placeholder="${s.kind === 'radio' ? '869.525,250,11,5' : ''}">`;
    return `<div class="setrow"><div>${esc(s.label)}${s.hint ? ` <span class="muted small">${esc(s.hint)}</span>` : ''}</div>
      <div class="setctl">${input}${s.kind === 'secret' ? '' : `<button class="small secondary" data-read="${esc(s.key)}">Lesen</button>`}
      <button class="small" data-write="${esc(s.key)}">Setzen</button></div></div>`;
  }
  let lastGroup = '';
  $('repSettings').innerHTML = cat.settings.map(s => {
    const head = s.group !== lastGroup ? `<div class="setgroup">${esc(s.group)}</div>` : '';
    lastGroup = s.group;
    return head + settingRow(s);
  }).join('');

  const field = k => [...host.querySelectorAll('[data-set]')].find(el => el.dataset.set === k);

  async function readOne(k) {
    const res = await post(`${base}/command`, {command: `get ${k}`});
    if (!alive) return false;
    term(res.command, res.text, res.ok);
    if (res.ok) { const text = (res.replies[0] || '').trim(); field(k).value = text.startsWith('>') ? text.slice(1).trim() : text; }
    return res.ok;
  }

  host.onclick = async e => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.quick != null) {
      const q = cat.quick[+b.dataset.quick];
      await run(() => sendCommand(q.command), `${q.label} … (Funk)`);
    } else if (b.dataset.read) {
      await run(async () => { if (!(await readOne(b.dataset.read))) msg('Keine Antwort vom Repeater.', 'err'); }, 'Lese … (Funk)');
    } else if (b.dataset.write) {
      const k = b.dataset.write, v = field(k).value;
      await run(async () => {
        const res = await withConfirm(c => post(`${base}/setting`, {name: k, value: v, confirm: c}));
        if (!alive) return;
        term(res.command, res.text, res.ok);
        msg(res.ok ? 'Gesetzt.' : 'Keine Antwort vom Repeater: nicht sicher, ob die Änderung angekommen ist. Mit „Lesen“ prüfen.', res.ok ? 'ok' : 'err');
        if (k === 'guest.password') field(k).value = '';
      }, 'Setze … (Funk)');
    }
  };

  $('repReadAll').onclick = async () => {
    abort = false;
    $('repStop').hidden = false;
    await run(async () => {
      const keys = cat.settings.filter(s => s.kind !== 'secret').map(s => s.key);
      for (let i = 0; i < keys.length && !abort && alive; i++) {
        $('repProg').textContent = `${i + 1} / ${keys.length}: ${keys[i]}`;
        if (!(await readOne(keys[i]))) { if (!alive) return; msg(`Keine Antwort bei „${keys[i]}“: abgebrochen.`, 'err'); break; }
        await sleep(cat.min_gap_s * 1000 + 300);
      }
      if (alive) $('repProg').textContent = '';
    });
    if (alive) $('repStop').hidden = true;
  };
  $('repStop').onclick = () => { abort = true; };

  $('repNb').onclick = () => run(async () => {
    const r = await get(`${base}/neighbours`);
    if (!alive) return;
    $('repLists').innerHTML = `<p>${r.total} Nachbarn laut Repeater (gezeigt: ${r.neighbours.length})</p><table><thead><tr><th>Nachbar</th><th>zuletzt gehört</th><th>SNR</th></tr></thead><tbody>${
      r.neighbours.map(n => `<tr><td>${esc(n.name || n.prefix)}</td><td>vor ${n.secs_ago >= 3600 ? Math.round(n.secs_ago / 3600) + ' h' : Math.round(n.secs_ago / 60) + ' min'}</td><td>${n.snr} dB</td></tr>`).join('')}</tbody></table>`;
    msg('');
  }, 'Frage Nachbarn ab … (Funk)');

  $('repAcl').onclick = () => run(async () => {
    const r = await get(`${base}/acl`);
    if (!alive) return;
    $('repLists').innerHTML = `<table><thead><tr><th>Teilnehmer</th><th>Recht</th></tr></thead><tbody>${
      r.entries.map(n => `<tr><td>${esc(n.name || n.prefix)}</td><td>${esc(n.perm_label)}</td></tr>`).join('')}</tbody></table>`;
    msg('');
  }, 'Frage Zugriffsliste ab … (Funk)');

  $('repForm').onsubmit = async e => {
    e.preventDefault();
    const c = $('repCmd').value.trim();
    if (!c) return;
    await run(async () => { await sendCommand(c); if (alive) $('repCmd').value = ''; }, 'Sende … (Funk)');
  };

  $('repLogout').onclick = () => run(async () => { st = await post(`${base}/logout`); renderLogin(); msg('Abgemeldet.'); });
  $('repClose').onclick = () => { destroy(); onClose(); };

  function destroy() { alive = false; abort = true; host.hidden = true; host.innerHTML = ''; }

  renderLogin();
  if (st.logged_in) await loadHistory();
  return destroy;
}
