// Raum: Beiträge lesen und schreiben, Raum-Bot ein-/ausschalten, Mitglieder verwalten.
import {get, put, post, del} from '../lib/api.js';
import {esc, fmtDateTime} from '../lib/util.js';

export async function mount(root, ctx) {
  root.innerHTML = `
    <section class="card">
      <h2 style="margin-top:0">Raum</h2>
      <p class="muted small">Ein Software-Raum ohne Extra-Firmware: Bekannte Kontakte schicken deinem Node Direktnachrichten wie <b>/p Text</b> (schreiben),
        <b>/neu</b> (älteste zuerst, je 3, danach „noch N neue“), <b>/verlauf 3</b>, <b>/anmelden</b> (neue Beiträge automatisch), <b>/abmelden</b> (automatische Zustellung beenden) und <b>/hilfe</b>. Alle Beiträge bleiben hier gespeichert (nicht nur 32).
        Jede Antwort ist eine Funknachricht und zählt zum Sendebudget. In der MeshCore-App erscheint der Node als normaler Kontakt, nicht als Raum.</p>
      <p id="msg" class="small"></p>
      <div class="row" style="gap:12px;flex-wrap:wrap;align-items:end">
        <label class="toggle"><input type="checkbox" id="en"> Raum-Bot aktiv</label>
        <label class="field" style="margin:0"><span>Name (Präfix der Antworten)</span><input id="title" maxlength="30" style="max-width:180px"></label>
        <label class="field" style="margin:0"><span>Höchstens Beiträge speichern</span><input id="maxp" type="number" min="100" max="20000" style="max-width:120px"></label>
        <button id="save" class="small">Speichern</button>
      </div>
    </section>
    <section class="card">
      <h3 style="margin-top:0">Beiträge</h3>
      <form id="pf" class="row"><input id="ptext" maxlength="100" placeholder="Beitrag von der Webseite" style="flex:1" autocomplete="off">
        <button class="small" type="submit">Senden</button></form>
      <div id="posts" class="small"></div>
    </section>
    <section class="card">
      <h3 style="margin-top:0">Mitglieder</h3>
      <div id="members" class="small"></div>
    </section>`;
  let alive = true;
  const $ = id => root.querySelector('#' + id);
  const msg = (t, c = '') => { const el = $('msg'); if (!alive || !el) return; el.className = 'small ' + c; el.textContent = t || ''; };
  async function load() {
    const d = await get('/api/room');
    if (!alive || !$('en')) return;
    $('en').checked = d.config.enabled; $('title').value = d.config.title; $('maxp').value = d.config.max_posts;
    $('ptext').maxLength = d.post_max;
    $('ptext').placeholder = `Beitrag von der Webseite (höchstens ${d.post_max} Zeichen)`;
    $('posts').innerHTML = d.posts.length ? [...d.posts].reverse().map(p => `<div class="setrow"><div class="muted">[${p.id}] ${esc(fmtDateTime(p.ts))} · ${esc(p.author)}
      <button class="small secondary" data-del="${p.id}">löschen</button></div><div>${esc(p.text)}</div></div>`).join('') : '<span class="muted">Noch keine Beiträge.</span>';
    $('members').innerHTML = d.members.length ? d.members.map(m => `<div class="setrow"><div>${esc(m.name)} ${m.subscribed ? '<span class="muted">· angemeldet (bekommt neue Beiträge)</span>' : ''}${m.offline ? ' <span class="muted">· offline (Zustellung ruht, bis sich sein Node meldet)</span>' : ''}
      <button class="small secondary" data-mem="${esc(m.public_key)}">entfernen</button></div></div>`).join('') : '<span class="muted">Noch niemand hat den Raum benutzt.</span>'
      + `<p class="muted small">Höchstens ${d.max_subscribers} Angemeldete. Entfernte Mitglieder werden beim nächsten Befehl neu angelegt (nur neue Beiträge zählen dann als ungelesen).</p>`;
  }
  const safe = fn => async e => { try { msg(''); await fn(e); } catch (err) { msg(err.message, 'err'); } };
  $('save').onclick = safe(async () => {
    await put('/api/room/config', {enabled: $('en').checked, title: $('title').value, max_posts: Number($('maxp').value)});
    msg('Gespeichert.', 'ok'); await load();
  });
  let sending = false;       // Doppeltippen/langsame Verbindung: nie denselben Beitrag mehrfach senden
  $('pf').onsubmit = safe(async e => {
    e.preventDefault();
    const text = $('ptext').value;
    if (sending || !text.trim()) return;
    sending = true; const btn = $('pf').querySelector('button'); if (btn) btn.disabled = true;
    $('ptext').value = '';
    try { await post('/api/room/posts', {text}); }
    catch (err) { if (!$('ptext').value) $('ptext').value = text; throw err; }        // Fehler: Text zurück, nichts ging verloren
    finally { sending = false; if (btn) btn.disabled = false; }
    await load();
  });
  root.onclick = safe(async e => {
    const b = e.target.closest('button'); if (!b) return;
    if (b.dataset.del && confirm('Beitrag löschen?')) { await del(`/api/room/posts/${b.dataset.del}`); await load(); }
    if (b.dataset.mem && confirm('Mitglied entfernen?')) { await del(`/api/room/members/${b.dataset.mem}`); await load(); }
  });
  // neu laden bei neuem Beitrag, geänderter Mitgliederliste (/anmelden, /abmelden, offline) und nach Rückkehr aus dem Hintergrund
  const offs = ['room_post', 'room_members', '_resume'].map(t => ctx.on(t, () => load().catch(() => {})));
  await load().catch(e => msg(e.message, 'err'));
  return () => { alive = false; offs.forEach(off => { if (typeof off === 'function') off(); }); };
}
