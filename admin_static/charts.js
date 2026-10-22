// Простые SVG-графики без внешних библиотек.
(function () {
  var box = document.getElementById('chart');
  if (!box) return;
  var NS = 'http://www.w3.org/2000/svg';
  function el(name, attrs, text) {
    var e = document.createElementNS(NS, name);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function fmt(v) { return (Math.round(v * 100) / 100).toLocaleString('ru-RU'); }
  function bars(labels, values, title) {
    var W = 900, H = 320, L = 60, B = 60, T = 20, n = values.length;
    var max = Math.max.apply(null, values.map(function (v) { return Math.abs(v || 0); }).concat([1]));
    var min = Math.min.apply(null, values.map(function (v) { return v || 0; }).concat([0]));
    var span = max - Math.min(min, 0) || 1, ih = H - T - B, zero = T + ih * (max / span);
    var svg = el('svg', {viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': title});
    svg.appendChild(el('line', {x1: L, y1: zero, x2: W - 10, y2: zero, 'class': 'axis'}));
    for (var g = 0; g <= 4; g++) {
      var val = max - span * g / 4, y = T + ih * g / 4;
      svg.appendChild(el('line', {x1: L, y1: y, x2: W - 10, y2: y, 'class': 'axis', 'stroke-dasharray': '2 4'}));
      svg.appendChild(el('text', {x: L - 6, y: y + 4, 'text-anchor': 'end'}, fmt(val)));
    }
    var bw = (W - L - 20) / Math.max(n, 1);
    values.forEach(function (v, i) {
      if (v === null || v === undefined) return;
      var h = ih * Math.abs(v) / span, x = L + i * bw + bw * 0.15, y = v >= 0 ? zero - h : zero;
      var r = el('rect', {x: x, y: y, width: bw * 0.7, height: Math.max(h, 1), 'class': 'b' + (v < 0 ? ' neg' : '')});
      r.appendChild(el('title', {}, labels[i] + ': ' + fmt(v)));
      svg.appendChild(r);
      if (n <= 14) svg.appendChild(el('text', {x: x + bw * 0.35, y: y - 4, 'text-anchor': 'middle'}, fmt(v)));
    });
    labels.forEach(function (lab, i) {
      if (n > 14 && i % Math.ceil(n / 14)) return;
      var x = L + i * bw + bw / 2;
      svg.appendChild(el('text', {x: x, y: H - B + 16, 'text-anchor': 'end', transform: 'rotate(-35 ' + x + ' ' + (H - B + 16) + ')'}, lab));
    });
    return svg;
  }
  fetch(box.dataset.url, {credentials: 'same-origin'}).then(function (r) { return r.json(); }).then(function (d) {
    box.innerHTML = '';
    var values = box.dataset.mode === 'item' ? d.consumption : d.totals;
    if (!d.periods || !d.periods.length || !values.some(function (v) { return v !== null; })) {
      box.innerHTML = '<p class="muted">Нет данных: для расхода нужны показания минимум за два месяца подряд.</p>';
      return;
    }
    box.appendChild(bars(d.labels, values, 'Расход по месяцам'));
    var p = document.createElement('p'); p.className = 'muted small';
    p.textContent = box.dataset.mode === 'item' ? 'Расход по месяцам (красным — отрицательный: показание меньше прошлого).'
                                                : 'Суммарный расход по месяцам (учтены строки с показаниями за два месяца подряд).';
    box.appendChild(p);
  }).catch(function () { box.innerHTML = '<p class="danger">Не удалось загрузить данные</p>'; });
})();
