import {get} from '../lib/api.js';
import {esc, ago, typeLabel, TYPE_COLOR, pathLabel, batteryPct} from '../lib/util.js';

function snrColor(snr) {
  if (snr == null) return '#8a94a3';
  return snr >= 5 ? '#2da44e' : snr >= 0 ? '#d29922' : '#cf222e';
}
// Gleiche Bedeutung wie auf der Netz-Seite, nur als Leaflet-Linienstil statt SVG.
const LINK_STYLE = {
  path: {color: '#6b7686', weight: 2, dashArray: null, opacity: 0.9},
  neighbour: {color: null, weight: 2, dashArray: '6 5', opacity: 0.9},    // color: snrColor(e.snr)
  flood: {color: '#8a94a3', weight: 1.5, dashArray: '2 6', opacity: 0.4},
  heard: {color: '#2da8a8', weight: 1.5, dashArray: '3 5', opacity: 0.7},
};

export async function mount(root, ctx) {
  root.innerHTML = `
    <section class="card">
      <div class="row between">
        <h2 style="margin:0">Karte</h2>
        <div class="chips" id="legend"></div>
      </div>
      <label class="toggle small"><input type="checkbox" id="showLinks"> Verbindungen zeigen (nur zwischen Punkten mit bekannter Position)</label>
      <p id="hint" class="muted small"></p>
      <div id="map"></div>
    </section>
    <section class="card" id="noPosCard" hidden>
      <h2>Ohne bekannte Position</h2>
      <div id="noPos" class="small"></div>
    </section>`;

  const $ = id => root.querySelector('#' + id);
  let alive = true;
  const L = window.Leaflet || window.L;
  if (!L) {
    $('map').outerHTML = '<p class="err">Kartenbibliothek konnte nicht geladen werden.</p>';
    return () => {};
  }

  // Den Container vor dem Warten merken: Wechselt man währenddessen die Seite, ist er nicht mehr im Dokument
  // (die nächste Seite hat den gemeinsamen Container neu befüllt) und $('map') fände ein fremdes Element oder keines.
  const mapEl = $('map');
  let nodes = [];
  try { nodes = await get('/api/nodes'); } catch (e) { if (mapEl.isConnected) $('hint').textContent = e.message; }
  if (!mapEl.isConnected) return () => {};

  const map = L.map(mapEl, {worldCopyJump: true}).setView([51.1, 10.4], 6);   // Deutschland
  const tiles = L.tileLayer(ctx.state.config.tile_url, {
    maxZoom: 18, attribution: '© OpenStreetMap-Mitwirkende',
  }).addTo(map);
  let tileErrors = 0;
  tiles.on('tileerror', () => {
    if (++tileErrors === 3 && alive) {
      $('hint').textContent = 'Kartenkacheln lassen sich nicht laden (kein Internet?). ' +
        'Die Knoten werden trotzdem an ihren Positionen angezeigt.';
    }
  });

  $('legend').innerHTML = ['client', 'repeater', 'room', 'sensor'].map(t =>
    `<span class="chip" style="background:${TYPE_COLOR[t]}">${esc(typeLabel(t))}</span>`).join('');

  const withPos = nodes.filter(n => n.lat != null && n.lon != null);
  const bounds = [];
  for (const n of withPos) {
    const marker = L.circleMarker([n.lat, n.lon], {
      radius: n.type_name === 'repeater' ? 9 : 7,
      color: '#fff', weight: 2, fillColor: TYPE_COLOR[n.type_name] || '#6b7686', fillOpacity: 0.95,
    }).addTo(map);
    const batt = n.battery ? `<br>Akku: ${n.battery.mv} mV · ≈ ${batteryPct(n.battery.mv)} %` : '';
    marker.bindPopup(`<strong>${esc(n.name)}</strong><br>${esc(typeLabel(n.type_name))}` +
      `<br>Zuletzt gesehen: ${esc(ago(n.last_advert))}<br>Pfad: ${esc(pathLabel(n.path_len))}${batt}`);
    bounds.push([n.lat, n.lon]);
  }

  // eigener Node, falls der Status eine Position liefert
  const me = ctx.state.status?.self_info;
  if (me && me.adv_lat && me.adv_lon) {
    L.circleMarker([me.adv_lat, me.adv_lon], {radius: 9, color: '#1c2330', weight: 3, fillColor: '#fff', fillOpacity: 1})
      .addTo(map).bindPopup(`<strong>${esc(ctx.state.status.name || 'Eigener Node')}</strong><br>Dieser Node`);
    bounds.push([me.adv_lat, me.adv_lon]);
  }

  if (bounds.length) map.fitBounds(bounds, {padding: [40, 40], maxZoom: 14});
  else $('hint').textContent = $('hint').textContent || 'Noch keine Kontakte mit Position bekannt.';

  const noPos = nodes.filter(n => n.lat == null || n.lon == null);
  if (noPos.length) {
    $('noPosCard').hidden = false;
    $('noPos').innerHTML = noPos.map(n =>
      `<span class="pill">${esc(n.name)} · ${esc(typeLabel(n.type_name))}</span> `).join('');
  }

  // ---- Verbindungslinien (optional, aus der Netz-Seite) -----------------------------------------
  let linksLayer = null;
  async function renderLinks() {
    if (linksLayer) { map.removeLayer(linksLayer); linksLayer = null; }
    if (!$('showLinks').checked) return;
    let g;
    try { g = await get('/api/network'); } catch (e) { if (alive) $('hint').textContent = e.message; return; }
    if (!alive) return;
    const posById = {};
    for (const n of withPos) posById[n.public_key] = [n.lat, n.lon];
    if (me && me.adv_lat && me.adv_lon) posById.me = [me.adv_lat, me.adv_lon];
    const group = L.layerGroup();
    for (const e of g.edges) {
      const a = posById[e.a], b = posById[e.b];
      if (!a || !b) continue;
      const s = LINK_STYLE[e.kind] || LINK_STYLE.path;
      L.polyline([a, b], {color: s.color || snrColor(e.snr), weight: s.weight, dashArray: s.dashArray, opacity: s.opacity}).addTo(group);
    }
    linksLayer = group.addTo(map);
  }
  $('showLinks').onchange = renderLinks;

  // Karte neu berechnen, wenn das Layout fertig ist
  setTimeout(() => map.invalidateSize(), 0);
  return () => { alive = false; map.remove(); };
}
