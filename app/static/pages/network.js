// Netz: Funkwege zu Kontakten und von Repeatern gemeldete Nachbarn als Grafik und als Text.
import {get} from '../lib/api.js';
import {esc, fmtDateTime} from '../lib/util.js';

const COLOR = {me: '#1f6feb', client: '#2da44e', repeater: '#d29922', room: '#8957e5', sensor: '#0aa', unknown: '#8a94a3'};

function snrColor(snr) {
  if (snr == null) return '#8a94a3';
  return snr >= 5 ? '#2da44e' : snr >= 0 ? '#d29922' : '#cf222e';
}

export async function mount(root, ctx) {
  root.innerHTML = `
    <section class="card">
      <h2 style="margin-top:0">Netz</h2>
      <p class="muted small">Durchgezogen: bekannter Funkweg zu einem Kontakt. Gestrichelt: Nachbar, den ein Repeater gehört hat (Farbe = Signalqualität).
        Blass gepunktet: Kontakt bekannt, aber noch kein Pfad gelernt (Flood) – meist erledigt sich das von selbst, sobald eine
        Nachricht hin und her ging, oder „Knoten → Ping“ hilft nach. Türkis gepunktet: Repeater, über den tatsächlich eine
        Kanalnachricht ankam (auch ohne bekannten Pfad – zeigt oft mehr Wege als der vom Node gemeldete).
        Auf einen Punkt tippen zeigt seinen Namen (bei vielen Kontakten überlappen sich sonst alle Beschriftungen).
        Nachbarlisten erscheinen, sobald du bei „Knoten → Verwalten“ einen Repeater anmeldest und „Nachbarn anzeigen“ drückst.</p>
      <p id="netMsg" class="small err"></p>
      <div id="netGraph"></div>
      <div class="row small" id="netZoomCtl" style="margin:6px 0;gap:6px" hidden>
        <button type="button" id="netZoomOut" class="secondary">−</button>
        <button type="button" id="netZoomReset" class="secondary">Zurücksetzen</button>
        <button type="button" id="netZoomIn" class="secondary">+</button>
        <span class="muted">Ziehen zum Verschieben, Scrollen/Kneifen zum Zoomen</span>
      </div>
      <h3>Wege zu den Kontakten</h3>
      <div id="netRoutes" class="small"></div>
      <h3>Nachbarlisten</h3>
      <div id="netReports" class="small muted"></div>
    </section>`;
  let g;
  try { g = await get('/api/network'); } catch (e) { root.querySelector('#netMsg').textContent = e.message; return () => {}; }

  const nodes = g.nodes;
  // Kräfte-basiertes Layout statt fester Kreisanordnung: bei vielen, ungleichmäßig verknüpften Knoten
  // (z. B. durch die aus Nachrichten-Pfaden ergänzten Kanten) ergeben feste Winkel schnell ein Gewirr aus
  // kreuzenden Linien. Mit Abstoßung zwischen allen Punkten und Anziehung entlang der Kanten ordnen sich
  // tatsächlich verbundene Knoten von selbst näher zueinander an - der übliche Ansatz für Graphen dieser
  // Größe (siehe Fruchterman-Reingold).
  const side = Math.max(420, Math.min(900, Math.round(70 * Math.sqrt(nodes.length))));
  const W = side, H = side;
  const posMap = forceLayout(nodes.map(n => n.id), g.edges, W, H);
  const pos = {};
  for (const id in posMap) pos[id] = [posMap[id].x, posMap[id].y];
  // Je Kanten-Art: Farbe, Strichbreite, Strichelung, Deckkraft und Tooltip-Text.
  const EDGE_STYLE = {
    path: {width: 1.5, dash: '', opacity: 1, tip: e => `Weg zu ${e.via.join(', ')}`},
    neighbour: {width: 2, dash: '5 4', opacity: 1, tip: e => `Nachbar${e.snr != null ? `, SNR ${e.snr} dB` : ''}`},
    flood: {width: 1.5, dash: '1 4', opacity: 0.4, tip: e => `Pfad noch unbekannt (Flood): ${e.via.join(', ')}`},
    heard: {width: 1.5, dash: '2 3', opacity: 0.7, tip: () => 'Hier kam zuletzt eine Kanalnachricht durch'},
  };
  const EDGE_COLOR = {path: '#6b7686', neighbour: null, flood: '#8a94a3', heard: '#2da8a8'};   // null: snrColor() entscheidet
  const lines = g.edges.map(e => {
    const [x1, y1] = pos[e.a] || [0, 0], [x2, y2] = pos[e.b] || [0, 0];
    const s = EDGE_STYLE[e.kind] || EDGE_STYLE.path;
    const col = EDGE_COLOR[e.kind] ?? snrColor(e.snr);
    return `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${col}" stroke-width="${s.width}" opacity="${s.opacity}"
      ${s.dash ? `stroke-dasharray="${s.dash}"` : ''}><title>${esc(s.tip(e))}</title></line>`;
  }).join('');
  // Bei vielen Kontakten überlappen sich waagerechte Namen oben/unten am Kreis stark (Zoomen hilft nicht,
  // weil Text und Punktabstand gemeinsam mitskalieren): Namen außer "Ich" erst beim Antippen eines Punkts zeigen.
  const dots = nodes.map(n => {
    const [x, y] = pos[n.id];
    const label = n.id === 'me' ? 'Ich' : n.name.length > 14 ? n.name.slice(0, 13) + '…' : n.name;
    const hidden = n.id === 'me' ? '' : ' visibility="hidden"';
    return `<g class="net-node" data-id="${esc(n.id)}" style="cursor:pointer"><circle cx="${x}" cy="${y}" r="${n.id === 'me' ? 9 : 7}" fill="${COLOR[n.kind] || COLOR.unknown}" stroke="var(--bg,#fff)" stroke-width="2"><title>${esc(n.name)} (${esc(n.kind)})</title></circle>
      <text x="${x}" y="${y + 20}" text-anchor="middle" font-size="10" fill="currentColor" class="net-label"${hidden}>${esc(label)}</text></g>`;
  }).join('');
  const hasGraph = g.edges.length || nodes.length > 1;
  root.querySelector('#netGraph').innerHTML = hasGraph
    ? `<svg id="netSvg" viewBox="0 0 ${W} ${H}" style="width:100%;max-width:640px;height:auto;touch-action:none;cursor:grab;border:1px solid var(--line);border-radius:10px"
         role="img" aria-label="Netzgrafik"><g id="netZoomG">${lines}${dots}</g></svg>
       <p class="small"><span style="color:${COLOR.repeater}">●</span> Repeater <span style="color:${COLOR.room}">●</span> Room <span style="color:${COLOR.client}">●</span> Client
       <span style="color:${COLOR.unknown}">●</span> unbekannte Station</p>`
    : '<p class="muted">Noch keine Kontakte.</p>';
  if (hasGraph) setupZoom(root, W, H);
  root.querySelector('#netGraph').addEventListener('click', e => {
    const dot = e.target.closest('g.net-node');
    if (dot && dot.dataset.id === 'me') return;          // "Ich" zeigt seinen Namen immer
    const t = dot ? dot.querySelector('.net-label') : null;
    const show = t && t.getAttribute('visibility') === 'hidden';
    // Tippen auf einen anderen Punkt oder daneben: vorher gezeigten Namen wieder verstecken ("Ich" ausgenommen).
    root.querySelectorAll('#netGraph .net-label').forEach(el => { if (el.closest('g').dataset.id !== 'me') el.setAttribute('visibility', 'hidden'); });
    if (show) t.removeAttribute('visibility');
  });

  root.querySelector('#netRoutes').innerHTML = g.routes.length ? `<ul style="padding-left:18px;margin:0">${g.routes.map(r => {
    const how = r.path_len === -1 ? 'Flood (Weg wird gesucht)' : r.path_len === 0 ? 'direkt'
      : r.hops.length ? 'über ' + r.hops.map(esc).join(' → ') : `${r.path_len} Zwischenschritte`;
    return `<li>${esc(r.name)}: ${how}</li>`;
  }).join('')}</ul>` : '<span class="muted">keine Angaben</span>';

  root.querySelector('#netReports').innerHTML = g.reports.length
    ? g.reports.map(r => `${esc((g.nodes.find(n => n.id === r.repeater) || {}).name || r.repeater.slice(0, 8))}: Stand ${esc(fmtDateTime(r.ts))}`).join('<br>')
    : 'Noch keine abgerufen.';
  return () => {};
}

