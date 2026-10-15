import {get, post, del} from '../lib/api.js';
import {esc, ago, typeLabel, TYPE_COLOR, pathLabel, fmtTime, debounce, mentions, locked, countLabel} from '../lib/util.js';

const FILTERS = [['all', 'Alle'], ['client', 'Clients'], ['repeater', 'Repeater'], ['room', 'Rooms'], ['sensor', 'Sensoren']];

export async function mount(root, ctx) {
  root.innerHTML = `
    <section class="card">
      <div class="row between">
        <h2 style="margin:0">Kontakte <span id="count" class="muted"></span></h2>
        <div class="row">
          <button id="addBtn">Kontakt hinzufügen</button>
          <button id="myCard" class="secondary">Meine Kontaktkarte</button>
        </div>
      </div>
      <div class="row" style="margin:10px 0">
        <input id="search" type="search" placeholder="Suchen …">
        <div class="chips" id="filters"></div>
      </div>
      <p id="error" class="err"></p>
      <div style="overflow-x:auto"><table>
        <thead><tr><th>Name</th><th>Typ</th><th class="hide-sm">Zuletzt gesehen</th><th class="hide-sm">Pfad</th><th></th></tr></thead>
        <tbody id="rows"><tr><td colspan="5" class="muted">lade …</td></tr></tbody>
      </table></div>
    </section>
    <section class="card" id="chatCard" hidden>
      <div class="row between"><h2 id="chatTitle" style="margin:0"></h2><button id="chatClose" class="small secondary">Schließen</button></div>
      <div id="chat" class="chat"></div>
      <div class="row">
        <input id="chatText" type="text" maxlength="139" placeholder="Nachricht">
        <button id="chatSend">Senden</button>
      </div>
      <div id="chatCount" class="muted small" style="text-align:right"></div>
    </section>
    <dialog id="dlgAdd">
      <h2>Kontakt hinzufügen</h2>
      <p class="muted small">Kontaktkarte einfügen (<code>meshcore://…</code> oder Hex-Text), die der andere Teilnehmer
        in seiner App teilt.</p>
      <textarea id="cardText" placeholder="meshcore://…"></textarea>
      <p id="addError" class="err"></p>
      <div class="row"><button id="addOk">Hinzufügen</button><button id="addCancel" class="secondary">Abbrechen</button></div>
    </dialog>
    <dialog id="dlgCard">
      <h2 id="cardTitle"></h2>
      <textarea id="cardOut" readonly></textarea>
      <div class="row"><button id="cardCopy">Kopieren</button><button id="cardClose" class="secondary">Schließen</button></div>
    </dialog>`;

  const $ = id => root.querySelector('#' + id);
  let contacts = [];
  let filter = 'all';
  let chatKey = null;
  let alive = true;

  $('filters').innerHTML = FILTERS.map(([k, l]) => `<button data-f="${k}" class="small">${l}</button>`).join('');
  const markFilter = () => $('filters').querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.f === filter));
  markFilter();
  $('filters').onclick = e => {
    if (e.target.dataset.f) { filter = e.target.dataset.f; markFilter(); render(); }
  };
  $('search').oninput = debounce(render, 150);

  function visible() {
    const q = $('search').value.trim().toLowerCase();
    return contacts.filter(c => (filter === 'all' || c.type_name === filter)
      && (!q || c.name.toLowerCase().includes(q) || c.public_key.startsWith(q)));
  }

  function render() {
    const list = visible();
    $('count').textContent = `(${list.length}/${contacts.length})`;
    $('rows').innerHTML = list.length ? list.map(c => `
      <tr data-key="${esc(c.public_key)}">
        <td><strong>${esc(c.name)}</strong><br><span class="muted small">${esc(c.public_key.slice(0, 12))}…</span></td>
        <td><span class="chip" style="background:${TYPE_COLOR[c.type_name] || '#6b7686'}">${esc(typeLabel(c.type_name))}</span></td>
        <td class="hide-sm">${esc(ago(c.last_advert))}</td>
        <td class="hide-sm">${esc(pathLabel(c.path_len))}</td>
        <td><div class="actions">
          ${['client', 'room'].includes(c.type_name) ? '<button class="small" data-act="chat">Nachricht</button>' : ''}
          <button class="small secondary" data-act="reset">Pfad zurücksetzen</button>
          <button class="small secondary" data-act="export">Teilen</button>
          <button class="small danger" data-act="delete">Löschen</button>
        </div></td>
      </tr>`).join('') : '<tr><td colspan="5" class="muted">Keine Kontakte gefunden.</td></tr>';
  }

  let loadSeq = 0;
  async function load() {
    const my = ++loadSeq;                     // nur die neueste Antwort zählt
    let list, err = '';
    try { list = await get('/api/contacts'); } catch (e) { list = []; err = e.message; }
    if (!alive || my !== loadSeq) return;
    contacts = list;
    $('error').textContent = err;
    render();
  }

  // ---- Zeilenaktionen -----------------------------------------------------
  $('rows').onclick = async e => {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    const key = btn.closest('tr').dataset.key;
    const c = contacts.find(x => x.public_key === key);
    btn.disabled = true;
    try {
      switch (btn.dataset.act) {
        case 'chat': await openChat(c); break;
        case 'reset':
          await post(`/api/contacts/${key}/reset-path`);
          await load();
          break;
        case 'export': showCard(`Kontaktkarte: ${c.name}`, (await get(`/api/contacts/${key}/export`)).uri); break;
        case 'delete':
          if (confirm(`„${c.name}“ wirklich löschen?`)) {
            await del(`/api/contacts/${key}`);
            if (chatKey === key) closeChat();
            await load();
          }
          break;
      }
      if (alive) $('error').textContent = '';
    } catch (err) { if (alive) $('error').textContent = err.message; }
    finally { if (alive) btn.disabled = false; }
  };

  // ---- Kontakt hinzufügen / Karten ----------------------------------------
  $('addBtn').onclick = () => { $('cardText').value = ''; $('addError').textContent = ''; $('dlgAdd').showModal(); };
  $('addCancel').onclick = () => $('dlgAdd').close();
  $('addOk').onclick = locked($('addOk'), async () => {
    try {
      await post('/api/contacts/import', {card: $('cardText').value.trim()});
      if (!alive) return;
      $('dlgAdd').close();
      await load();
    } catch (e) { if (alive) $('addError').textContent = e.message; }
  });

  function showCard(title, uri) {
    $('cardTitle').textContent = title;
    $('cardOut').value = uri;
    $('dlgCard').showModal();
  }
  $('myCard').onclick = async () => {
    try {
      const {uri} = await get('/api/contacts/export');
      if (alive) showCard('Meine Kontaktkarte', uri);
    } catch (e) { if (alive) $('error').textContent = e.message; }
  };
  $('cardClose').onclick = () => $('dlgCard').close();
  $('cardCopy').onclick = async () => {
    try { await navigator.clipboard.writeText($('cardOut').value); $('cardCopy').textContent = 'Kopiert'; }
    catch { $('cardOut').select(); }
  };

  // ---- Chat ---------------------------------------------------------------
  function bubble(m) {
    const out = m.direction === 'out';
    return `<div class="bubble ${out ? 'out' : ''} ${m.auto ? 'auto' : ''}">${mentions(m.text, ctx.state.status?.name)}
      <div class="meta">${fmtTime(m.ts)}${m.auto ? ' · automatisch' : ''}</div></div>`;
  }

  let chatSeq = 0;
  async function openChat(c) {
    chatKey = c.public_key;
    $('chatCard').hidden = false;
    $('chatTitle').textContent = `Nachrichten mit ${c.name}`;
    const key = c.public_key;
    const my = ++chatSeq;
    const msgs = await get(`/api/messages?peer=${key.slice(0, 12)}&limit=100`);
    if (!alive || chatKey !== key || my !== chatSeq) return;   // anderer Chat offen, geschlossen oder neuere Abfrage unterwegs: verwerfen
    $('chat').innerHTML = msgs.map(bubble).join('') || '<p class="muted">Noch keine Nachrichten.</p>';
    $('chat').scrollTop = $('chat').scrollHeight;
    $('chatText').focus();
  }
  function closeChat() { chatKey = null; $('chatCard').hidden = true; }
  $('chatClose').onclick = closeChat;

  const chatMax = Number($('chatText').maxLength);
  const updateChatCount = () => {
    const c = countLabel($('chatText').value, chatMax);
    $('chatCount').textContent = c.text;
    $('chatCount').classList.toggle('err', c.warn);
  };
  $('chatText').oninput = updateChatCount;
  updateChatCount();

  let sending = false;                                 // Senden läuft: Enter löst kein zweites Funkpaket aus
  async function sendChat() {
    const text = $('chatText').value.trim();
    if (sending || !chatKey || !text) return;
    sending = true;
    $('chatSend').disabled = true;
    try {
      await post('/api/message', {to: chatKey, text});
      if (!alive) return;
      $('chatText').value = '';
      updateChatCount();
      const c = contacts.find(x => x.public_key === chatKey);
      if (c) await openChat(c);
    } catch (e) { if (alive) $('error').textContent = e.message; }
    finally { sending = false; if (alive) $('chatSend').disabled = false; }
  }
  $('chatSend').onclick = sendChat;
  $('chatText').onkeydown = e => { if (e.key === 'Enter' && !e.isComposing) sendChat(); };     // Enter während der Wortwahl (IME) sendet nicht

  await load();
  const offs = [
    ctx.on('contact_message', ev => {
      const c = contacts.find(x => x.public_key === chatKey);
      if (c && (ev.payload.pubkey_prefix || '') === chatKey.slice(0, 12)) setTimeout(() => openChat(c).catch(() => {}), 300);
    }),
    ctx.on('new_contact', load),
    ctx.on('advertisement', debounce(load, 1000)),
  ];
  return () => { alive = false; offs.forEach(off => off()); };
}
