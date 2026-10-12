// Kleines SVG-Liniendiagramm mit Fadenkreuz und Tooltip. Keine Abhängigkeiten.
// Farben kommen aus CSS-Variablen (hell/dunkel), Beschriftungen immer per textContent.
const NS = 'http://www.w3.org/2000/svg';
const el = (tag, attrs = {}, text) => {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (text !== undefined) n.textContent = text;
  return n;
};

export function niceTicks(lo, hi, want = 5) {
  if (!(hi > lo)) { lo -= 1; hi += 1; }
  const raw = (hi - lo) / want;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) || 10 * mag;
  const start = Math.floor(lo / step) * step, end = Math.ceil(hi / step) * step;
  const ticks = [];
  for (let v = start; v <= end + step / 2; v += step) ticks.push(+v.toFixed(6));
  return ticks;
}

// Rechter Rand für eine zweite Skala: nach der längsten Beschriftung bemessen (grobe Breite je Zeichen bei
// 11px Schrift), sonst ragen längere Texte wie "4200 mV" über den Rand hinaus und werden auf dem Handy abgeschnitten.
export function rightMargin(rightAxis) {
  if (!rightAxis || !rightAxis.length) return 14;
  return Math.max(44, 14 + Math.max(...rightAxis.map(t => t.label.length)) * 6.5);
}

function timeLabel(ts, spanS) {
  const d = new Date(ts * 1000);
  const hm = d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  if (spanS <= 36 * 3600) return hm;
  return d.toLocaleDateString([], {day: '2-digit', month: '2-digit'});
}

/**
 * host: Container-Element. points: [{x: unixSekunden, y: Zahl}]
 * opts: {unit, decimals, colorVar, dash, shape, label, threshold: {y, label}, ariaLabel}
 * dash: Linienmuster (z. B. '7 4', leer = durchgezogen), shape: Punktform 'circle' | 'square' | 'triangle' (Standard Kreis).
 * Gibt {destroy} zurück. Zeichnet bei Größenänderung neu.
 */