// Fruchterman-Reingold-ähnliches Kräftelayout: Abstoßung zwischen allen Punktpaaren (verhindert Überlappung),
// Anziehung entlang der Kanten (zieht Verbundenes zusammen), mit abkühlender Schrittweite. "me" bleibt in der
// Mitte fest, alles andere pendelt sich darum ein. Deterministisch (fester Startwinkel je Knoten), damit sich
// das Bild nicht bei jedem Neuladen unnötig anders anordnet.
function forceLayout(ids, edges, W, H, iterations = 250) {
  const n = ids.length;
  const k = Math.sqrt((W * H) / Math.max(1, n)) * 0.9;     // angestrebter Punktabstand
  const pos = {};
  ids.forEach((id, i) => {
    const a = (2 * Math.PI * i) / Math.max(1, n);
    const r = Math.min(W, H) * 0.32;
    pos[id] = {x: W / 2 + Math.cos(a) * r, y: H / 2 + Math.sin(a) * r, dx: 0, dy: 0};
  });
  if (pos.me) { pos.me.x = W / 2; pos.me.y = H / 2; pos.me.fixed = true; }
  const validEdges = edges.filter(e => pos[e.a] && pos[e.b] && e.a !== e.b);
  let temp = Math.min(W, H) / 10;
  for (let iter = 0; iter < iterations; iter++) {
    for (const id of ids) { pos[id].dx = 0; pos[id].dy = 0; }
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const a = pos[ids[i]], b = pos[ids[j]];
        let dx = a.x - b.x, dy = a.y - b.y;
        const dist = Math.max(0.01, Math.hypot(dx, dy));
        const f = (k * k) / dist;
        dx = (dx / dist) * f; dy = (dy / dist) * f;
        a.dx += dx; a.dy += dy; b.dx -= dx; b.dy -= dy;
      }
    }
    for (const e of validEdges) {
      const a = pos[e.a], b = pos[e.b];
      let dx = a.x - b.x, dy = a.y - b.y;
      const dist = Math.max(0.01, Math.hypot(dx, dy));
      const f = (dist * dist) / k;
      dx = (dx / dist) * f; dy = (dy / dist) * f;
      a.dx -= dx; a.dy -= dy; b.dx += dx; b.dy += dy;
    }
    for (const id of ids) {
      const p = pos[id];
      if (p.fixed) continue;
      const disp = Math.max(0.01, Math.hypot(p.dx, p.dy));
      p.x += (p.dx / disp) * Math.min(disp, temp);
      p.y += (p.dy / disp) * Math.min(disp, temp);
      p.x = Math.max(24, Math.min(W - 24, p.x));
      p.y = Math.max(24, Math.min(H - 24, p.y));
    }
    temp *= 0.97;
  }
  return pos;
}

