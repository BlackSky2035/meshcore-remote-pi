// Suche in allen gespeicherten Nachrichten und Stichwörter, die eine Benachrichtigung auslösen.
import {get, put} from '../lib/api.js';
import {esc, fmtDateTime} from '../lib/util.js';

export async function mount(root, ctx) {
  root.innerHTML = `
    <section class="card">
      <h2 style="margin-top:0">Suche</h2>
      <form id="sf" class="row"><input id="q" placeholder="Suchbegriff (mindestens 2 Zeichen)" maxlength="100" style="flex:1" autocomplete="off">
        <select id="kind"><option value="">alles</option><option value="channel">Kanäle</option><option value="direct">Direktnachrichten</option></select>
        <button class="small" type="submit">Suchen</button></form>
      <p id="err" class="err"></p>
      <div id="res" class="small"></div>
    </section>
    <section class="card">
      <h3 style="margin-top:0">Stichwort-Benachrichtigung</h3>
      <p class="muted small">Kommt eines dieser Wörter in einer neuen Kanal- oder Direktnachricht vor, bekommen alle Geräte mit aktivierten Benachrichtigungen
        eine Meldung, auch wenn der Kanal dort sonst stumm ist. Höchstens 10 Wörter; Groß-/Kleinschreibung egal. Benachrichtigungen schaltest du
        auf der Seite „Kanäle“ ein.</p>
      <div class="row"><input id="kw" placeholder="z. B. Notfall, CQ, Treffen" style="flex:1" maxlength="400" autocomplete="off"><button id="kwSave" class="small">Speichern</button></div>
      <p id="kwMsg" class="small"></p>
    </section>`;
  const $ = id => root.querySelector('#' + id);
  const show = (el, t, cls = '') => { el.className = 'small ' + cls; el.textContent = t; };
  try { $('kw').value = (await get('/api/push/keywords')).words.join(', '); } catch (e) { show($('kwMsg'), e.message, 'err'); }

  $('sf').onsubmit = async e => {
    e.preventDefault();
    $('err').textContent = '';
    const q = $('q').value.trim();
    try {
      const {results} = await get(`/api/search?q=${encodeURIComponent(q)}${$('kind').value ? `&kind=${$('kind').value}` : ''}`);
      const mark = t => esc(t).replace(new RegExp(esc(q).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'), m => `<mark>${m}</mark>`);
      $('res').innerHTML = results.length ? results.map(m => `<div class="setrow"><div class="muted">${esc(fmtDateTime(m.ts))} · ${m.kind === 'channel' ? 'Kanal' : 'Direkt'} „${esc(m.where)}“ · ${m.direction === 'out' ? 'gesendet' : esc(m.name || 'empfangen')}</div>
        <div>${mark(m.text)}</div></div>`).join('') : '<span class="muted">Keine Treffer.</span>';
    } catch (err) { $('err').textContent = err.message; }
  };
  $('kwSave').onclick = async () => {
    const words = $('kw').value.split(',').map(w => w.trim()).filter(Boolean);
    try { const r = await put('/api/push/keywords', {words}); $('kw').value = r.words.join(', '); show($('kwMsg'), r.words.length ? 'Gespeichert.' : 'Keine Stichwörter aktiv.', 'ok'); }
    catch (err) { show($('kwMsg'), err.message, 'err'); }
  };
  return () => {};
}