export function lineChart(host, points, opts) {
  const {unit = '', decimals = 0, colorVar = '--c-series', label = '', threshold = null,
    rightAxis = null, yLabels = null, extra = null, yMin = null, yMax = null, yTicks = null,
    xMin = null, xMax = null, extraSeries = [], rightPad = null} = opts;
    // yMin/yMax/yTicks: feste Skala (Werte außerhalb werden am Rand gezeichnet); yLabels: eigener Text je
    // yTicks-Eintrag statt der Zahl selbst (gleiche Reihenfolge wie yTicks); rightAxis: [{y, label}] zweite Skala;
    // xMin/xMax: fester Zeitraum (z. B. der ausgewählte Bereich, bis jetzt) statt nur der Spanne der Messpunkte -
    // sonst zeigt die x-Achse bei lückenhaften/frischen Daten einen viel kürzeren Zeitraum als ausgewählt, und
    // die aktuelle Uhrzeit fehlt als Beschriftung, wenn die letzte Messung schon eine Weile zurückliegt.
    // rightPad: fester rechter Rand in px (z. B. rightMargin() eines anderen Diagramms), damit zwei untereinander
    // stehende Diagramme gleich breit zeichnen und ihre Zeitachsen deckungsgleich sind.
    // extraSeries: [{points, colorVar, label}] weitere Linien zum Vergleich, gleiche Skala wie die erste.
  host.classList.add('lc');
  host.textContent = '';
  if (!points.length && !extraSeries.some(s => s.points.length)) {
    const p = document.createElement('p');
    p.className = 'muted';
    p.textContent = 'Noch keine Messwerte in diesem Zeitraum.';
    host.append(p);
    return {destroy() {}};
  }
  const tip = document.createElement('div');
  tip.className = 'lc-tip';
  tip.hidden = true;
  const tipVal = document.createElement('strong');
  const tipTime = document.createElement('span');
  tip.append(tipVal, tipTime);
  const fmt = v => `${v.toFixed(decimals).replace('.', ',')} ${unit}`.trim();

  function draw() {
    for (const n of [...host.querySelectorAll('svg')]) n.remove();
    const W = Math.max(280, host.clientWidth || 640), H = 240;
    const legendH = extraSeries.length ? (extraSeries.length + 1) * 13 + 4 : 0;       // Legende steht über dem Diagramm, nicht über den Linien
    const m = {l: 52, r: rightPad ?? rightMargin(rightAxis), t: 12 + legendH, b: 26};
    const allPoints = points.length ? points : extraSeries.find(s => s.points.length)?.points || points;
    const x0 = xMin ?? allPoints[0].x, x1 = xMax ?? allPoints[allPoints.length - 1].x;
    const ys = points.map(p => p.y).concat(...extraSeries.map(s => s.points.map(p => p.y)))
      .concat(threshold ? [threshold.y] : []);
    const fixed = yMin != null && yMax != null;
    const ticks = fixed ? (yTicks || [yMin, yMax]) : niceTicks(Math.min(...ys), Math.max(...ys));
    const yLo = fixed ? yMin : ticks[0], yHi = fixed ? yMax : ticks[ticks.length - 1];
    const sx = x => m.l + (Math.min(x1, Math.max(x0, x)) - x0) / ((x1 - x0) || 1) * (W - m.l - m.r);
    const sy = y => m.t + (1 - (Math.min(yHi, Math.max(yLo, y)) - yLo) / ((yHi - yLo) || 1)) * (H - m.t - m.b);
    const svg = el('svg', {viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: 'img',
                           'aria-label': opts.ariaLabel || label});
    ticks.forEach((t, i) => {
      svg.append(el('line', {x1: m.l, x2: W - m.r, y1: sy(t), y2: sy(t), class: 'lc-grid'}));
      svg.append(el('text', {x: m.l - 8, y: sy(t) + 4, 'text-anchor': 'end', class: 'lc-axis'},
                    yLabels ? yLabels[i] : t.toFixed(decimals).replace('.', ',')));
    });
    const span = x1 - x0, nx = span < 60 ? 1 : Math.max(2, Math.min(6, Math.floor((W - m.l) / 90)));
    let lastLabel = null;
    for (let i = 0; i < nx; i++) {
      const ts = x0 + span * (nx === 1 ? 0 : i / (nx - 1));
      const text = timeLabel(ts, span);
      if (text === lastLabel) continue;                     // gleiche Beschriftung nicht doppelt (überlappt sonst)
      lastLabel = text;
      const anchor = i === 0 ? 'start' : i === nx - 1 ? 'end' : 'middle';
      svg.append(el('text', {x: sx(ts), y: H - 6, 'text-anchor': anchor, class: 'lc-axis'}, text));
    }
    if (rightAxis) {
      // Unabhängig von der Reihenfolge der übergebenen Einträge nach Bildschirmposition sortieren, sonst
      // greift die "zu eng"-Prüfung unten nicht (sie geht von oben nach unten aufsteigend aus).
      const sorted = rightAxis.filter(t => t.y >= yLo && t.y <= yHi)
        .map(t => ({...t, py: sy(t.y)})).sort((a, b) => a.py - b.py);
      let lastY = -Infinity;
      for (const t of sorted) {
        if (t.py - lastY < 18) continue;       // zu eng: weglassen
        lastY = t.py;
        svg.append(el('text', {x: W - m.r + 8, y: t.py + 4, 'text-anchor': 'start', class: 'lc-axis'}, t.label));
      }
    }
    if (threshold) {
      svg.append(el('line', {x1: m.l, x2: W - m.r, y1: sy(threshold.y), y2: sy(threshold.y), class: 'lc-thresh'}));
      svg.append(el('text', {x: W - m.r - 2, y: sy(threshold.y) - 4, 'text-anchor': 'end', class: 'lc-axis'}, threshold.label));
    }
    // Punkt je Form an (x, y); jede Form behält ihre Farbe und den hellen Rand (lc-dot).
    const marker = (shape, x, y, cVar) => {
      const style = `fill: var(${cVar})`;
      if (shape === 'square') return el('rect', {x: x - 3, y: y - 3, width: 6, height: 6, class: 'lc-dot', style});
      if (shape === 'triangle') return el('polygon', {points: `${x},${y - 4} ${x + 4},${y + 3} ${x - 4},${y + 3}`, class: 'lc-dot', style});
      return el('circle', {cx: x, cy: y, r: 3, class: 'lc-dot', style});
    };
    const drawSeries = (pts, cVar, dash, shape) => {
      if (!pts.length) return;
      const d = pts.map((p, i) => `${i ? 'L' : 'M'}${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join(' ');
      const attrs = {d, class: 'lc-line', style: `stroke: var(${cVar})`};
      if (dash) attrs['stroke-dasharray'] = dash;
      svg.append(el('path', attrs));
      if (pts.length <= 80) for (const p of pts) svg.append(marker(shape, sx(p.x), sy(p.y), cVar));
    };
    drawSeries(points, colorVar, opts.dash, opts.shape);
    for (const s of extraSeries) drawSeries(s.points, s.colorVar, s.dash, s.shape);
    if (extraSeries.length) {
      // Kleine Legende oben links, damit klar ist, welche Linie wem gehört.
      const items = [{label: label || 'A', colorVar, dash: opts.dash, shape: opts.shape}, ...extraSeries];
      items.forEach((it, i) => {
        const ly = 12 + i * 13;
        // Muster der Linie samt Punktform, so wie sie im Diagramm gezeichnet wird
        const sample = {x1: m.l, x2: m.l + 20, y1: ly - 3, y2: ly - 3, class: 'lc-line', style: `stroke: var(${it.colorVar})`};
        if (it.dash) sample['stroke-dasharray'] = it.dash;
        svg.append(el('line', sample));
        svg.append(marker(it.shape, m.l + 10, ly - 3, it.colorVar));
        svg.append(el('text', {x: m.l + 26, y: ly, 'font-size': 10, fill: 'currentColor'}, it.label));
      });
    }
    const cross = el('line', {y1: m.t, y2: H - m.b, class: 'lc-cross'});
    cross.style.display = 'none';
    const focus = el('circle', {r: 5, class: 'lc-focus', style: `fill: var(${colorVar})`});
    focus.style.display = 'none';
    const hit = el('rect', {x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: 'transparent'});
    svg.append(cross, focus, hit);

    function nearest(pts, px) {
      let best = pts[0];
      for (const p of pts) if (Math.abs(sx(p.x) - px) < Math.abs(sx(best.x) - px)) best = p;
      return best;
    }
    function show(ev) {
      const box = svg.getBoundingClientRect();
      const px = (ev.clientX - box.left) * (W / box.width);
      const best = nearest(allPoints, px);
      const cx = sx(best.x), cy = sy(best.y);
      cross.setAttribute('x1', cx); cross.setAttribute('x2', cx); cross.style.display = '';
      focus.setAttribute('cx', cx); focus.setAttribute('cy', cy); focus.style.display = '';
      let text = points.length ? (extraSeries.length && label ? `${label}: ` : '') + fmt(best.y) + (extra ? ` · ${extra(best.y)}` : '') : '';
      for (const s of extraSeries) {
        if (!s.points.length) continue;
        const sp = nearest(s.points, px);
        text += `${text ? ' · ' : ''}${s.label}: ${fmt(sp.y)}`;
      }
      tipVal.textContent = text;
      tipTime.textContent = new Date(best.x * 1000).toLocaleString();
      tip.hidden = false;
      const left = Math.min(Math.max(cx * (box.width / W) - tip.offsetWidth / 2, 0), box.width - tip.offsetWidth);
      tip.style.left = `${left}px`;
      tip.style.top = `${Math.max(cy * (box.height / H) - tip.offsetHeight - 12, 0)}px`;
    }
    function hide() { cross.style.display = 'none'; focus.style.display = 'none'; tip.hidden = true; }
    hit.addEventListener('pointermove', show);
    hit.addEventListener('pointerdown', show);
    hit.addEventListener('pointerleave', hide);
    host.prepend(svg);
  }

  host.append(tip);
  draw();
  let ro = null, lastW = host.clientWidth;
  if (typeof ResizeObserver !== 'undefined') {
    ro = new ResizeObserver(() => { if (host.clientWidth !== lastW) { lastW = host.clientWidth; draw(); } });
    ro.observe(host);
  }
  return {destroy() { ro?.disconnect(); }};
}