// Ziehen zum Verschieben, Scrollen/Kneifen zum Zoomen – nur Pointer-Events, keine Bibliothek.
function setupZoom(root, W, H) {
  const svg = root.querySelector('#netSvg'), gEl = root.querySelector('#netZoomG');
  const ctl = root.querySelector('#netZoomCtl');
  if (ctl) ctl.hidden = false;
  let k = 1, x = 0, y = 0;
  const MIN_K = 0.4, MAX_K = 8;
  const apply = () => gEl.setAttribute('transform', `translate(${x},${y}) scale(${k})`);

  function toSvgPoint(clientX, clientY) {
    const pt = svg.createSVGPoint();
    pt.x = clientX; pt.y = clientY;
    const ctm = svg.getScreenCTM();
    return ctm ? pt.matrixTransform(ctm.inverse()) : {x: clientX, y: clientY};
  }
  function zoomAt(clientX, clientY, factor) {
    const P = toSvgPoint(clientX, clientY);
    const newK = Math.max(MIN_K, Math.min(MAX_K, k * factor));
    const px = (P.x - x) / k, py = (P.y - y) / k;
    x = P.x - newK * px; y = P.y - newK * py; k = newK;
    apply();
  }
  function reset() { k = 1; x = 0; y = 0; apply(); }

  let dragging = false, lastX = 0, lastY = 0, startX = 0, startY = 0, moved = false, swallowClick = false;
  const touches = new Map();
  let pinchDist = 0;
  // Zeiger erst festhalten, wenn er sich bewegt hat (ab 4 px): Sofortiges setPointerCapture lenkt den folgenden „click“ auf das SVG um,
  // dann zeigt Antippen eines Punkts nie seinen Namen.
  const capture = id => { try { if (!svg.hasPointerCapture?.(id)) svg.setPointerCapture(id); } catch { /* Zeiger schon weg */ } };

  svg.addEventListener('pointerdown', e => {
    moved = false; startX = e.clientX; startY = e.clientY;
    if (e.pointerType === 'touch') {
      touches.set(e.pointerId, {x: e.clientX, y: e.clientY});
      if (touches.size === 2) {
        dragging = false;
        const [a, b] = [...touches.values()];
        pinchDist = Math.hypot(a.x - b.x, a.y - b.y);
        moved = true;
        touches.forEach((_, id) => capture(id));
        return;
      }
    }
    dragging = true; lastX = e.clientX; lastY = e.clientY;
    svg.style.cursor = 'grabbing';
  });
  svg.addEventListener('pointermove', e => {
    if (e.pointerType === 'touch' && touches.has(e.pointerId)) {
      touches.set(e.pointerId, {x: e.clientX, y: e.clientY});
      if (touches.size === 2) {
        const [a, b] = [...touches.values()];
        const dist = Math.hypot(a.x - b.x, a.y - b.y);
        if (pinchDist > 0) zoomAt((a.x + b.x) / 2, (a.y + b.y) / 2, dist / pinchDist);
        pinchDist = dist;
        return;
      }
    }
    if (!dragging) return;
    if (!moved) {
      if (Math.hypot(e.clientX - startX, e.clientY - startY) < 4) return;      // Zittern beim Antippen ist kein Ziehen
      moved = true;
      capture(e.pointerId);
    }
    const cur = toSvgPoint(e.clientX, e.clientY), prev = toSvgPoint(lastX, lastY);
    x += cur.x - prev.x; y += cur.y - prev.y;
    lastX = e.clientX; lastY = e.clientY;
    apply();
  });
  function endPointer(e) {
    if (moved && e.type === 'pointerup') swallowClick = true;            // nach dem Ziehen kein „Antippen“ auswerten
    touches.delete(e.pointerId);
    pinchDist = 0;
    dragging = false;
    svg.style.cursor = 'grab';
  }
  svg.addEventListener('pointerup', endPointer);
  svg.addEventListener('pointercancel', endPointer);
  svg.addEventListener('pointerdown', () => { swallowClick = false; }, true);
  svg.addEventListener('click', e => { if (swallowClick) { swallowClick = false; e.stopPropagation(); } }, true);
  svg.addEventListener('wheel', e => {
    e.preventDefault();
    zoomAt(e.clientX, e.clientY, e.deltaY < 0 ? 1.15 : 1 / 1.15);
  }, {passive: false});

  const mid = () => { const b = svg.getBoundingClientRect(); return [b.left + b.width / 2, b.top + b.height / 2]; };
  root.querySelector('#netZoomIn').onclick = () => zoomAt(...mid(), 1.3);
  root.querySelector('#netZoomOut').onclick = () => zoomAt(...mid(), 1 / 1.3);
  root.querySelector('#netZoomReset').onclick = reset;
}
