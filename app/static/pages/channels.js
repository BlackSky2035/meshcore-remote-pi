import {get, post, put, del} from '../lib/api.js';
import {esc, ago, fmtTime, mentions, mentionTag, countLabel} from '../lib/util.js';
import {pushState, enablePush, disablePush, savePrefs, testPush} from '../lib/push.js';

function hue(name) {                       // gleicher Absender = gleiche Farbe
  let h = 0;
  for (const c of name || '?') h = (h * 31 + c.charCodeAt(0)) % 360;
  return h;
}

export async function mount(root, ctx) {
  root.innerHTML = `
    <div class="chanwrap">
      <section class="card chanlist">
        <div class="row between">
          <h2 style="margin:0">Kanäle</h2>
          <button id="addBtn" class="small">Neu</button>
        </div>
        <p id="error" class="err"></p>
        <div id="list" class="chanitems"><p class="muted">lade …</p></div>
        <p id="slots" class="muted small"></p>
        <div id="pushBox" class="pushbox">
          <h3 style="margin:10px 0 4px">Benachrichtigungen</h3>
          <p id="pushInfo" class="muted small">prüfe …</p>
          <div class="row" id="pushBtns" hidden>
            <button id="pushOn" class="small">Auf diesem Gerät aktivieren</button>
            <button id="pushTest" class="small secondary" hidden>Test</button>
            <button id="pushOff" class="small secondary" hidden>Abschalten</button>
          </div>
          <label class="toggle small" id="pushBattRow" hidden><input type="checkbox" id="pushBatt"> auch bei Warnungen (Akku, USB-Node)</label>
          <p id="pushErr" class="err small"></p>
        </div>
      </section>
      <section class="card chanchat" id="chatCard">
        <div class="row between chat-head" style="flex-wrap:nowrap">
          <div class="row" style="gap:8px;align-items:center;min-width:0">
            <button id="chatBack" class="small secondary" type="button" aria-label="Zurück zu den Kanälen" hidden>←</button>
            <h2 id="chatTitle" style="margin:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">Kein Kanal gewählt</h2>
          </div>
          <div class="chat-menu-wrap" id="chatActions" hidden>
            <button id="chatMenuBtn" class="small secondary" type="button" aria-haspopup="true" aria-expanded="false" title="Optionen">⋮</button>
            <div id="chatMenu" class="chat-menu" hidden>
              <label class="toggle small chat-menu-item" title="Gespeichert werden Pfade und Repeats immer, hier wird nur die Anzeige für diesen Kanal umgeschaltet">
                <input type="checkbox" id="detailToggle"> Pfade &amp; Repeats anzeigen</label>
              <label class="small chat-menu-item" id="pushChanRow" hidden>Push:
                <select id="pushMode" title="Push-Meldungen für diesen Kanal auf diesem Gerät">
                  <option value="off">aus</option>
                  <option value="every">jede Nachricht</option>
                  <option value="once">einmal, bis gelesen</option>
                </select></label>
              <button id="keyBtn" class="chat-menu-item" type="button">Schlüssel</button>
              <button id="editBtn" class="chat-menu-item" type="button">Bearbeiten</button>
              <button id="delBtn" class="chat-menu-item danger" type="button">Löschen</button>
            </div>
          </div>
        </div>
        <div id="chat" class="chat big"><p class="muted">Links einen Kanal wählen oder einen neuen anlegen.</p></div>
        <div class="chat-compose">
        <div class="row">
          <input id="chatText" type="text" maxlength="139" placeholder="Nachricht" disabled style="flex:1">
          <button id="chatSend" disabled>Senden</button>
        </div>
        <div id="chatCount" class="muted small" style="text-align:right"></div>
        </div>
        <p class="muted small" style="margin:6px 0 0">Doppelklick auf eine Nachricht erwähnt den Absender (<code>@[Name]</code>).</p>
      </section>
    </div>

    <dialog id="dlg">
      <h2 id="dlgTitle">Kanal</h2>
      <label class="small muted">Name (bis 32 Zeichen). Mit # am Anfang entsteht ein Hashtag-Kanal, dessen
        Schlüssel sich aus dem Namen ergibt – jeder mit dem gleichen Namen ist im selben Kanal.</label>
      <input id="fName" type="text" maxlength="32" placeholder="z. B. Familie oder #beispiel" style="width:100%">
      <div id="secretBox">
        <label class="small muted">Schlüssel (32 Hex-Zeichen). Leer lassen: neuer zufälliger Schlüssel
          (beim Bearbeiten: bisheriger bleibt).</label>
        <input id="fSecret" type="text" placeholder="optional" autocomplete="off" style="width:100%;font-family:ui-monospace,monospace">
      </div>
      <div id="slotBox" class="row">
        <label class="small muted" for="fSlot">Platz auf dem Node</label>
        <select id="fSlot"></select>
      </div>
      <p id="dlgError" class="err"></p>
      <div class="row"><button id="dlgOk">Speichern</button><button id="dlgCancel" class="secondary">Abbrechen</button></div>
    </dialog>

    <dialog id="dlgKey">
      <h2 id="keyTitle">Kanalschlüssel</h2>
      <p class="muted small">Wer diesen Schlüssel kennt, kann den Kanal mitlesen und mitschreiben. Nur an
        Personen weitergeben, die dabei sein sollen. In der MeshCore-App den Kanal mit Name und Schlüssel hinzufügen.</p>
      <label class="small muted">Name</label><input id="keyName" readonly style="width:100%">
      <label class="small muted">Schlüssel</label><input id="keyOut" readonly style="width:100%;font-family:ui-monospace,monospace">
      <div class="row" style="margin-top:10px"><button id="keyCopy">Schlüssel kopieren</button><button id="keyClose" class="secondary">Schließen</button></div>
    </dialog>`;

  const $ = id => root.querySelector('#' + id);
  const wrap = root.querySelector('.chanwrap');
  let channels = [], maxCh = 0, current = null, editing = null, lastMsgs = [], contacts = [];
  let alive = true;
  let msgsFor = null;                        // Kanal, zu dem lastMsgs gehört
  // Wo „Ungelesene Nachrichten“ steht, festgelegt beim Öffnen und bis zum Schließen unverändert (Neuzeichnen, neue Nachrichten):
  // {upto: ID} = alles Eingehende mit größerer ID ist ungelesen (vom Server, genau); {count: n} = Ersatz aus der Kanalliste.
  let unreadMark = null;
  let draftFor = null;                        // Kanal, zu dem Eingabefeld und Erwähnung gehören
  let sending = false;                        // Senden läuft: Enter löst kein zweites Funkpaket aus
  // Verwaister Verlaufseintrag (Seite über das Menü verlassen, während ein Chat offen war): ersetzen, sonst gibt es später einen „toten“ Zurück-Schritt.
  if (window.history.state?.mcChat) { try { window.history.replaceState({}, ''); } catch { /* ohne Verlauf */ } }

  // ---- Optionen-Menü (⋮) ------------------------------------------------------
  function closeMenu() { $('chatMenu').hidden = true; $('chatMenuBtn').setAttribute('aria-expanded', 'false'); }
  $('chatMenuBtn').onclick = () => {
    const willOpen = $('chatMenu').hidden;
    $('chatMenu').hidden = !willOpen;
    $('chatMenuBtn').setAttribute('aria-expanded', String(willOpen));
  };
  const onDocClick = e => { if (!$('chatMenu').hidden && !e.target.closest('.chat-menu-wrap')) closeMenu(); };
  document.addEventListener('click', onDocClick);
  $('chatBack').onclick = closeChat;
  const ownName = () => ctx.state.status?.name || '';
  const initial = new URLSearchParams((location.hash.split('?')[1] || '')).get('c');

  // ---- Push (dieses Gerät) -----------------------------------------------------
  let ps = {supported: false, subscribed: false, modes: {}, battery: false, endpoint: null, why: '', permission: 'default'};

  function renderPush() {
    if (!alive) return;
    const box = $('pushBtns');
    $('pushErr').textContent = '';
    if (!ps.supported) {
      $('pushInfo').textContent = ps.why;
      box.hidden = true; $('pushBattRow').hidden = true; $('pushChanRow').hidden = true;
      return;
    }
    box.hidden = false;
    $('pushOn').hidden = ps.subscribed;
    $('pushTest').hidden = $('pushOff').hidden = !ps.subscribed;
    $('pushBattRow').hidden = !ps.subscribed;
    $('pushBatt').checked = ps.battery;
    const n = Object.keys(ps.modes).length;
    $('pushInfo').textContent = ps.subscribed
      ? `Dieses Gerät bekommt Push für ${n} Kanal${n === 1 ? '' : 'e'}. Die Art (jede Nachricht oder einmal, bis gelesen) wählst du oben im Chat des jeweiligen Kanals.`
      : 'Noch nicht aktiv. Nach dem Aktivieren wählst du je Kanal, wofür du Meldungen bekommst.';
    $('pushChanRow').hidden = !ps.subscribed || current === null;
    if (current !== null) $('pushMode').value = ps.modes[current] || 'off';
    renderList();
  }

  async function refreshPush() {
    try { ps = await pushState(); } catch { /* bleibt wie es war */ }
    renderPush();
  }

  async function pushAction(fn) {
    $('pushErr').textContent = '';
    try { await fn(); } catch (e) { if (alive) $('pushErr').textContent = e.message; }
    await refreshPush();
  }

  $('pushOn').onclick = () => pushAction(() => enablePush(current !== null ? {[current]: 'every'} : {}, false));
  $('pushOff').onclick = () => pushAction(() => disablePush());
  $('pushTest').onclick = () => pushAction(() => testPush(ps.endpoint));
  $('pushBatt').onchange = e => pushAction(() => savePrefs(ps.endpoint, ps.modes, e.target.checked));
  $('pushMode').onchange = e => pushAction(() => {
    const modes = {...ps.modes};
    if (e.target.value === 'off') delete modes[current]; else modes[current] = e.target.value;
    return savePrefs(ps.endpoint, modes, ps.battery);
  });

  // ---- Liste ----------------------------------------------------------------
  function renderList() {
    $('list').innerHTML = channels.map(c => `
      <button class="chanitem ${current === c.idx ? 'on' : ''}" data-idx="${c.idx}">
        <span class="channame">${esc(c.name)}${c.unread ? ` <span class="bell" title="${c.unread} ungelesen">🔔 <b>${c.unread > 99 ? '99+' : c.unread}</b></span>` : ''}${ps.modes[c.idx] ? ` <span title="${ps.modes[c.idx] === 'once' ? 'Push: einmal, bis gelesen' : 'Push: jede Nachricht'}">📲</span>` : ''}</span>
        <span class="muted small">${c.hashtag ? 'Hashtag · ' : ''}Platz ${c.idx}${c.last_ts ? ' · ' + esc(ago(c.last_ts)) : ''}</span>
      </button>`).join('') || '<p class="muted">Noch keine Kanäle auf dem Node.</p>';
    $('slots').textContent = maxCh ? `${channels.length} von ${maxCh} Plätzen belegt` : '';
  }

  async function loadList() {
    try {
      const d = await get('/api/channels');
      if (!alive) return;
      channels = d.channels; maxCh = d.max_channels;
      if (current !== null) $('detailToggle').checked = !!currentChannel()?.show_details;
      $('error').textContent = '';
      if (current !== null && !channels.some(c => c.idx === current)) closeChat();
      renderList();
    } catch (e) { if (!alive) return; $('error').textContent = e.message; $('list').innerHTML = ''; }
  }

  $('list').onclick = e => {
    const b = e.target.closest('.chanitem');
    if (b) openChat(Number(b.dataset.idx));
  };

  // ---- Pfade und Repeats ------------------------------------------------------
  const currentChannel = () => channels.find(c => c.idx === current);

  // Hop-Kennung (erstes Byte/erste Bytes des Public Keys eines Repeaters) -> Name, falls eindeutig
  function hopName(hash) {
    const hits = contacts.filter(c => c.type_name === 'repeater' && c.public_key.startsWith(hash));
    if (hits.length === 1) return `<span title="${esc(hash)}">${esc(hits[0].name)}</span>`;
    const hint = hits.length ? `mehrdeutig: ${hits.map(h => h.name).join(', ')}` : 'unbekannter Repeater';
    return `<span class="hop" title="${esc(hint)}">${esc(hash)}</span>`;
  }
  function pathHtml(path, size) {
    if (path === null || path === undefined) return null;
    if (path === '') return 'direkt';
    const step = (size || 1) * 2, hops = [];
    for (let i = 0; i < path.length; i += step) hops.push(hopName(path.slice(i, i + step)));
    return hops.join(' → ');
  }
  const sig = (snr, rssi) => [snr != null ? `SNR ${snr} dB` : null, rssi != null ? `RSSI ${rssi} dBm` : null]
    .filter(Boolean).join(' · ');

  function detailsHtml(m) {
    if (m.direction === 'out') {
      const reps = m.repeats || [];
      if (!reps.length) return '<div class="detail">Noch keine Repeats gehört</div>';
      return `<div class="detail">${reps.length} Repeat${reps.length === 1 ? '' : 's'} gehört:` + reps.map(r =>
        `<div>${pathHtml(r.path, r.path_hash_size) ?? ''}${sig(r.snr, r.rssi) ? ' · ' + esc(sig(r.snr, r.rssi)) : ''}</div>`).join('') + '</div>';
    }
    const p = pathHtml(m.path, m.path_hash_size);
    const hops = m.hops == null ? '' : (m.hops === 0 ? 'direkt' : `${m.hops} Hop${m.hops === 1 ? '' : 's'}`);
    const where = p === null ? hops : (m.path ? `über ${p}${hops ? ` (${hops})` : ''}` : 'direkt');
    const parts = [where || null, sig(m.snr, m.rssi) ? esc(sig(m.snr, m.rssi)) : null].filter(Boolean);
    return `<div class="detail">${parts.join(' · ') || 'Pfad unbekannt'}</div>`;
  }

  // ---- Chat -----------------------------------------------------------------
  function dayLabel(ts) { return new Date(ts * 1000).toLocaleDateString(undefined, {weekday: 'long', day: 'numeric', month: 'long'}); }

  // Erste ungelesene (eingehende) Nachricht in `msgs`. Bevorzugt über die Nachrichten-ID vom Server: der Zähler aus der
  // Kanalliste kann veraltet sein (Liste vor dem Öffnen geladen, Rückkehr aus dem Hintergrund) und setzte die Markierung dann viel zu weit oben.
  function firstUnreadIndex(msgs, mark) {
    if (!mark) return -1;
    if (mark.upto != null) return msgs.findIndex(m => m.direction !== 'out' && Number(m.id) > mark.upto);
    const unreadCount = mark.count;
    if (!unreadCount) return -1;
    let seen = 0;
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].direction !== 'out' && ++seen === unreadCount) return i;
    }
    return 0;          // mehr ungelesen gemeldet als geladen: ganz oben beginnen
  }

  // keep: nur neu zeichnen (z. B. Kontakte nachgeladen), Scrollposition behalten statt zu springen.
  // Der Bereich, der wirklich scrollt: der Chat selbst (PC) oder die Seite (#scrollArea, Handy).
  function scroller(el) {
    let page = false;
    try { page = getComputedStyle(el).overflowY === 'visible'; } catch { /* ohne CSS (Test) */ }
    return page ? (el.closest('#scrollArea') || document.scrollingElement || el) : el;
  }

  // keep: false = beim Öffnen (zur Markierung „Ungelesen“, sonst ans Ende), 'bottom' = ans Ende (eigene Nachricht),
  // 'follow' = nur ans Ende, wer schon unten war, true = Position behalten (nur neu zeichnen).
  function renderMessages(msgs, keep = false) {
    lastMsgs = msgs;
    const details = !!currentChannel()?.show_details;
    const unreadAt = firstUnreadIndex(msgs, unreadMark);
    let lastDay = '';
    const html = msgs.map((m, i) => {
      const out = m.direction === 'out';
      const day = dayLabel(m.ts);
      const sep = day !== lastDay ? `<div class="daysep">${esc(day)}</div>` : '';
      lastDay = day;
      const unreadSep = i === unreadAt
        ? '<div class="unread-sep"><span class="line"></span><span class="label">Ungelesene Nachrichten</span><span class="line"></span></div>' : '';
      const who = out ? 'Ich' : (m.name || 'Unbekannt');
      const color = out ? '' : `style="color:hsl(${hue(who)} 55% 45%)"`;
      return `${sep}${unreadSep}<div class="bubble ${out ? 'out' : ''}" ${out ? '' : `data-name="${esc(m.name || '')}" title="Doppelklick: Absender erwähnen"`}>
        <div class="who" ${color}>${esc(who)}</div>${mentions(m.text, ownName())}
        ${details ? detailsHtml(m) : ''}
        <div class="meta">${fmtTime(m.ts)}</div></div>`;
    }).join('');
    // Am Handy scrollt nicht der Chat selbst, sondern die ganze Seite (style.css): so wirkt ein Wisch überall,
    // auch neben der Karte, und es gibt keine zwei verschachtelten Scrollbereiche mehr.
    const el = $('chat'), sc = scroller(el), top = sc.scrollTop;
    const atEnd = sc.scrollHeight - top - sc.clientHeight < 80;          // liest gerade ganz unten mit
    el.innerHTML = html || '<p class="muted">Noch keine Nachrichten in diesem Kanal.</p>';
    // „follow“ (neue Nachricht kam rein): nur nach unten springen, wer schon unten war – wer ältere Nachrichten liest, bleibt dort
    if (keep === true || (keep === 'follow' && !atEnd)) { sc.scrollTop = top; return; }
    const marker = keep === false && unreadAt >= 0 ? el.querySelector('.unread-sep') : null;
    const place = () => { if (marker) marker.scrollIntoView({block: 'start'}); else sc.scrollTop = sc.scrollHeight; };
    place();
    // Beim Öffnen ändert sich das Layout oft noch kurz danach (Kopfzeile, Tastatur, Schrift): einmal nachziehen,
    // aber nur, wenn inzwischen niemand selbst gescrollt hat.
    if (keep === false) {
      const at = sc.scrollTop;
      (window.requestAnimationFrame || (f => setTimeout(f, 16)))(() => setTimeout(() => { if (alive && msgsFor === current && marker?.isConnected !== false && Math.abs(sc.scrollTop - at) < 2) place(); }, 120));
    }
  }

  async function reloadMessages(keep = false, pending = null) {
    if (current === null) return;
    const idx = current;
    const msgs = await (pending || get(`/api/channels/${idx}/messages?limit=200`));
    if (!alive || current !== idx) return;             // inzwischen anderer Kanal offen: alte Antwort verwerfen
    msgsFor = idx;
    renderMessages(msgs, keep);
  }

  // Als gelesen markieren (Server merkt es sich: Glocke und „einmal“-Push werden damit zurückgesetzt).
  // Antwort: bis zu welcher Nachrichten-ID vorher gelesen war (`read_upto`), null bei Fehler.
  function markRead(idx) {
    return post(`/api/channels/${idx}/read`).then(r => { ctx.refreshUnread?.(); return Number.isFinite(r?.read_upto) ? r.read_upto : null; })
      .catch(() => null);
  }

  async function openChat(idx) {
    const c = channels.find(x => x.idx === idx);
    if (!c) return;
    const unreadAtOpen = c.unread || 0;      // Ersatz, falls der Server nicht sagt, bis wohin gelesen war
    // Ein Verlaufseintrag für den offenen Chat: „Zurück“ des Browsers/iOS-Randwisch schließt dann den Chat statt die Seite zu wechseln.
    if (current === null && !window.history.state?.mcChat) { try { window.history.pushState({mcChat: 1}, ''); } catch { /* ohne Verlauf */ } }
    current = idx;
    // Anderer Kanal: Entwurf und Erwähnung gehören zum vorigen Kanal, nicht mitnehmen.
    if (draftFor !== idx) { $('chatText').value = ''; updateChatCount(); }
    draftFor = idx;
    wrap.classList.add('chat-open');
    $('chatBack').hidden = false;
    closeMenu();
    $('chatTitle').textContent = c.name;
    $('chatActions').hidden = false;
    $('detailToggle').checked = !!c.show_details;
    $('chatText').disabled = false; $('chatSend').disabled = sending;
    $('pushChanRow').hidden = !ps.subscribed;
    $('pushMode').value = ps.modes[idx] || 'off';
    // Gelesen-Markierung und Verlauf gleichzeitig anfragen; der Server nennt die alte Markierung (genau, nie veraltet).
    const readP = markRead(idx);
    const msgsP = get(`/api/channels/${idx}/messages?limit=200`);
    msgsP.catch(() => {});
    c.unread = 0;                             // Glocke sofort weg; sonst nutzte ein schnelles Wiederöffnen den alten Zähler
    renderList();
    unreadMark = null;
    const upto = await readP;
    if (!alive || current !== idx) return;
    unreadMark = upto != null ? {upto} : {count: unreadAtOpen};
    try { await reloadMessages(false, msgsP); if (alive && current === idx) $('error').textContent = ''; }
    catch (e) { if (alive && current === idx) $('error').textContent = e.message; }
    if (alive && current === idx) { try { $('chatText').focus({preventScroll: true}); } catch { /* alt */ } }
  }

  function closeChat() {
    try { const sa = document.getElementById('scrollArea'); if (sa && current !== null) sa.scrollTop = 0; } catch { /* egal */ }   // Liste wieder von oben
    if (window.history.state?.mcChat) { try { window.history.back(); } catch { /* ohne Verlauf */ } }     // Verlaufseintrag wieder entfernen
    current = null;
    wrap.classList.remove('chat-open');
    $('chatBack').hidden = true;
    closeMenu();
    $('chatTitle').textContent = 'Kein Kanal gewählt';
    $('chatActions').hidden = true;
    $('pushChanRow').hidden = true;
    $('chatText').disabled = $('chatSend').disabled = true;
    $('chat').innerHTML = '<p class="muted">Links einen Kanal wählen oder einen neuen anlegen.</p>';
  }

  // Wie bei WhatsApp: Chat nach rechts wischen = der Chat folgt dem Finger und gleitet nach rechts weg, danach steht die Kanalliste da.
  // Reicht der Wisch (ein Drittel der Breite oder schnell), schließt er sich, sonst springt er zurück. Nur waagerechte Wischer
  // nach rechts; senkrechtes Scrollen und Textmarkieren bleiben unberührt. Nur in der einspaltigen Handy-Ansicht (style.css: max-width 640px,
  // dort ist der Chat allein sichtbar) und nicht im offenen Menü: sonst bedeutet Wischen nirgends etwas.
  const singleColumn = () => !window.matchMedia || window.matchMedia('(max-width: 640px)').matches;
  let swipe = null;
  const cleanups = [];                                              // Aufräumen beim Verlassen der Seite (Wisch- und Verlaufs-Listener)
  const chatCard = $('chatCard');
  const slide = (x, anim) => { chatCard.style.transition = anim ? 'transform .18s ease-out' : 'none'; chatCard.style.transform = x ? `translateX(${x}px)` : ''; };
  // Die Wischer hören auf der ganzen Seite (nicht nur auf der Chat-Karte), damit es egal ist, wo der Finger ansetzt, auch neben der Karte.
  const onDoc = (type, fn) => { document.addEventListener(type, fn, {passive: true}); cleanups.push(() => document.removeEventListener(type, fn)); };
  onDoc('touchstart', e => {
    const t = e.touches && e.touches[0];
    const own = e.target.closest?.('input, textarea, select, dialog, #nav, .nav-backdrop');   // Textfelder, Dialoge und das Menü behalten ihre Wischer
    swipe = current !== null && t && !own && e.touches.length === 1 && singleColumn() ? {x: t.clientX, y: t.clientY, t: Date.now(), lock: null} : null;
  });
  onDoc('touchmove', e => {
    const t = e.touches && e.touches[0];
    if (!swipe || !t) return;
    const dx = t.clientX - swipe.x, dy = t.clientY - swipe.y;
    if (!swipe.lock && Math.max(Math.abs(dx), Math.abs(dy)) > 10) swipe.lock = dx > 0 && Math.abs(dy) < dx / 1.5 ? 'h' : 'v';
    if (swipe.lock === 'h') slide(Math.max(0, dx), false);
  });
  onDoc('touchend', e => {
    const t = e.changedTouches && e.changedTouches[0], s = swipe;
    swipe = null;
    if (!s || !t || current === null) return;
    const dx = t.clientX - s.x, dy = t.clientY - s.y, w = chatCard.offsetWidth || 360;
    const fast = Date.now() - s.t < 300 && dx > 60;
    const horizontal = s.lock ? s.lock === 'h' : Math.abs(dy) < dx / 2;      // ohne touchmove (z. B. Test): nur Start/Ende vergleichen
    if (horizontal && (dx > w / 3 || fast || (!s.lock && dx > 80))) {
      if (!s.lock) { slide(0, false); closeChat(); return; }
      slide(w, true);
      setTimeout(() => { slide(0, false); if (alive) closeChat(); }, 180);
    } else slide(0, true);
  });
  onDoc('touchcancel', () => { swipe = null; slide(0, true); });

  const onPop = () => { if (current !== null) closeChat(); };      // Browser-„Zurück“ (auch der Randwisch am iPhone) = zurück zur Kanalliste
  window.addEventListener('popstate', onPop);
  cleanups.push(() => window.removeEventListener('popstate', onPop));

  const chatMax = Number($('chatText').maxLength);
  const updateChatCount = () => {
    const c = countLabel($('chatText').value, chatMax);
    $('chatCount').textContent = c.text;
    $('chatCount').classList.toggle('err', c.warn);
  };
  $('chatText').oninput = updateChatCount;
  updateChatCount();

  async function sendChat() {
    const text = $('chatText').value.trim();
    if (sending || current === null || !text) return;    // läuft schon: kein zweites Funkpaket
    const idx = current;
    sending = true;
    $('chatSend').disabled = true;
    try {
      await post('/api/channel', {channel: idx, text});
      if (!alive) return;
      if (draftFor === idx) { $('chatText').value = ''; updateChatCount(); }
      await reloadMessages('bottom');
      if (alive) $('error').textContent = '';
    } catch (e) { if (alive) $('error').textContent = e.message; }
    finally { sending = false; if (alive) $('chatSend').disabled = current === null; }
  }
  $('detailToggle').onchange = async e => {
    const c = currentChannel();
    if (!c) return;
    try {
      await put(`/api/channels/${c.idx}/view`, {show_details: e.target.checked});
      if (!alive) return;
      c.show_details = e.target.checked;
      if (current === c.idx && msgsFor === c.idx) renderMessages(lastMsgs, true);
      $('error').textContent = '';
    } catch (err) { if (!alive) return; e.target.checked = !!c.show_details; $('error').textContent = err.message; }
  };

  // Doppelklick auf eine fremde Nachricht: Absender im Eingabefeld erwähnen ("@[Name]"). Es wird nur die Erwähnung eingefügt, keine Uhrzeit.
  function mentionFrom(target) {
    const b = target.closest('.bubble[data-name]');
    if (!b || !b.dataset.name || $('chatText').disabled) return;
    const mention = mentionTag(b.dataset.name);
    if (!mention) return;
    window.getSelection?.()?.removeAllRanges();
    const tag = mention + ' ', input = $('chatText');
    if (!input.value.startsWith(tag)) input.value = tag + input.value;
    updateChatCount();
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }
  $('chat').ondblclick = e => mentionFrom(e.target);
  // Am Handy (vor allem iOS) kommt „dblclick“ nicht zuverlässig: Doppeltippen zusätzlich selbst erkennen (zweimal kurz hintereinander
  // an fast derselben Stelle). Mehrfaches Auslösen schadet nicht, der Name wird nur einmal vorangestellt.
  let lastTap = null, tapStart = null;
  $('chat').addEventListener('touchstart', e => {
    const t = e.touches[0];
    tapStart = e.touches.length === 1 && t ? {x: t.clientX, y: t.clientY} : null;
  }, {passive: true});
  $('chat').addEventListener('touchend', e => {
    const t = e.changedTouches[0], s = tapStart;
    tapStart = null;
    if (!s || !t || Math.hypot(t.clientX - s.x, t.clientY - s.y) > 10) { lastTap = null; return; }      // Wischen/Scrollen ist kein Tippen
    const now = Date.now();
    if (lastTap && now - lastTap.t < 350 && Math.hypot(t.clientX - lastTap.x, t.clientY - lastTap.y) < 30) { lastTap = null; mentionFrom(e.target); }
    else lastTap = {t: now, x: t.clientX, y: t.clientY};
  }, {passive: true});

  $('chatSend').onclick = sendChat;
  $('chatText').onkeydown = e => { if (e.key === 'Enter' && !e.isComposing) sendChat(); };     // Enter während der Wortwahl (IME) sendet nicht

  // ---- Anlegen / Bearbeiten ---------------------------------------------------
  function openDialog(ch) {
    editing = ch;
    $('dlgTitle').textContent = ch ? `Kanal „${ch.name}“ bearbeiten` : 'Neuer Kanal';
    $('fName').value = ch ? ch.name : '';
    $('fSecret').value = '';
    $('dlgError').textContent = '';
    const used = new Set(channels.map(c => c.idx));
    const free = [...Array(maxCh).keys()].filter(i => !used.has(i));
    $('slotBox').hidden = !!ch;
    $('fSlot').innerHTML = `<option value="">automatisch (erster freier)</option>` +
      free.map(i => `<option value="${i}">Platz ${i}</option>`).join('');
    syncSecretBox();
    if (!ch && !free.length) $('dlgError').textContent = 'Alle Kanalplätze sind belegt. Zuerst einen Kanal löschen.';
    $('dlg').showModal();
    $('fName').focus();
  }
  function syncSecretBox() { $('secretBox').hidden = $('fName').value.trim().startsWith('#'); }
  $('fName').oninput = syncSecretBox;
  $('addBtn').onclick = () => openDialog(null);
  $('editBtn').onclick = () => { closeMenu(); openDialog(channels.find(c => c.idx === current)); };
  $('dlgCancel').onclick = () => $('dlg').close();

  $('dlgOk').onclick = async () => {
    const body = {name: $('fName').value.trim()};
    const secret = $('fSecret').value.trim();
    if (secret && !body.name.startsWith('#')) body.secret = secret;
    $('dlgOk').disabled = true;
    try {
      let saved;
      if (editing) saved = await put(`/api/channels/${editing.idx}`, body);
      else {
        if ($('fSlot').value !== '') body.idx = Number($('fSlot').value);
        saved = await post('/api/channels', body);
      }
      if (!alive) return;
      $('dlg').close();
      await loadList();
      await openChat(saved.idx);
    } catch (e) { if (alive) $('dlgError').textContent = e.message; }
    finally { if (alive) $('dlgOk').disabled = false; }
  };

  // ---- Schlüssel / Löschen ----------------------------------------------------
  $('keyBtn').onclick = async () => {
    closeMenu();
    try {
      const d = await get(`/api/channels/${current}/secret`);
      if (!alive) return;
      $('keyName').value = d.name; $('keyOut').value = d.secret;
      $('keyCopy').textContent = 'Schlüssel kopieren';
      $('dlgKey').showModal();
    } catch (e) { if (alive) $('error').textContent = e.message; }
  };
  $('keyClose').onclick = () => { $('keyOut').value = ''; $('dlgKey').close(); };
  $('keyCopy').onclick = async () => {
    try { await navigator.clipboard.writeText($('keyOut').value); $('keyCopy').textContent = 'Kopiert'; }
    catch { $('keyOut').select(); }
  };
  $('delBtn').onclick = async () => {
    closeMenu();
    const c = channels.find(x => x.idx === current);
    if (!c || !confirm(`Kanal „${c.name}“ vom Node löschen? Der gespeicherte Verlauf geht ebenfalls verloren.`)) return;
    try { await del(`/api/channels/${c.idx}`); if (!alive) return; closeChat(); await loadList(); }
    catch (e) { if (alive) $('error').textContent = e.message; }
  };

  // ---- Live -------------------------------------------------------------------
  get('/api/contacts').then(c => { contacts = c; if (alive && current !== null && msgsFor === current) renderMessages(lastMsgs, true); }).catch(() => {});
  await loadList();
  refreshPush();
  if (initial !== null && channels.some(c => c.idx === Number(initial))) await openChat(Number(initial));
  const offs = [
    ctx.on('channel_message', ev => {
      const mine = current !== null && Number(ev.payload.channel_idx) === current;
      if (mine && document.visibilityState === 'visible') markRead(current);     // Kanal ist offen und sichtbar: gelesen
      loadList();
      if (mine) setTimeout(() => reloadMessages('follow').catch(() => {}), 300);
    }),
    ctx.on('channel_read', () => loadList()),
    // zurück aus dem Hintergrund: verpasste Nachrichten nachladen (Liste mit Glocken, offener Chat)
    ctx.on('_resume', () => { loadList(); if (current !== null) reloadMessages('follow').catch(() => {}); }),
    ctx.on('channel_repeat', ev => {
      if (current !== null && Number(ev.payload.channel_idx) === current) reloadMessages('follow').catch(() => {});
    }),
    ctx.on('new_contact', () => get('/api/contacts').then(c => { contacts = c; }).catch(() => {})),
    ctx.on('_status', () => {}),
  ];
  const onVisible = () => { if (document.visibilityState === 'visible' && current !== null) markRead(current); };
  document.addEventListener('visibilitychange', onVisible);
  return () => {
    alive = false;
    offs.forEach(off => off());
    cleanups.forEach(off => off());
    document.removeEventListener('visibilitychange', onVisible);
    document.removeEventListener('click', onDocClick);
  };
}
