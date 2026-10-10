/* ==========================================================================
   The map of Gotham — the whole city, live, as a holographic map.

   The city is built offline by scripts/build_map.py from the layout in
   wayne/engine/gotham.json: curved coastlines, the Sprang River and the Kane,
   lakes, parks, thirty districts with streets of their own, avenues, highways,
   bridges, rail, piers — and fifteen thousand buildings, extruded in 3D. It is
   drawn here with MapLibre GL (vendored, web/vendor/maplibre) in the console's
   own palette: cyan light on deep blue-black, glowing edges, scan lines.

   On it: everyone who shares where they are, as their portrait in their own
   colour, gliding as their presence changes; the trail of where they've been;
   pins of his own. Hover for names, click someone for who and where, search to
   fly somewhere, toggle layers, tilt into 3D, follow someone as they move.

   Coordinates arrive in the layout's units (x east, y south) and are turned
   into map positions here, around 0°,0° where the projection is flat.
   ========================================================================== */
(function () {
  'use strict';

  var map = null, opts = null, root = null, gazetteer = null;
  var people = {}, pins = {}, selected = null, following = null;
  var dropping = false, fitted = false, ready = false, threeD = false;
  var hover = null, hoverId = null;
  var SHOWN_KEY = 'gotham-map-layers-v2';

  // One layout unit is ~150 m; centred on the island so the projection is flat.
  var K = 0.0013475;
  function ll(x, y) { return [(x - 55) * K, (65 - y) * K]; }
  function unit(lngLat) { return { x: lngLat.lng / K + 55, y: 65 - lngLat.lat / K }; }
  function convert(coords) {
    if (typeof coords[0] === 'number') return ll(coords[0], coords[1]);
    return coords.map(convert);
  }
  function $(sel) { return root.querySelector(sel); }
  function remember(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* private */ } }
  function recall(key, fallback) {
    try { var v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch (e) { return fallback; }
  }

  /* --- palette ------------------------------------------------------------ */

  var C = {
    void: '#02050a', land: '#0c1724', mainland: '#0b1520', edge: '#4fa8e0', lit: '#8fd3ff',
    street: '#17344d', secondary: '#24506f', primary: '#3d79a3', highway: '#7cc3ee',
    park: '#0b2a24', parkEdge: '#2f8f6a', water: '#02050a', label: '#9cb4c9', faint: '#56718a',
    alert: '#e0574f', good: '#58c08a'
  };
  // Every district a quiet shade of the same night.
  var TINTS = ['#0e1a28', '#0c1826', '#101c2b', '#0d1724', '#0f1b29', '#0b1622',
               '#111d2d', '#0e1927', '#0c1725', '#101b2a', '#0d1826', '#0f1a29'];

  /* --- icons: drawn here, in the palette, never a picture ------------------ */

  var ICON_TINT = {
    asylum: C.alert, prison: C.alert, industry: C.alert, nightlife: C.alert,
    manor: C.lit, clock: C.lit, tower: C.lit,
    police: '#9fd8ff', hospital: '#9fd8ff', civic: '#9fd8ff',
    garden: C.good, water: C.edge, beach: '#e0c48a', cemetery: C.faint, statue: C.lit, lighthouse: C.lit, airport: '#9fd8ff',
    // The night's own colours: neon for the clubs, amber for the bars.
    club: '#c77dff', bar: '#e0a43a', diner: '#5fd0c4', fire: C.alert, school: '#9fd8ff',
    // The day's own colours: coffee, the gym, the pictures, shopping, home.
    cafe: '#d9a06b', gym: '#7fd1a8', cinema: '#e07fb0', shop: '#e8c86a', market: '#e0a43a', library: '#9fd8ff',
    heliport: '#9fd8ff', home: C.lit
  };
  var ICON_KINDS = ['tower', 'manor', 'police', 'hospital', 'asylum', 'prison', 'church', 'theatre', 'university',
                    'museum', 'industry', 'station', 'stadium', 'lab', 'nightlife', 'civic', 'news', 'zoo', 'marina',
                    'naval', 'dock', 'clock', 'garden', 'water', 'cemetery', 'circus', 'statue', 'lighthouse',
                    'airport', 'observatory', 'hotel', 'club', 'bar', 'diner', 'fire', 'school', 'beach',
                    'market', 'library', 'heliport', 'gym', 'home', 'cafe', 'cinema', 'shop'];

  function glyph(kind) {
    var p = new Path2D();
    switch (kind) {
      case 'tower': p.rect(-3, -9, 6, 18); p.moveTo(0, -13); p.lineTo(0, -9); p.moveTo(-3, -3); p.lineTo(3, -3); p.moveTo(-3, 3); p.lineTo(3, 3); break;
      case 'manor': p.moveTo(-8, 7); p.lineTo(-8, -1); p.lineTo(0, -8); p.lineTo(8, -1); p.lineTo(8, 7); p.closePath(); p.rect(-2, 2, 4, 5); break;
      case 'police': p.moveTo(0, -9); p.lineTo(8, -5); p.quadraticCurveTo(7, 6, 0, 10); p.quadraticCurveTo(-7, 6, -8, -5); p.closePath(); p.moveTo(0, -4); p.lineTo(0, 4); break;
      case 'hospital': p.moveTo(-2.5, -8); p.lineTo(2.5, -8); p.lineTo(2.5, -2.5); p.lineTo(8, -2.5); p.lineTo(8, 2.5); p.lineTo(2.5, 2.5); p.lineTo(2.5, 8); p.lineTo(-2.5, 8); p.lineTo(-2.5, 2.5); p.lineTo(-8, 2.5); p.lineTo(-8, -2.5); p.lineTo(-2.5, -2.5); p.closePath(); break;
      case 'asylum': p.rect(-8, -7, 16, 14); [-4, 0, 4].forEach(function (x) { p.moveTo(x, -7); p.lineTo(x, 7); }); p.moveTo(-8, 0); p.lineTo(8, 0); break;
      case 'prison': p.rect(-8, -8, 16, 16); [-4, 0, 4].forEach(function (x) { p.moveTo(x, -8); p.lineTo(x, 8); }); break;
      case 'church': p.moveTo(0, -11); p.lineTo(0, -3); p.moveTo(-3, -8); p.lineTo(3, -8); p.moveTo(-6, 9); p.lineTo(-6, 1); p.arc(0, 1, 6, Math.PI, 0); p.lineTo(6, 9); break;
      case 'theatre': for (var i = 0; i < 5; i++) { var a = -Math.PI / 2 + i * 4 * Math.PI / 5; p[i ? 'lineTo' : 'moveTo'](Math.cos(a) * 9, Math.sin(a) * 9); } p.closePath(); break;
      case 'university': p.moveTo(-10, -2); p.lineTo(0, -7); p.lineTo(10, -2); p.lineTo(0, 3); p.closePath(); p.moveTo(-6, 0); p.lineTo(-6, 6); p.quadraticCurveTo(0, 9, 6, 6); p.lineTo(6, 0); break;
      case 'museum': p.moveTo(-9, -4); p.lineTo(0, -9); p.lineTo(9, -4); p.closePath(); [-6, -2, 2, 6].forEach(function (x) { p.moveTo(x, -3); p.lineTo(x, 6); }); p.moveTo(-9, 8); p.lineTo(9, 8); break;
      case 'industry': p.moveTo(-9, 8); p.lineTo(-9, -1); p.lineTo(-4, 2); p.lineTo(-4, -1); p.lineTo(1, 2); p.lineTo(1, -9); p.lineTo(5, -9); p.lineTo(5, 2); p.lineTo(9, 2); p.lineTo(9, 8); p.closePath(); break;
      case 'station': p.moveTo(-4, -9); p.lineTo(4, -9); p.quadraticCurveTo(7, -9, 7, -6); p.lineTo(7, 2); p.quadraticCurveTo(7, 5, 4, 5); p.lineTo(-4, 5); p.quadraticCurveTo(-7, 5, -7, 2); p.lineTo(-7, -6); p.quadraticCurveTo(-7, -9, -4, -9); p.moveTo(-7, -2); p.lineTo(7, -2); p.moveTo(-4, 5); p.lineTo(-7, 10); p.moveTo(4, 5); p.lineTo(7, 10); break;
      case 'stadium': p.ellipse(0, 0, 10, 6, 0, 0, Math.PI * 2); p.moveTo(6, 0); p.ellipse(0, 0, 6, 3, 0, 0, Math.PI * 2); break;
      case 'lab': p.moveTo(-3, -9); p.lineTo(-3, -2); p.lineTo(-8, 8); p.lineTo(8, 8); p.lineTo(3, -2); p.lineTo(3, -9); p.moveTo(-5, -9); p.lineTo(5, -9); p.moveTo(-5, 3); p.lineTo(5, 3); break;
      case 'nightlife': p.moveTo(-8, -8); p.lineTo(8, -8); p.lineTo(0, 1); p.closePath(); p.moveTo(0, 1); p.lineTo(0, 8); p.moveTo(-5, 8); p.lineTo(5, 8); break;
      case 'civic': p.moveTo(-8, 2); p.arc(0, 2, 8, Math.PI, 0); p.moveTo(-10, 2); p.lineTo(10, 2); p.moveTo(-10, 8); p.lineTo(10, 8); [-6, 0, 6].forEach(function (x) { p.moveTo(x, 2); p.lineTo(x, 8); }); break;
      case 'news': p.rect(-8, -8, 16, 16); [-1, 2.5, 6].forEach(function (y) { p.moveTo(-5, y); p.lineTo(5, y); }); p.rect(-5, -5.5, 10, 2.5); break;
      case 'zoo': p.ellipse(0, 4, 5, 4, 0, 0, Math.PI * 2); [[-6, -3], [-2, -7], [2, -7], [6, -3]].forEach(function (c) { p.moveTo(c[0] + 2, c[1]); p.arc(c[0], c[1], 2, 0, Math.PI * 2); }); break;
      case 'marina': case 'naval': case 'dock':
        p.moveTo(2.5, -7); p.arc(0, -7, 2.5, 0, Math.PI * 2); p.moveTo(0, -4.5); p.lineTo(0, 9); p.moveTo(-8, 3); p.quadraticCurveTo(-7, 9, 0, 9); p.quadraticCurveTo(7, 9, 8, 3); p.moveTo(-4, -1); p.lineTo(4, -1); break;
      case 'clock': p.arc(0, 0, 9, 0, Math.PI * 2); p.moveTo(0, 0); p.lineTo(0, -6); p.moveTo(0, 0); p.lineTo(4, 3); break;
      case 'garden': p.moveTo(0, -10); p.quadraticCurveTo(10, 0, 0, 10); p.quadraticCurveTo(-10, 0, 0, -10); p.moveTo(0, -6); p.lineTo(0, 8); break;
      case 'water': [-4, 2].forEach(function (y) { p.moveTo(-9, y); p.bezierCurveTo(-5, y - 4, -2, y + 4, 2, y); p.bezierCurveTo(5, y - 4, 7, y + 2, 9, y); }); break;
      case 'cemetery': p.moveTo(-6, 9); p.lineTo(-6, -3); p.arc(0, -3, 6, Math.PI, 0); p.lineTo(6, 9); p.closePath(); p.moveTo(0, -5); p.lineTo(0, 3); p.moveTo(-3, -2); p.lineTo(3, -2); break;
      case 'beach': p.moveTo(-9, -2); p.quadraticCurveTo(0, -12, 9, -2); p.closePath(); p.moveTo(0, -7); p.lineTo(0, 8); p.moveTo(-9, 9); p.quadraticCurveTo(-4.5, 6, 0, 9); p.quadraticCurveTo(4.5, 12, 9, 9); break;
      case 'circus': p.moveTo(-9, 8); p.lineTo(0, -6); p.lineTo(9, 8); p.closePath(); p.moveTo(0, -6); p.lineTo(0, -11); p.lineTo(4, -9.5); p.lineTo(0, -8); break;
      case 'statue': p.moveTo(2, -8); p.arc(0, -8, 2, 0, Math.PI * 2); p.moveTo(0, -6); p.lineTo(0, 4); p.moveTo(0, -4); p.lineTo(6, -11); p.moveTo(0, -3); p.lineTo(-4, 1); p.moveTo(-5, 9); p.lineTo(5, 9); p.lineTo(4, 4); p.lineTo(-4, 4); p.closePath(); break;
      case 'lighthouse': p.moveTo(-3, 9); p.lineTo(-2, -5); p.lineTo(2, -5); p.lineTo(3, 9); p.closePath(); p.rect(-3, -9, 6, 4); p.moveTo(-5, -7); p.lineTo(-9, -9); p.moveTo(5, -7); p.lineTo(9, -9); break;
      case 'airport': p.moveTo(0, -10); p.lineTo(0, 9); p.moveTo(-9, 1); p.lineTo(0, -3); p.lineTo(9, 1); p.moveTo(-4, 8); p.lineTo(0, 6); p.lineTo(4, 8); break;
      case 'observatory': p.moveTo(-8, 3); p.arc(0, 3, 8, Math.PI, 0); p.moveTo(-9, 3); p.lineTo(9, 3); p.lineTo(9, 8); p.lineTo(-9, 8); p.closePath(); p.moveTo(1, -5); p.lineTo(5, -9); break;
      case 'hotel': p.rect(-9, 0, 18, 5); p.moveTo(-9, 5); p.lineTo(-9, 9); p.moveTo(9, 5); p.lineTo(9, 9); p.moveTo(-9, 0); p.lineTo(-9, -6); p.rect(-6, -3, 5, 3); break;
      case 'club': p.moveTo(-2, 6); p.arc(-4, 6, 2.5, 0, Math.PI * 2); p.moveTo(7, 3); p.arc(5, 3, 2.5, 0, Math.PI * 2); p.moveTo(-1.5, 6); p.lineTo(-1.5, -7); p.lineTo(7.5, -9); p.lineTo(7.5, 3); break;
      case 'bar': p.moveTo(-6, -8); p.lineTo(6, -8); p.lineTo(4, 9); p.lineTo(-4, 9); p.closePath(); p.moveTo(-5.4, -3); p.lineTo(5.4, -3); break;
      case 'diner': p.moveTo(-7, -2); p.lineTo(5, -2); p.lineTo(4, 6); p.lineTo(-6, 6); p.closePath(); p.moveTo(5, 0); p.quadraticCurveTo(9, 0, 8, 3); p.lineTo(4.6, 3.4); p.moveTo(-3, -5); p.quadraticCurveTo(-1, -8, -3, -10); p.moveTo(1, -5); p.quadraticCurveTo(3, -8, 1, -10); break;
      case 'fire': p.moveTo(0, -10); p.bezierCurveTo(6, -4, 8, 2, 4, 8); p.quadraticCurveTo(0, 11, -4, 8); p.bezierCurveTo(-8, 2, -4, -2, -2, -5); p.quadraticCurveTo(-1, 0, 1, 1); p.quadraticCurveTo(2, -4, 0, -10); break;
      case 'school': p.moveTo(-9, -3); p.lineTo(0, -8); p.lineTo(9, -3); p.lineTo(0, 2); p.closePath(); p.rect(-6, 0, 12, 8); break;
      case 'market': p.moveTo(-10, -2); p.lineTo(-8, -8); p.lineTo(8, -8); p.lineTo(10, -2); p.closePath();
        [-5, 0, 5].forEach(function (x) { p.moveTo(x, -8); p.lineTo(x * 1.25, -2); }); p.rect(-8, -2, 16, 10); p.rect(-3, 3, 6, 5); break;
      case 'library': p.moveTo(0, -5); p.quadraticCurveTo(-5, -8, -10, -6); p.lineTo(-10, 7); p.quadraticCurveTo(-5, 5, 0, 8);
        p.quadraticCurveTo(5, 5, 10, 7); p.lineTo(10, -6); p.quadraticCurveTo(5, -8, 0, -5); p.lineTo(0, 8); break;
      case 'heliport': p.arc(0, 0, 9.5, 0, Math.PI * 2); p.moveTo(-4, -5); p.lineTo(-4, 5); p.moveTo(4, -5); p.lineTo(4, 5);
        p.moveTo(-4, 0); p.lineTo(4, 0); break;
      case 'gym': p.rect(-10, -4, 3, 8); p.rect(-7, -6, 3, 12); p.rect(4, -6, 3, 12); p.rect(7, -4, 3, 8); p.moveTo(-4, 0); p.lineTo(4, 0); break;
      case 'home': p.moveTo(-8, 8); p.lineTo(-8, -1); p.lineTo(0, -8); p.lineTo(8, -1); p.lineTo(8, 8); p.closePath();
        p.rect(-2.5, 2, 5, 6); p.moveTo(4, -4.5); p.lineTo(4, -9); p.lineTo(6.5, -9); p.lineTo(6.5, -2.3); break;
      case 'cafe': p.moveTo(-7, -2); p.lineTo(5, -2); p.lineTo(4, 7); p.lineTo(-6, 7); p.closePath(); p.moveTo(5, 0);
        p.quadraticCurveTo(9.5, 0, 8.5, 3.5); p.lineTo(4.4, 4); p.moveTo(-9, 9); p.lineTo(7, 9);
        p.moveTo(-3, -5); p.quadraticCurveTo(-1, -8, -3, -11); p.moveTo(1, -5); p.quadraticCurveTo(3, -8, 1, -11); break;
      case 'cinema': p.rect(-9, -3, 18, 11); p.moveTo(-9, -3); p.lineTo(-7, -9); p.lineTo(9, -6.5); p.lineTo(9, -3);
        [-4, 1, 6].forEach(function (x) { p.moveTo(x, -3); p.lineTo(x + 1.6, -7.6); }); break;
      case 'shop': p.moveTo(-8, -3); p.lineTo(8, -3); p.lineTo(9, 9); p.lineTo(-9, 9); p.closePath(); p.moveTo(-4, -3);
        p.lineTo(-4, -5); p.quadraticCurveTo(0, -11, 4, -5); p.lineTo(4, -3); break;
      default: p.arc(0, 0, 4, 0, Math.PI * 2);
    }
    return p;
  }

  function icon(kind) {
    var ratio = 2, size = 34 * ratio;
    var canvas = document.createElement('canvas');
    canvas.width = canvas.height = size;
    var ctx = canvas.getContext('2d');
    ctx.scale(ratio, ratio);
    ctx.translate(17, 17);
    var tint = ICON_TINT[kind] || C.edge;
    // A small hexagonal badge, like a marker on a tactical display.
    ctx.beginPath();
    for (var i = 0; i < 6; i++) {
      var a = Math.PI / 6 + i * Math.PI / 3;
      ctx[i ? 'lineTo' : 'moveTo'](Math.cos(a) * 14.5, Math.sin(a) * 14.5);
    }
    ctx.closePath();
    ctx.fillStyle = 'rgba(4, 10, 18, 0.94)';
    ctx.shadowColor = tint;
    ctx.shadowBlur = 6;
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.lineWidth = 1.4;
    ctx.strokeStyle = tint;
    ctx.stroke();
    ctx.scale(0.7, 0.7);
    ctx.lineWidth = 1.9;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.stroke(glyph(kind));
    return { width: size, height: size, data: ctx.getImageData(0, 0, size, size).data };
  }

  // Things that move, drawn pointing north (the map turns them): a ferry with its
  // wake, and a police helicopter from above — rotor, body, tail, beacon.
  function sprite(size, draw) {
    var ratio = 2, canvas = document.createElement('canvas');
    canvas.width = canvas.height = size * ratio;
    var ctx = canvas.getContext('2d');
    ctx.scale(ratio, ratio);
    ctx.translate(size / 2, size / 2);
    draw(ctx);
    return { width: size * ratio, height: size * ratio, data: ctx.getImageData(0, 0, size * ratio, size * ratio).data };
  }
  function boat(ctx) {
    ctx.fillStyle = 'rgba(143, 211, 255, 0.22)';
    ctx.beginPath(); ctx.moveTo(-3, 7); ctx.lineTo(-7, 14); ctx.lineTo(7, 14); ctx.lineTo(3, 7); ctx.fill();
    ctx.fillStyle = '#d8ecfa'; ctx.strokeStyle = '#02050a'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, -10); ctx.quadraticCurveTo(5, -4, 4, 8); ctx.lineTo(-4, 8);
    ctx.quadraticCurveTo(-5, -4, 0, -10); ctx.closePath(); ctx.fill(); ctx.stroke();
    ctx.fillStyle = '#4fa8e0'; ctx.fillRect(-2, -2, 4, 5);
  }
  function heli(ctx) {
    ctx.fillStyle = 'rgba(0, 0, 0, 0.45)';                    // its shadow, below and behind
    ctx.beginPath(); ctx.ellipse(2.5, 1.5, 4, 6, 0, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = 'rgba(200, 230, 255, 0.12)'; ctx.strokeStyle = 'rgba(210, 236, 255, 0.7)'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.arc(0, -2, 10.5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(-10, -2); ctx.lineTo(10, -2); ctx.moveTo(0, -12.5); ctx.lineTo(0, 8.5); ctx.stroke();
    ctx.fillStyle = '#1c2c3c'; ctx.strokeStyle = '#bfe6ff'; ctx.lineWidth = 1.4;
    ctx.beginPath(); ctx.ellipse(0, -2, 3.4, 5.6, 0, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(0, 3.5); ctx.lineTo(0, 11); ctx.moveTo(-2.6, 11); ctx.lineTo(2.6, 11); ctx.stroke();
    ctx.fillStyle = '#ff3b3b'; ctx.beginPath(); ctx.arc(0, 0.5, 1.2, 0, Math.PI * 2); ctx.fill();
  }

  // A train of three cars, its headlight on; a container ship, deck stacked;
  // a tug; a sailboat; the harbour launch; an airliner; helicopters in their
  // liveries — all drawn pointing north for the map to turn.
  function train(ctx) {
    ctx.fillStyle = 'rgba(255, 236, 170, 0.35)';
    ctx.beginPath(); ctx.moveTo(-3, -16); ctx.lineTo(3, -16); ctx.lineTo(5, -22); ctx.lineTo(-5, -22); ctx.fill();
    [-14, -4, 6].forEach(function (y, i) {
      ctx.fillStyle = i ? '#cfe4f4' : '#e8f6ff'; ctx.strokeStyle = '#02050a'; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.roundRect ? ctx.roundRect(-3.2, y, 6.4, 9, 1.6) : ctx.rect(-3.2, y, 6.4, 9); ctx.fill(); ctx.stroke();
      ctx.fillStyle = '#4fa8e0'; ctx.fillRect(-2.2, y + 2, 4.4, 1.2); ctx.fillRect(-2.2, y + 5, 4.4, 1.2);
    });
    ctx.fillStyle = '#fff3c4'; ctx.beginPath(); ctx.arc(0, -13.6, 1.1, 0, Math.PI * 2); ctx.fill();
  }
  function ship(ctx) {
    ctx.fillStyle = 'rgba(143, 211, 255, 0.18)';
    ctx.beginPath(); ctx.moveTo(-4, 16); ctx.lineTo(-9, 24); ctx.lineTo(9, 24); ctx.lineTo(4, 16); ctx.fill();
    ctx.fillStyle = '#1c2c3c'; ctx.strokeStyle = '#8fb9d8'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, -22); ctx.quadraticCurveTo(5, -14, 5, -6); ctx.lineTo(5, 17); ctx.lineTo(-5, 17);
    ctx.lineTo(-5, -6); ctx.quadraticCurveTo(-5, -14, 0, -22); ctx.closePath(); ctx.fill(); ctx.stroke();
    var boxes = ['#c97a3a', '#3a7aa8', '#a83a3a', '#58c08a', '#c9a23a', '#7a5aa8'];
    for (var r = 0; r < 6; r++) for (var c = 0; c < 2; c++) {
      ctx.fillStyle = boxes[(r * 2 + c) % boxes.length]; ctx.fillRect(-4 + c * 4.1, -10 + r * 3.6, 3.7, 3.1);
    }
    ctx.fillStyle = '#e8f6ff'; ctx.fillRect(-4, 12, 8, 3.4);
  }
  function tug(ctx) {
    ctx.fillStyle = '#26323f'; ctx.strokeStyle = '#bfe6ff'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, -8); ctx.quadraticCurveTo(4, -4, 4, 2); ctx.quadraticCurveTo(4, 7, 0, 7);
    ctx.quadraticCurveTo(-4, 7, -4, 2); ctx.quadraticCurveTo(-4, -4, 0, -8); ctx.fill(); ctx.stroke();
    ctx.fillStyle = '#e0574f'; ctx.fillRect(-2, -2, 4, 4);
  }
  function sail(ctx) {
    ctx.fillStyle = '#d8ecfa'; ctx.strokeStyle = '#02050a'; ctx.lineWidth = 0.8;
    ctx.beginPath(); ctx.moveTo(0, -6); ctx.quadraticCurveTo(2.4, 0, 1.8, 6); ctx.lineTo(-1.8, 6); ctx.quadraticCurveTo(-2.4, 0, 0, -6); ctx.fill(); ctx.stroke();
    ctx.fillStyle = 'rgba(255, 255, 255, 0.92)'; ctx.beginPath(); ctx.moveTo(0.3, -5); ctx.lineTo(6, 3); ctx.lineTo(0.3, 3.4); ctx.closePath(); ctx.fill();
  }
  function launch(ctx) {
    ctx.fillStyle = '#e8f6ff'; ctx.strokeStyle = '#02050a'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, -9); ctx.quadraticCurveTo(4, -3, 3.4, 7); ctx.lineTo(-3.4, 7); ctx.quadraticCurveTo(-4, -3, 0, -9); ctx.fill(); ctx.stroke();
    ctx.fillStyle = '#3a6fd8'; ctx.fillRect(-2.2, -1, 4.4, 4);
    ctx.fillStyle = '#5aa8ff'; ctx.beginPath(); ctx.arc(0, -1.5, 1.3, 0, Math.PI * 2); ctx.fill();
  }
  function airliner(ctx, shadow) {
    ctx.beginPath();
    ctx.moveTo(0, -14); ctx.quadraticCurveTo(1.8, -12, 1.8, -7); ctx.lineTo(1.8, -3); ctx.lineTo(13, 3); ctx.lineTo(13, 5);
    ctx.lineTo(1.8, 2); ctx.lineTo(1.6, 8); ctx.lineTo(5, 11); ctx.lineTo(5, 12.6); ctx.lineTo(0, 11.4);
    ctx.lineTo(-5, 12.6); ctx.lineTo(-5, 11); ctx.lineTo(-1.6, 8); ctx.lineTo(-1.8, 2); ctx.lineTo(-13, 5); ctx.lineTo(-13, 3);
    ctx.lineTo(-1.8, -3); ctx.lineTo(-1.8, -7); ctx.quadraticCurveTo(-1.8, -12, 0, -14); ctx.closePath();
    if (shadow) { ctx.fillStyle = 'rgba(0, 0, 0, 0.55)'; ctx.fill(); return; }
    ctx.fillStyle = '#e8f3fb'; ctx.strokeStyle = 'rgba(2, 5, 10, 0.9)'; ctx.lineWidth = 1;
    ctx.fill(); ctx.stroke();
    ctx.fillStyle = '#ff3b3b'; ctx.beginPath(); ctx.arc(-12.6, 4, 0.9, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = '#58c08a'; ctx.beginPath(); ctx.arc(12.6, 4, 0.9, 0, Math.PI * 2); ctx.fill();
  }
  function airshipSprite(shadow) {
    return function (ctx) {
      ctx.beginPath(); ctx.ellipse(0, 0, 5.2, 19, 0, 0, Math.PI * 2);
      if (shadow) { ctx.fillStyle = 'rgba(0, 0, 0, 0.4)'; ctx.fill(); return; }
      var g = ctx.createLinearGradient(-5, 0, 5, 0);
      g.addColorStop(0, '#9fb6c8'); g.addColorStop(0.5, '#e6f1f8'); g.addColorStop(1, '#8aa2b5');
      ctx.fillStyle = g; ctx.strokeStyle = 'rgba(2, 5, 10, 0.85)'; ctx.lineWidth = 1; ctx.fill(); ctx.stroke();
      ctx.strokeStyle = 'rgba(2, 5, 10, 0.25)';
      [-10, -4, 2, 8].forEach(function (y) { ctx.beginPath(); ctx.moveTo(-4.6, y); ctx.lineTo(4.6, y); ctx.stroke(); });
      ctx.fillStyle = '#2a3a4a'; ctx.fillRect(-1.6, 2, 3.2, 6);
      ctx.fillStyle = '#c9a23a'; ctx.fillRect(-4.8, -2, 9.6, 2.2);          // the STAGG band
      ctx.fillStyle = '#ff3b3b'; ctx.beginPath(); ctx.arc(0, -18, 1, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = '#9fb6c8';
      ctx.beginPath(); ctx.moveTo(0, 14); ctx.lineTo(-5, 21); ctx.lineTo(5, 21); ctx.closePath(); ctx.fill();
    };
  }
  // The Batwing from above: a black bat in silhouette, scalloped wings edged in
  // the console's blue, the canopy and twin engines glowing.
  function batwingSprite(shadow) {
    return function (ctx) {
      ctx.beginPath();
      ctx.moveTo(0, -16); ctx.quadraticCurveTo(2.6, -9, 3, -3);
      ctx.lineTo(16, 2); ctx.quadraticCurveTo(13, 4, 12.5, 7); ctx.quadraticCurveTo(9.5, 5.5, 8, 8);
      ctx.quadraticCurveTo(5.5, 6.5, 4, 10); ctx.lineTo(2, 12); ctx.lineTo(0, 9.5); ctx.lineTo(-2, 12); ctx.lineTo(-4, 10);
      ctx.quadraticCurveTo(-5.5, 6.5, -8, 8); ctx.quadraticCurveTo(-9.5, 5.5, -12.5, 7); ctx.quadraticCurveTo(-13, 4, -16, 2);
      ctx.lineTo(-3, -3); ctx.quadraticCurveTo(-2.6, -9, 0, -16); ctx.closePath();
      if (shadow) { ctx.fillStyle = 'rgba(0, 0, 0, 0.5)'; ctx.fill(); return; }
      ctx.fillStyle = '#0b1118'; ctx.strokeStyle = 'rgba(143, 211, 255, 0.85)'; ctx.lineWidth = 0.8;
      ctx.fill(); ctx.stroke();
      ctx.fillStyle = 'rgba(90, 168, 255, 0.95)';
      ctx.beginPath(); ctx.ellipse(0, -7, 1.1, 3, 0, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = 'rgba(140, 210, 255, 0.95)';
      [-1.6, 1.6].forEach(function (x) { ctx.beginPath(); ctx.arc(x, 10.6, 0.9, 0, Math.PI * 2); ctx.fill(); });
    };
  }
  function livery(body, beacon) {
    return function (ctx) {
      ctx.fillStyle = 'rgba(0, 0, 0, 0.45)';
      ctx.beginPath(); ctx.ellipse(2.5, 1.5, 4, 6, 0, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = 'rgba(200, 230, 255, 0.12)'; ctx.strokeStyle = 'rgba(210, 236, 255, 0.7)'; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.arc(0, -2, 10.5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(-10, -2); ctx.lineTo(10, -2); ctx.moveTo(0, -12.5); ctx.lineTo(0, 8.5); ctx.stroke();
      ctx.fillStyle = body; ctx.strokeStyle = '#bfe6ff'; ctx.lineWidth = 1.4;
      ctx.beginPath(); ctx.ellipse(0, -2, 3.4, 5.6, 0, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, 3.5); ctx.lineTo(0, 11); ctx.moveTo(-2.6, 11); ctx.lineTo(2.6, 11); ctx.stroke();
      ctx.fillStyle = beacon; ctx.beginPath(); ctx.arc(0, 0.5, 1.2, 0, Math.PI * 2); ctx.fill();
    };
  }

  // A report on the scanner: a warning triangle, amber to red with how bad it is.
  var SEVERITY = { 1: '#c9a23a', 2: '#e08a3f', 3: '#e0574f', 4: '#ff3b3b' };

  function warning(severity) {
    var ratio = 2, size = 30 * ratio;
    var canvas = document.createElement('canvas');
    canvas.width = canvas.height = size;
    var ctx = canvas.getContext('2d');
    ctx.scale(ratio, ratio);
    var tint = SEVERITY[severity];
    ctx.beginPath();
    ctx.moveTo(15, 3); ctx.lineTo(27, 25); ctx.lineTo(3, 25); ctx.closePath();
    ctx.fillStyle = 'rgba(12, 5, 4, 0.92)';
    ctx.shadowColor = tint; ctx.shadowBlur = 7;
    ctx.fill();
    ctx.shadowBlur = 0; ctx.lineWidth = 1.8; ctx.strokeStyle = tint; ctx.lineJoin = 'round';
    ctx.stroke();
    ctx.fillStyle = tint;
    ctx.fillRect(14, 10, 2, 8);
    ctx.fillRect(14, 20, 2, 2);
    return { width: size, height: size, data: ctx.getImageData(0, 0, size, size).data };
  }

  /* --- the style ------------------------------------------------------------ */

  function is(layer) { return ['==', ['get', 'l'], layer]; }
  function road(cls) { return ['all', is('road'), ['==', ['get', 'c'], cls]]; }
  function z(stops) { return ['interpolate', ['exponential', 1.6], ['zoom']].concat(stops); }

  function style() {
    var tints = ['match', ['get', 't']];
    TINTS.forEach(function (c, i) { tints.push(i, c); });
    tints.push(TINTS[0]);
    return {
      version: 8,
      // Paint changes apply at once: the effects animate themselves, and a
      // 300 ms fade on every tick kept the whole map redrawing at frame rate.
      transition: { duration: 0, delay: 0 },
      glyphs: '/static/map/fonts/{fontstack}/{range}.pbf',
      // The night over the city when it tilts: the horizon fades into fog.
      sky: { 'sky-color': '#02050a', 'horizon-color': '#0b2236', 'fog-color': '#02050a',
             'fog-ground-blend': 0.9, 'horizon-fog-blend': 0.35, 'sky-horizon-blend': 0.6, 'atmosphere-blend': 0 },
      sources: {
        // Tiled to z14 and overzoomed past it: stored to 1.5 m, the geometry is no
        // finer, and tiling deeper only cost the workers time on every zoom in.
        city: { type: 'geojson', data: { type: 'FeatureCollection', features: [] }, generateId: true, maxzoom: 14 },
        buildings: { type: 'geojson', data: { type: 'FeatureCollection', features: [] }, maxzoom: 14, buffer: 32 },
        trees: { type: 'geojson', data: { type: 'FeatureCollection', features: [] }, maxzoom: 14, buffer: 32 },
        incidents: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
        trail: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
        movers: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
        // The ground itself: hills shaded in 2D, raised when the map tilts — finer
        // close in (z12–13 only where the map can be panned), so a house sits level.
        terrain: { type: 'raster-dem', tiles: ['/static/map/terrain/{z}/{x}/{y}.png?v=' + (opts.data.version || '0')], tileSize: 256,
                   encoding: 'mapbox', minzoom: 9, maxzoom: 13, bounds: [-0.48, -0.36, 0.38, 0.5] },
        // The same ground for the hillshade: one DEM source serving both shading
        // and 3D terrain renders each worse (MapLibre's own advice).
        relief: { type: 'raster-dem', tiles: ['/static/map/terrain/{z}/{x}/{y}.png?v=' + (opts.data.version || '0')], tileSize: 256,
                  encoding: 'mapbox', minzoom: 9, maxzoom: 13, bounds: [-0.48, -0.36, 0.38, 0.5] }
      },
      layers: [
        { id: 'void', type: 'background', paint: { 'background-color': C.void } },
        // Light off the coast: the shoreline glows into the water.
        { id: 'coast-glow', type: 'line', source: 'city', filter: is('land'),
          paint: { 'line-color': C.edge, 'line-width': z([10, 6, 14, 22, 17, 60]), 'line-blur': z([10, 6, 14, 22, 17, 60]), 'line-opacity': 0.14 } },
        { id: 'land', type: 'fill', source: 'city', filter: is('land'),
          paint: { 'fill-color': ['match', ['get', 'k'], 'mainland', C.mainland, C.land] } },
        { id: 'district', type: 'fill', source: 'city', filter: is('district'),
          paint: { 'fill-color': tints,
                   'fill-opacity': ['case', ['boolean', ['feature-state', 'hover'], false], 1,
                                    ['==', ['get', 'k'], 'outer'], 0, 0.85] } },
        { id: 'district-hover', type: 'line', source: 'city', filter: is('district'),
          paint: { 'line-color': C.lit, 'line-width': 1.6, 'line-blur': 1,
                   'line-opacity': ['case', ['boolean', ['feature-state', 'hover'], false], 0.55, 0] } },
        // Safety, when asked for: each district tinted by how much trouble it sees.
        { id: 'safety', type: 'fill', source: 'city', filter: is('district'), layout: { visibility: 'none' },
          paint: { 'fill-color': ['interpolate', ['linear'], ['get', 'cr'], 0, C.good, 0.5, '#c9a23a', 1, C.alert],
                   'fill-opacity': 0.24 } },
        { id: 'relief', type: 'hillshade', source: 'relief',
          paint: { 'hillshade-exaggeration': 0.4, 'hillshade-shadow-color': '#03080f',
                   'hillshade-highlight-color': '#1d4d6e', 'hillshade-accent-color': '#06121c',
                   'hillshade-illumination-direction': 315 } },
        // The county past the sprawl: fields in their hedgerows and the woods
        // between them — faint, as farmland is at night, but not nothing.
        { id: 'field', type: 'fill', source: 'city', filter: is('field'),
          paint: { 'fill-color': ['match', ['get', 'k'], 'crop', '#0f1d1e', 'pasture', '#0c1c1b', 'orchard', '#0d2220', '#101a21'],
                   'fill-opacity': 0.95 } },
        { id: 'field-edge', type: 'line', source: 'city', filter: is('field'), minzoom: 11.6,
          paint: { 'line-color': '#143a2c', 'line-width': z([11.6, 0.5, 14, 1.6, 17, 5]), 'line-opacity': 0.7 } },
        { id: 'wood', type: 'fill', source: 'city', filter: is('wood'), paint: { 'fill-color': '#09231c', 'fill-opacity': 0.95 } },
        { id: 'wood-edge', type: 'line', source: 'city', filter: is('wood'),
          paint: { 'line-color': C.parkEdge, 'line-width': 0.6, 'line-opacity': 0.25 } },
        { id: 'park', type: 'fill', source: 'city', filter: is('park'), paint: { 'fill-color': C.park, 'fill-opacity': 0.9 } },
        { id: 'plaza', type: 'fill', source: 'city', filter: is('plaza'), paint: { 'fill-color': '#122131', 'fill-opacity': 0.95 } },
        { id: 'plaza-edge', type: 'line', source: 'city', filter: is('plaza'),
          paint: { 'line-color': '#2f6f9c', 'line-width': 0.6, 'line-opacity': 0.5 } },
        { id: 'apron', type: 'fill', source: 'city', filter: is('apron'), paint: { 'fill-color': '#0e1f2e' } },
        { id: 'lot', type: 'fill', source: 'city', filter: is('lot'), paint: { 'fill-color': '#0f1c28', 'fill-opacity': 0.95 } },
        { id: 'lot-edge', type: 'line', source: 'city', filter: is('lot'),
          paint: { 'line-color': '#2f6f9c', 'line-width': 0.6, 'line-opacity': 0.45 } },
        // A car park's stalls: rows drawn as ticks across each one.
        { id: 'bay', type: 'line', source: 'city', filter: is('bay'), minzoom: 14.2,
          paint: { 'line-color': '#3d6a8c', 'line-width': z([14.2, 1.2, 16, 4, 18, 14]), 'line-dasharray': [0.08, 0.55],
                   'line-opacity': 0.75 } },
        { id: 'works', type: 'fill', source: 'city', filter: is('works'), paint: { 'fill-color': '#12171c', 'fill-opacity': 0.95 } },
        { id: 'works-edge', type: 'line', source: 'city', filter: is('works'),
          paint: { 'line-color': '#6b5326', 'line-width': 0.8, 'line-dasharray': [3, 2], 'line-opacity': 0.55 } },
        { id: 'taxiway', type: 'line', source: 'city', filter: is('taxiway'), layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': '#152a3b', 'line-width': z([11, 0.8, 14, 4.5, 16, 18, 18, 72]) } },
        { id: 'taxiway-line', type: 'line', source: 'city', filter: is('taxiway'), minzoom: 13.4,
          paint: { 'line-color': '#c9a23a', 'line-width': z([13.4, 0.4, 17, 1.4]), 'line-opacity': 0.75 } },
        { id: 'runway', type: 'fill', source: 'city', filter: is('runway'), paint: { 'fill-color': '#15293a' } },
        { id: 'runway-line', type: 'line', source: 'city', filter: is('runway_line'),
          paint: { 'line-color': '#8fb9d8', 'line-width': z([11, 0.4, 15, 1.6]), 'line-dasharray': [4, 4], 'line-opacity': 0.7 } },
        // The lights: down both edges of each runway, and the approach lights out past its ends.
        { id: 'edgelights', type: 'line', source: 'city', filter: is('edgelights'), minzoom: 12.4,
          paint: { 'line-color': '#e8f6ff', 'line-width': z([12.4, 1, 16, 3]), 'line-dasharray': [0.2, 3.2], 'line-opacity': 0.8 } },
        { id: 'approach', type: 'line', source: 'city', filter: is('approach'), minzoom: 11.6,
          paint: { 'line-color': '#fff1c4', 'line-width': z([11.6, 1.2, 16, 4]), 'line-dasharray': [0.25, 1.6], 'line-opacity': 0.9,
                   'line-blur': 0.6 } },
        { id: 'park-edge', type: 'line', source: 'city', filter: is('park'),
          paint: { 'line-color': C.parkEdge, 'line-width': 0.8, 'line-opacity': 0.4 } },
        // Slaughter Swamp: black water and reeds.
        { id: 'marsh', type: 'fill', source: 'city', filter: is('marsh'), paint: { 'fill-color': '#0a1f1b', 'fill-opacity': 0.92 } },
        { id: 'marsh-edge', type: 'line', source: 'city', filter: is('marsh'),
          paint: { 'line-color': '#2f6f5a', 'line-width': 0.8, 'line-dasharray': [1, 2], 'line-opacity': 0.5 } },
        // The country club: fairways, greens, tees and bunkers.
        { id: 'golf', type: 'fill', source: 'city', filter: is('golf'),
          paint: { 'fill-color': ['match', ['get', 'k'], 'green', '#1f7a52', 'tee', '#1a6a48', 'bunker', '#4d4128', '#14523a'],
                   'fill-opacity': 0.95 } },
        // Where the city plays: the turf, the diamond's dirt, the courts and the track — and their lines.
        { id: 'pitch', type: 'fill', source: 'city', filter: is('pitch'),
          paint: { 'fill-color': ['match', ['get', 'k'], 'dirt', '#3a2a1a', 'court', '#123047', 'track', '#3a2420', '#0f3b2c'],
                   'fill-opacity': 0.95 } },
        { id: 'pitch-line', type: 'line', source: 'city', filter: is('pitch_line'), minzoom: 13.6,
          paint: { 'line-color': '#d6ecf8', 'line-width': z([13.6, 0.4, 16, 1, 18, 2.4]), 'line-opacity': 0.6 } },
        { id: 'path', type: 'line', source: 'city', filter: is('path'), minzoom: 13.8,
          paint: { 'line-color': '#4e8f72', 'line-width': z([13.8, 0.5, 17, 2.6]), 'line-dasharray': [2, 1.2], 'line-opacity': 0.6 } },
        // Ponds in the parks (rivers and lakes are simply where the land isn't).
        { id: 'water', type: 'fill', source: 'city', filter: is('water'), paint: { 'fill-color': C.water } },
        { id: 'water-edge', type: 'line', source: 'city', filter: is('water'),
          paint: { 'line-color': C.edge, 'line-width': z([10, 0.6, 14, 1.2, 17, 2.4]), 'line-opacity': 0.7 } },
        // Amusement Mile: Gotham's one beach, the boardwalk behind it, the fairground's lights.
        { id: 'beach', type: 'fill', source: 'city', filter: is('beach'), paint: { 'fill-color': '#2e2a1e', 'fill-opacity': 0.95 } },
        { id: 'beach-edge', type: 'line', source: 'city', filter: is('beach'),
          paint: { 'line-color': '#e0c48a', 'line-width': z([12, 0.4, 16, 1.2]), 'line-opacity': 0.35, 'line-blur': 1 } },
        { id: 'fair', type: 'fill', source: 'city', filter: is('fair'), paint: { 'fill-color': '#1e1426', 'fill-opacity': 0.92 } },
        { id: 'fair-edge', type: 'line', source: 'city', filter: is('fair'),
          paint: { 'line-color': '#ff5fa2', 'line-width': z([12, 0.5, 16, 1.4]), 'line-opacity': 0.45, 'line-dasharray': [1, 2] } },
        { id: 'boardwalk', type: 'line', source: 'city', filter: is('boardwalk'), minzoom: 12,
          layout: { 'line-cap': 'butt' },
          paint: { 'line-color': '#8a6a3a', 'line-width': z([12, 1, 15, 3, 18, 9]), 'line-dasharray': [0.35, 0.12], 'line-opacity': 0.9 } },
        { id: 'yard', type: 'fill', source: 'city', filter: is('yard'), paint: { 'fill-color': '#101c27', 'fill-opacity': 0.95 } },
        { id: 'site', type: 'fill', source: 'city', filter: is('site'), paint: { 'fill-color': '#2a2414', 'fill-opacity': 0.9 } },
        { id: 'site-edge', type: 'line', source: 'city', filter: is('site'),
          paint: { 'line-color': '#c9a23a', 'line-width': 0.8, 'line-dasharray': [2, 2], 'line-opacity': 0.6 } },
        // Light on the water, twinkling (animated in the loop below): eight groups,
        // each its own rhythm, so every tick sets eight numbers rather than
        // re-evaluating 2,600 points.
      ].concat(SPARKLE.map(function (k) {
        return { id: 'sparkle-' + k, type: 'circle', source: 'city', minzoom: 11,
                 filter: ['all', is('sparkle'), ['==', ['%', ['floor', ['*', ['get', 'p'], SPARKLE.length / (2 * Math.PI)]], SPARKLE.length], k]],
                 paint: { 'circle-radius': z([11, 0.6, 15, 1.6, 18, 2.6]), 'circle-color': C.lit, 'circle-opacity': 0, 'circle-blur': 0.4 } };
      })).concat([
        { id: 'ferry', type: 'line', source: 'city', filter: is('ferry'),
          paint: { 'line-color': '#3a7aa8', 'line-width': z([10, 0.6, 15, 1.4]), 'line-dasharray': [1, 3], 'line-opacity': 0.75 } },
        // The shipping channels in from the sea: marked faintly, as on a chart.
        { id: 'shipping', type: 'line', source: 'city', filter: is('lane'), minzoom: 10.5,
          paint: { 'line-color': '#1d4766', 'line-width': z([10.5, 0.6, 15, 1.6]), 'line-dasharray': [6, 5], 'line-opacity': 0.45 } },
        { id: 'pier', type: 'fill', source: 'city', filter: is('pier'), paint: { 'fill-color': '#163650', 'fill-opacity': 0.95 } },
        // The ferries on their routes, a wake behind them.
        { id: 'boat', type: 'symbol', source: 'movers', filter: ['==', ['get', 'm'], 'ferry'], minzoom: 11,
          layout: { 'icon-image': 'gm-boat', 'icon-size': z([11, 0.7, 15, 1.1, 18, 1.5]), 'icon-rotate': ['get', 'r'],
                    'icon-rotation-alignment': 'map', 'icon-allow-overlap': true, 'icon-ignore-placement': true } },
        { id: 'ship', type: 'symbol', source: 'movers', filter: ['==', ['get', 'm'], 'ship'], minzoom: 10.5,
          layout: { 'icon-image': 'gm-ship', 'icon-size': z([10.5, 0.55, 15, 1.2, 18, 2.4]), 'icon-rotate': ['get', 'r'],
                    'icon-rotation-alignment': 'map', 'icon-allow-overlap': true, 'icon-ignore-placement': true } },
        { id: 'tug', type: 'symbol', source: 'movers', filter: ['==', ['get', 'm'], 'tug'], minzoom: 12,
          layout: { 'icon-image': 'gm-tug', 'icon-size': z([12, 0.6, 16, 1.2]), 'icon-rotate': ['get', 'r'],
                    'icon-rotation-alignment': 'map', 'icon-allow-overlap': true, 'icon-ignore-placement': true } },
        { id: 'sail', type: 'symbol', source: 'movers', filter: ['==', ['get', 'm'], 'sail'], minzoom: 12.4,
          layout: { 'icon-image': 'gm-sail', 'icon-size': z([12.4, 0.6, 16, 1.3]), 'icon-rotate': ['get', 'r'],
                    'icon-rotation-alignment': 'map', 'icon-allow-overlap': true, 'icon-ignore-placement': true } },
        { id: 'patrol', type: 'symbol', source: 'movers', filter: ['==', ['get', 'm'], 'patrol'], minzoom: 11.6,
          layout: { 'icon-image': 'gm-launch', 'icon-size': z([11.6, 0.6, 16, 1.2]), 'icon-rotate': ['get', 'r'],
                    'icon-rotation-alignment': 'map', 'icon-allow-overlap': true, 'icon-ignore-placement': true } },
        { id: 'coast', type: 'line', source: 'city', filter: is('land'),
          paint: { 'line-color': C.edge, 'line-width': z([10, 0.8, 14, 1.6, 17, 3]), 'line-opacity': 0.75 } },
        // Roads, quietest first. Widths grow with the zoom, as on any good map.
        // Streets show from far out as texture — the city's grain — and widen in.
        { id: 'street', type: 'line', source: 'city', filter: road('street'), minzoom: 10.6,
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': C.street, 'line-width': z([10.6, 0.25, 13, 0.6, 14, 0.9, 16, 3, 18, 10]),
                   'line-opacity': z([10.6, 0.35, 12.5, 0.75, 13.5, 0.95]) } },
        // Out in the sprawl: lanes, fainter the further they are from anywhere.
        { id: 'lane', type: 'line', source: 'city', filter: road('lane'), minzoom: 11,
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': '#143047', 'line-width': z([11, 0.3, 14, 0.8, 16, 2, 18, 6]),
                   'line-opacity': z([11, 0.3, 13, 0.7]) } },
        { id: 'drive', type: 'line', source: 'city', filter: road('drive'), minzoom: 12.4,
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': '#2a5677', 'line-width': z([12.4, 0.4, 16, 2.4]), 'line-dasharray': [2, 1] } },
        { id: 'secondary', type: 'line', source: 'city', filter: road('secondary'),
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': C.secondary, 'line-width': z([10, 0.35, 14, 1.6, 16, 4.5, 18, 14]),
                   'line-opacity': z([10, 0.45, 13, 0.9]) } },
        { id: 'avenue', type: 'line', source: 'city', filter: road('avenue'), minzoom: 11.2,
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': '#21486a', 'line-width': z([11.2, 0.4, 14, 1.3, 16, 4, 18, 12]),
                   'line-opacity': z([11.2, 0.5, 13, 0.95]) } },
        { id: 'primary-glow', type: 'line', source: 'city', filter: road('primary'),
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': C.primary, 'line-width': z([10, 3, 14, 7, 17, 22]), 'line-blur': z([10, 3, 14, 7, 17, 22]), 'line-opacity': 0.35 } },
        { id: 'primary', type: 'line', source: 'city', filter: road('primary'),
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': C.primary, 'line-width': z([10, 0.7, 14, 2.4, 16, 6, 18, 18]) } },
        // The railways, drawn as railways: a dark bed with the sleepers across it —
        // nothing like a road, and nothing like the coloured lines underground.
        { id: 'railbridge', type: 'line', source: 'city', filter: is('railbridge'), layout: { 'line-cap': 'butt' },
          paint: { 'line-color': '#0d2133', 'line-width': z([10, 2.2, 14, 6, 17, 16]) } },
        { id: 'railbridge-truss', type: 'line', source: 'city', filter: is('railbridge'), minzoom: 13,
          paint: { 'line-color': '#4f7896', 'line-width': z([13, 2, 17, 12]), 'line-dasharray': [0.3, 0.6], 'line-opacity': 0.45 } },
        { id: 'rail-bed', type: 'line', source: 'city', filter: road('rail'),
          paint: { 'line-color': '#1d2c3a', 'line-width': z([10, 1, 14, 2.4, 17, 6]), 'line-opacity': 0.9 } },
        { id: 'rail', type: 'line', source: 'city', filter: road('rail'),
          paint: { 'line-color': '#7a9bb8', 'line-width': z([10, 0.6, 14, 1.6, 17, 4.5]), 'line-dasharray': [0.25, 0.9],
                   'line-opacity': 0.85 } },
        // A train on the surface: three cars, their headlight on (see gothamtraffic.js).
        { id: 'train-car', type: 'symbol', source: 'movers', filter: ['==', ['get', 'm'], 'train'], minzoom: 11.4,
          layout: { 'icon-image': 'gm-train', 'icon-size': z([11.4, 0.4, 15, 0.9, 18, 2.4]), 'icon-rotate': ['get', 'r'],
                    'icon-rotation-alignment': 'map', 'icon-allow-overlap': true, 'icon-ignore-placement': true } },
        // The skyway stands above the street: its shadow falls beside it.
        { id: 'skyway-shadow', type: 'line', source: 'city', filter: ['all', road('highway'), ['has', 'e']],
          paint: { 'line-color': '#000', 'line-width': z([10, 2, 14, 5, 17, 14]), 'line-translate': [4, 5],
                   'line-blur': 3, 'line-opacity': 0.6 } },
        { id: 'highway-glow', type: 'line', source: 'city', filter: ['any', road('highway'), road('bridge')],
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': C.highway, 'line-width': z([10, 5, 14, 11, 17, 30]), 'line-blur': z([10, 5, 14, 11, 17, 30]), 'line-opacity': 0.32 } },
        { id: 'bridge-deck', type: 'line', source: 'city', filter: road('bridge'),
          layout: { 'line-cap': 'butt', 'line-join': 'round' },
          paint: { 'line-color': '#0d2133', 'line-width': z([10, 2.4, 14, 6, 16, 12, 18, 30]) } },
        { id: 'highway', type: 'line', source: 'city', filter: ['any', road('highway'), road('bridge')],
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': C.highway, 'line-width': z([10, 1, 14, 2.6, 16, 6, 18, 18]) } },
        // The city in three dimensions: every block, its height from its district.
        // Footprints first, as lit outlines — the hologram's crisp edges at street level.
        { id: 'parked', type: 'symbol', source: 'city', filter: ['all', is('gate'), ['!=', ['%', ['id'], 5], 0]], minzoom: 12.8,
          layout: { 'icon-image': 'gm-plane', 'icon-size': z([12.8, 0.35, 15, 0.75, 18, 2.6]), 'icon-rotate': ['get', 'r'],
                    'icon-rotation-alignment': 'map', 'icon-allow-overlap': true, 'icon-ignore-placement': true } },
        { id: 'footprint', type: 'line', source: 'buildings', minzoom: 13.6,
          paint: { 'line-color': ['case', ['has', 'n'], C.lit, '#2f6f9c'], 'line-width': z([13.4, 0.3, 16, 1, 18, 1.6]),
                   'line-opacity': z([13.4, 0, 14.2, 0.55]) } },
        // The city in three dimensions: dark volumes, brightening with height;
        // the landmarks lit, the skyway a deck in the air.
        { id: 'buildings', type: 'fill-extrusion', source: 'buildings', minzoom: 12.6,
          paint: {
            'fill-extrusion-color': ['match', ['get', 'k'], '~landmark', '#7ccaf5', '~deck', '#2a6a94',
              '~container', ['match', ['%', ['get', 'h'], 2], 0, '#6b5326', '#2f5068'], '~lit', '#3a86bd',
              '~tank', '#3d5568', '~pad', '#8fd3ff', '~crane', '#c9a23a', '~frame', '#2b3a47', '~stall', '#3f6f8f', '~stall-red', '#a8343a', '~stall-gold', '#b58a2e', '~gate', '#c0392b', '~ride', '#ff5fa2', '~wheel', '#e8d7b0',
              // Each quarter's own stuff: the old town's stone and brownstone, downtown's
              // glass, the works' steel, the suburbs' painted houses.
              '~old', ['interpolate', ['linear'], ['get', 'h'], 0, '#13171f', 30, '#1c2230', 70, '#283044'],
              '~glass', ['interpolate', ['linear'], ['get', 'h'], 40, '#123a5c', 120, '#1f5f92', 260, '#3f9ee2'],
              '~works', ['interpolate', ['linear'], ['get', 'h'], 0, '#181d21', 14, '#262c31'],
              '~house', '#1b2733',
              // The works' own: acid in the vats, a lit sign, brick stacks, garages, a copper roof gone green.
              '~vat', '#5fd08a', '~sign', '#ffd27f', '~stack', '#4a3430', '~garage', '#22313d', '~copper', '#3f8f7a',
              '~rock', '#2a323b', '~pier', '#1b3a52',
              ['interpolate', ['linear'], ['get', 'h'], 0, '#081521', 25, '#0b2133', 60, '#10334d', 120, '#184d73', 240, '#2f78ad']],
            'fill-extrusion-height': ['interpolate', ['linear'], ['zoom'], 12.6, 0, 13.8, ['get', 'h']],
            'fill-extrusion-base': ['interpolate', ['linear'], ['zoom'], 12.6, 0, 13.8, ['get', 'b']],
            'fill-extrusion-opacity': 1,      // solid: a translucent extrusion is drawn twice, and looked the same
            'fill-extrusion-vertical-gradient': true
          } },
        // Trees: a trunk, and a crown in two tiers that rounds off at the top.
        { id: 'trees', type: 'fill-extrusion', source: 'trees', minzoom: 13, filter: ['!=', ['get', 'p'], 0],
          paint: { 'fill-extrusion-color': ['interpolate', ['linear'], ['get', 'h'], 8, ['case', ['==', ['get', 'p'], 2], '#17573f', '#0f3d2d'],
                                            17, ['case', ['==', ['get', 'p'], 2], '#2a8a62', '#1d6b4c']],
                   'fill-extrusion-height': ['interpolate', ['linear'], ['zoom'], 13, 0, 14, ['get', 'top']],
                   'fill-extrusion-base': ['interpolate', ['linear'], ['zoom'], 13, 0, 14, ['get', 'b']],
                   'fill-extrusion-opacity': 1 } },
        // Their trunks, only close enough to see under the crowns.
        { id: 'trunks', type: 'fill-extrusion', source: 'trees', minzoom: 15, filter: ['==', ['get', 'p'], 0],
          paint: { 'fill-extrusion-color': '#13261f', 'fill-extrusion-height': ['get', 'top'], 'fill-extrusion-opacity': 1 } },
        // The subway as a transit map: clean coloured lines on a dark casing, its trains
        // riding them as dots — over the city at a distance only. They're gone by the time
        // the buildings rise, flat or tilted, leaving the stations as dots: lines across
        // the roofs looked like paint spilled on the city.
        { id: 'subway-casing', type: 'line', source: 'city', filter: is('subway'), minzoom: 10.8, maxzoom: 12.8,
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': C.void, 'line-width': z([10.8, 2.6, 12.8, 5]),
                   'line-opacity': ['interpolate', ['linear'], ['zoom'], 12.1, 0.9, 12.7, 0] } },
        { id: 'subway', type: 'line', source: 'city', filter: is('subway'), minzoom: 10.8, maxzoom: 12.8,
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': ['get', 'col'], 'line-width': z([10.8, 1.3, 12.8, 2.6]),
                   'line-opacity': ['interpolate', ['linear'], ['zoom'], 12.1, 0.95, 12.7, 0] } },
        { id: 'metro', type: 'circle', source: 'movers', filter: ['==', ['get', 'm'], 'metro'], minzoom: 11, maxzoom: 12.8,
          paint: { 'circle-radius': z([11, 2, 12.8, 3.2]), 'circle-color': '#f4faff',
                   'circle-stroke-color': ['get', 'col'], 'circle-stroke-width': z([11, 1, 12.8, 1.6]),
                   'circle-opacity': ['interpolate', ['linear'], ['zoom'], 12.1, 1, 12.7, 0],
                   'circle-stroke-opacity': ['interpolate', ['linear'], ['zoom'], 12.1, 1, 12.7, 0] } },
        // Aircraft: the shadow on the ground, falling further off as it climbs, then the plane.
        { id: 'plane-shadow', type: 'symbol', source: 'movers', filter: ['==', ['get', 'm'], 'plane-shadow'], minzoom: 10.5,
          layout: { 'icon-image': 'gm-plane-shadow', 'icon-size': z([10.5, 0.5, 15, 1.1, 18, 2.6]), 'icon-rotate': ['get', 'r'],
                    'icon-rotation-alignment': 'map', 'icon-allow-overlap': true, 'icon-ignore-placement': true } },
        { id: 'plane', type: 'symbol', source: 'movers', filter: ['==', ['get', 'm'], 'plane'], minzoom: 10.5,
          layout: { 'icon-image': 'gm-plane', 'icon-rotate': ['get', 'r'], 'icon-rotation-alignment': 'map',
                    'icon-size': ['interpolate', ['linear'], ['zoom'], 10.5, ['+', 0.5, ['*', 0.35, ['get', 's']]],
                                  15, ['+', 1.1, ['*', 0.6, ['get', 's']]], 18, ['+', 2.6, ['*', 1.2, ['get', 's']]]],
                    'icon-allow-overlap': true, 'icon-ignore-placement': true } },
        // Where someone has been.
        { id: 'trail-ahead', type: 'line', source: 'trail', filter: ['all', ['==', ['geometry-type'], 'LineString'], ['==', ['get', 'k'], 'ahead']],
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': ['get', 'color'], 'line-width': 2, 'line-dasharray': [0.2, 2], 'line-opacity': 0.75 } },
        { id: 'trail', type: 'line', source: 'trail', filter: ['all', ['==', ['geometry-type'], 'LineString'], ['!=', ['get', 'k'], 'ahead']],
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': ['get', 'color'], 'line-width': 2.4, 'line-dasharray': [1.5, 1.8], 'line-opacity': 0.9 } },
        { id: 'trail-stop', type: 'circle', source: 'trail', filter: ['==', ['geometry-type'], 'Point'],
          paint: { 'circle-radius': 4, 'circle-color': C.void, 'circle-stroke-color': ['get', 'color'], 'circle-stroke-width': 1.8 } },
        // The city's life: neon pooled under the clubs and bars, then the places themselves.
        { id: 'venue-glow', type: 'circle', source: 'city', minzoom: 12.6,
          filter: ['all', is('venue'), ['in', ['get', 'k'], ['literal', ['club', 'bar']]]],
          paint: { 'circle-radius': z([12.6, 6, 16, 22]), 'circle-blur': 1,
                   'circle-color': ['match', ['get', 'k'], 'club', '#c77dff', '#e0a43a'], 'circle-opacity': 0.35,
                   'circle-pitch-alignment': 'map' } },
        { id: 'venue', type: 'symbol', source: 'city', filter: is('venue'), minzoom: 13.4,
          layout: { 'icon-image': ['concat', 'gm-', ['get', 'k']], 'icon-size': z([13.4, 0.5, 16, 0.75]),
                    'icon-allow-overlap': false, 'icon-padding': 1,
                    'text-field': ['step', ['zoom'], '', 15.2, ['get', 'n']], 'text-font': ['Noto Sans Italic'],
                    'text-size': 10, 'text-offset': [0, 1.3], 'text-anchor': 'top', 'text-optional': true },
          paint: { 'text-color': ['match', ['get', 'k'], 'club', '#d7a8ff', 'bar', '#f0c880', '#9cc4d8'],
                   'text-halo-color': C.void, 'text-halo-width': 1.4 } },
        // Smoke over a fire, gas over a gas attack (animated below).
        { id: 'smoke', type: 'circle', source: 'incidents',
          filter: ['in', ['get', 'kind'], ['literal', ['Arson', 'Explosion reported', 'Chemical spill', 'Toxin exposure',
                                                        'Laughing-gas attack', 'Freezing incident']]],
          paint: { 'circle-radius': 26, 'circle-blur': 1, 'circle-pitch-alignment': 'map',
                   'circle-color': ['match', ['get', 'kind'], 'Laughing-gas attack', '#7dff6a', 'Toxin exposure', '#a7ff3a',
                                    'Chemical spill', '#d4ff4a', 'Freezing incident', '#bfe9ff', '#9a8f86'],
                   'circle-opacity': 0.3 } },
        // Trouble, as the scanner has it: a pulse under each report.
        { id: 'incident-pulse', type: 'circle', source: 'incidents',
          paint: { 'circle-radius': 10, 'circle-color': ['match', ['get', 'severity'], 1, SEVERITY[1], 2, SEVERITY[2], 3, SEVERITY[3], SEVERITY[4]],
                   'circle-opacity': 0.25, 'circle-blur': 0.6, 'circle-pitch-alignment': 'map' } },
        // Someone's on it: a ring in their colour.
        { id: 'case-ring', type: 'circle', source: 'incidents', filter: ['has', 'assignee'],
          paint: { 'circle-radius': 15, 'circle-color': 'rgba(0,0,0,0)', 'circle-stroke-color': ['get', 'acc'],
                   'circle-stroke-width': 2, 'circle-pitch-alignment': 'map' } },
        { id: 'incident', type: 'symbol', source: 'incidents',
          layout: { 'icon-image': ['concat', 'gm-warn-', ['to-string', ['get', 'severity']]], 'icon-size': z([10.6, 0.6, 14, 0.85, 17, 1]),
                    'icon-allow-overlap': true, 'icon-anchor': 'bottom' } },
        // A police helicopter over the worst report, its searchlight on the street.
        { id: 'heli-light', type: 'circle', source: 'movers', filter: ['==', ['get', 'm'], 'light'], minzoom: 11,
          paint: { 'circle-radius': z([11, 9, 15, 36, 18, 96]), 'circle-color': '#fff1c4', 'circle-opacity': 0.2,
                   'circle-blur': 0.85, 'circle-pitch-alignment': 'map' } },
        { id: 'heli-spot', type: 'circle', source: 'movers', filter: ['==', ['get', 'm'], 'light'], minzoom: 11,
          paint: { 'circle-radius': z([11, 3, 15, 12, 18, 32]), 'circle-color': '#fff6dc', 'circle-opacity': 0.32,
                   'circle-blur': 0.6, 'circle-pitch-alignment': 'map' } },
        { id: 'heli', type: 'symbol', source: 'movers', filter: ['==', ['get', 'm'], 'heli'], minzoom: 11,
          layout: { 'icon-image': 'gm-heli', 'icon-size': z([11, 0.8, 15, 1.2, 18, 1.6]), 'icon-rotate': ['get', 'r'],
                    'icon-rotation-alignment': 'map', 'icon-allow-overlap': true, 'icon-ignore-placement': true } },
        { id: 'chopper', type: 'symbol', source: 'movers', minzoom: 11,
          filter: ['in', ['get', 'm'], ['literal', ['heli-news', 'heli-med', 'heli-civ']]],
          layout: { 'icon-image': ['concat', 'gm-', ['get', 'm']], 'icon-size': z([11, 0.7, 15, 1.1, 18, 1.5]),
                    'icon-rotate': ['get', 'r'], 'icon-rotation-alignment': 'map', 'icon-allow-overlap': true,
                    'icon-ignore-placement': true } },
        { id: 'airship-shadow', type: 'symbol', source: 'movers', filter: ['==', ['get', 'm'], 'airship-shadow'], minzoom: 10.5,
          layout: { 'icon-image': 'gm-airship-shadow', 'icon-size': z([10.5, 0.7, 15, 1.6, 18, 4]), 'icon-rotate': ['get', 'r'],
                    'icon-rotation-alignment': 'map', 'icon-allow-overlap': true, 'icon-ignore-placement': true } },
        { id: 'airship', type: 'symbol', source: 'movers', filter: ['==', ['get', 'm'], 'airship'], minzoom: 10.5,
          layout: { 'icon-image': 'gm-airship', 'icon-size': z([10.5, 0.8, 15, 1.8, 18, 4.4]), 'icon-rotate': ['get', 'r'],
                    'icon-rotation-alignment': 'map', 'icon-allow-overlap': true, 'icon-ignore-placement': true } },
        // Lanterns strung over the night market's aisles: warm dots, brighter after dark.
        { id: 'lantern', type: 'line', source: 'city', filter: is('lantern'), minzoom: 14.6,
          layout: { 'line-cap': 'round' },
          paint: { 'line-color': '#ffb347', 'line-width': z([14.6, 1.6, 17, 4, 18, 6]), 'line-dasharray': [0.05, 1.6],
                   'line-opacity': 0.85, 'line-blur': 0.6 } },
        { id: 'batwing-shadow', type: 'symbol', source: 'movers', filter: ['==', ['get', 'm'], 'batwing-shadow'], minzoom: 10.5,
          layout: { 'icon-image': 'gm-batwing-shadow', 'icon-size': z([10.5, 0.5, 15, 1.1, 18, 2.4]), 'icon-rotate': ['get', 'r'],
                    'icon-rotation-alignment': 'map', 'icon-allow-overlap': true, 'icon-ignore-placement': true } },
        { id: 'batwing', type: 'symbol', source: 'movers', filter: ['==', ['get', 'm'], 'batwing'], minzoom: 10.5,
          layout: { 'icon-image': 'gm-batwing', 'icon-size': z([10.5, 0.6, 15, 1.25, 18, 2.8]), 'icon-rotate': ['get', 'r'],
                    'icon-rotation-alignment': 'map', 'icon-allow-overlap': true, 'icon-ignore-placement': true } },
        { id: 'beam', type: 'fill', source: 'movers', filter: ['==', ['get', 'm'], 'beam'],
          paint: { 'fill-color': '#fff1c4', 'fill-opacity': ['get', 'o'] } },
        // Stations as roundels: white, ringed in their line's colour — the entrances, at any zoom.
        { id: 'station', type: 'circle', source: 'city', filter: is('station'), minzoom: 11.4,
          paint: { 'circle-radius': z([11.4, 2.4, 14, 4, 17, 6.5]), 'circle-color': '#f4faff',
                   'circle-stroke-color': ['get', 'col'], 'circle-stroke-width': z([11.4, 1.2, 15, 2.4]) } },
        { id: 'railstation', type: 'circle', source: 'city', filter: is('railstation'), minzoom: 11.8,
          paint: { 'circle-radius': z([11.8, 2.4, 15, 5, 18, 7]), 'circle-color': '#e8f6ff',
                   'circle-stroke-color': '#02050a', 'circle-stroke-width': z([11.8, 1, 15, 2]) } },
        { id: 'railstation-label', type: 'symbol', source: 'city', filter: is('railstation'), minzoom: 13.8,
          layout: { 'text-field': ['get', 'n'], 'text-font': ['Noto Sans Medium'], 'text-size': 10.5, 'text-offset': [0, 1.2],
                    'text-anchor': 'top', 'text-optional': true },
          paint: { 'text-color': '#e8f6ff', 'text-halo-color': C.void, 'text-halo-width': 1.5 } },
        { id: 'station-label', type: 'symbol', source: 'city', filter: is('station'), minzoom: 12.8,
          layout: { 'text-field': ['get', 'n'], 'text-font': ['Noto Sans Regular'], 'text-size': 10, 'text-offset': [0, 1.1],
                    'text-anchor': 'top', 'text-optional': true },
          paint: { 'text-color': ['get', 'col'], 'text-halo-color': C.void, 'text-halo-width': 1.4 } },
        // Words on top, laid out so they never collide.
        // Moving lights along the highways and avenues (animated below): the city never stops.
        { id: 'traffic', type: 'line', source: 'city', filter: ['any', road('highway'), road('bridge'), road('primary')],
          minzoom: 11.4, layout: { 'line-cap': 'round' },
          paint: { 'line-color': '#e8f6ff', 'line-width': z([11.4, 0.6, 15, 1.6, 18, 3]), 'line-opacity': 0.55,
                   'line-dasharray': [0.1, 6] } },
        // The rest of the city's traffic: headlights one way, tail lights the other, on every avenue.
        { id: 'traffic-avenue', type: 'line', source: 'city', filter: ['any', road('avenue'), road('secondary')],
          minzoom: 13.2, layout: { 'line-cap': 'round' },
          paint: { 'line-color': '#ffd9a8', 'line-width': z([13.2, 0.5, 16, 1.4, 18, 2.4]), 'line-opacity': 0.42,
                   'line-dasharray': [0.1, 7], 'line-offset': z([13.2, 0.4, 16, 1.6, 18, 4]) } },
        { id: 'traffic-tail', type: 'line', source: 'city', filter: ['any', road('avenue'), road('secondary'), road('primary')],
          minzoom: 13.2, layout: { 'line-cap': 'round' },
          paint: { 'line-color': '#ff5a4f', 'line-width': z([13.2, 0.5, 16, 1.3, 18, 2.2]), 'line-opacity': 0.38,
                   'line-dasharray': [0.1, 8], 'line-offset': z([13.2, -0.4, 16, -1.6, 18, -4]) } },
        // The subway lines' names along them, as a transit map has them.
        { id: 'subway-label', type: 'symbol', source: 'city', filter: is('subway'), minzoom: 11.4, maxzoom: 12.7,
          layout: { 'symbol-placement': 'line', 'text-field': ['upcase', ['get', 'n']], 'text-font': ['Noto Sans Medium'],
                    'text-size': z([11.4, 9, 12.7, 10]), 'text-letter-spacing': 0.2, 'symbol-spacing': 520, 'text-max-angle': 25 },
          paint: { 'text-color': ['get', 'col'], 'text-halo-color': C.void, 'text-halo-width': 1.8,
                   'text-opacity': ['interpolate', ['linear'], ['zoom'], 12.1, 1, 12.6, 0] } },
        { id: 'water-label', type: 'symbol', source: 'city', filter: is('water_label'),
          layout: { 'text-field': ['get', 'n'], 'text-font': ['Noto Sans Italic'],
                    'text-size': ['interpolate', ['linear'], ['zoom'], 10, ['case', ['has', 's'], 9, 11], 12, ['case', ['has', 's'], 9, 13.5],
                                   14, ['case', ['has', 's'], 11, 16], 15, ['case', ['has', 's'], 12, 16]],
                    'text-letter-spacing': 0.35, 'text-rotate': ['get', 'r'], 'text-max-width': 30 },
          paint: { 'text-color': '#3a7aa8', 'text-opacity': 0.85 } },
        // The piers' numbers at their ends, the way the waterfront knows them.
        { id: 'pier-label', type: 'symbol', source: 'city', filter: is('pier_label'), minzoom: 15.4,
          layout: { 'text-field': ['upcase', ['get', 'n']], 'text-font': ['Noto Sans Medium'], 'text-size': z([15.4, 8.5, 18, 11]),
                    'text-letter-spacing': 0.18, 'text-rotate': ['get', 'r'], 'text-rotation-alignment': 'map',
                    'text-allow-overlap': false },
          paint: { 'text-color': '#7fa9c8', 'text-halo-color': C.void, 'text-halo-width': 1.4, 'text-opacity': 0.9 } },
        { id: 'road-label', type: 'symbol', source: 'city', minzoom: 13.6,
          filter: ['all', is('road'), ['has', 'n'], ['!=', ['get', 'c'], 'street']],
          layout: { 'symbol-placement': 'line', 'text-field': ['upcase', ['get', 'n']], 'text-font': ['Noto Sans Medium'],
                    'text-size': z([13.6, 9, 17, 13]), 'text-letter-spacing': 0.18, 'symbol-spacing': 420, 'text-max-angle': 30 },
          paint: { 'text-color': '#8fb9d8', 'text-halo-color': C.void, 'text-halo-width': 1.6 } },
        { id: 'area-label', type: 'symbol', source: 'city', filter: is('area_label'),
          layout: { 'text-field': ['upcase', ['get', 'n']], 'text-font': ['Noto Sans Medium'], 'text-size': z([10, 13, 14, 22]),
                    'text-letter-spacing': 0.6 },
          paint: { 'text-color': '#7f98ad', 'text-opacity': 0.55, 'text-halo-color': C.void, 'text-halo-width': 2 } },
        { id: 'district-label', type: 'symbol', source: 'city',
          filter: ['all', is('place'), ['==', ['get', 'k'], 'district']],
          layout: { 'text-field': ['upcase', ['get', 'n']], 'text-font': ['Noto Sans Medium'], 'text-size': z([11, 9, 13.5, 12, 16, 16]),
                    'text-letter-spacing': 0.32, 'text-max-width': 7, 'text-padding': 6 },
          paint: { 'text-color': C.label, 'text-halo-color': C.void, 'text-halo-width': 1.8,
                   'text-opacity': z([10.5, 0.4, 12.5, 0.95]) } },
        { id: 'quarter-label', type: 'symbol', source: 'city', minzoom: 14.2,
          filter: ['all', is('place'), ['==', ['get', 'k'], 'quarter']],
          layout: { 'text-field': ['upcase', ['get', 'n']], 'text-font': ['Noto Sans Regular'], 'text-size': 10.5,
                    'text-letter-spacing': 0.24 },
          paint: { 'text-color': C.faint, 'text-halo-color': C.void, 'text-halo-width': 1.5 } },
        { id: 'landmark', type: 'symbol', source: 'city', minzoom: 12.4,
          filter: ['all', is('place'), ['==', ['get', 'k'], 'landmark']],
          layout: { 'icon-image': ['concat', 'gm-', ['get', 'i']], 'icon-size': z([12.4, 0.62, 15, 0.9, 17, 1.1]),
                    'icon-allow-overlap': false, 'icon-padding': 2,
                    'text-field': ['step', ['zoom'], '', 14.4, ['get', 'n']], 'text-font': ['Noto Sans Regular'],
                    'text-size': 11, 'text-offset': [0, 1.5], 'text-anchor': 'top', 'text-optional': true,
                    'text-max-width': 9 },
          paint: { 'text-color': '#b9d3e6', 'text-halo-color': C.void, 'text-halo-width': 1.6 } },
        { id: 'spot', type: 'symbol', source: 'city', minzoom: 14.2,
          filter: ['all', is('place'), ['==', ['get', 'k'], 'spot']],
          layout: { 'icon-image': ['concat', 'gm-', ['get', 'i']], 'icon-size': z([14.2, 0.5, 16, 0.75, 18, 0.95]),
                    'icon-allow-overlap': false, 'icon-padding': 2,
                    'text-field': ['step', ['zoom'], '', 15.4, ['get', 'n']], 'text-font': ['Noto Sans Italic'],
                    'text-size': 10.5, 'text-offset': [0, 1.35], 'text-anchor': 'top', 'text-optional': true, 'text-max-width': 9 },
          paint: { 'text-color': '#9fc0d6', 'text-halo-color': C.void, 'text-halo-width': 1.5 } }
      ])
    };
  }

  function loadCity() {
    // All three at once — then built in order, so the city draws first.
    var files = ['gotham.geojson', 'gotham-buildings.json', 'gotham-trees.json'].map(function (name) {
      return fetch('/static/map/' + name + '?v=' + (opts.data.version || '0')).then(function (r) { return r.json(); });
    });
    return files[0].then(function (fc) {
      fc.features.forEach(function (f) { f.geometry.coordinates = convert(f.geometry.coordinates); });
      map.getSource('city').setData(fc);
      if (window.GothamTraffic) GothamTraffic.init(fc, gazetteer.places || []);
      return files[1];
    }).then(function (rows) {
      // [height, base, x, y, x, y, ..., kind?] in hundredths of a unit.
      var features = rows.map(function (row) {
        var kind = typeof row[row.length - 1] === 'string' ? row[row.length - 1] : null;
        var nums = kind ? row.slice(2, -1) : row.slice(2);
        var ring = [];
        for (var i = 0; i < nums.length; i += 2) ring.push(ll(nums[i] / 100, nums[i + 1] / 100));
        ring.push(ring[0]);
        var props = { h: row[0], b: row[1] };
        if (kind) props.k = kind;
        return { type: 'Feature', geometry: { type: 'Polygon', coordinates: [ring] }, properties: props };
      });
      map.getSource('buildings').setData({ type: 'FeatureCollection', features: features });
      return files[2];
    }).then(function (trees) {
      // Each tree stands up in 3D: a thin trunk, a wide crown, a smaller one on
      // top — round from above, rounded from the side.
      function disc(x, y, r, sides) {
        var ring = [];
        for (var i = 0; i <= sides; i++) {
          var a = i * 2 * Math.PI / sides;
          ring.push(ll(x + Math.cos(a) * r, y + Math.sin(a) * r));
        }
        return { type: 'Polygon', coordinates: [ring] };
      }
      var features = [];
      trees.forEach(function (t) {
        var x = t[0] / 100, y = t[1] / 100, r = (t[3] || 9) / 100, h = t[2];
        features.push({ type: 'Feature', geometry: disc(x, y, r * 0.16, 6), properties: { p: 0, h: h, b: 0, top: h * 0.4 } });
        features.push({ type: 'Feature', geometry: disc(x, y, r, 14), properties: { p: 1, h: h, b: h * 0.3, top: h * 0.78 } });
        features.push({ type: 'Feature', geometry: disc(x, y, r * 0.64, 12), properties: { p: 2, h: h, b: h * 0.78, top: h } });
      });
      map.getSource('trees').setData({ type: 'FeatureCollection', features: features });
    }).catch(function () { /* the city still draws without its buildings */ });
  }

  /* --- hovering: the console's own tooltip, never the map's ----------------- */

  var HOVERABLE = ['incident', 'airship', 'batwing', 'plane', 'chopper', 'heli', 'train-car', 'ship', 'boat', 'tug', 'sail', 'patrol', 'metro',
                   'landmark', 'spot', 'venue', 'station', 'railstation', 'parked', 'district-label', 'quarter-label', 'road-label',
                   'primary', 'highway', 'avenue', 'secondary', 'subway', 'ferry', 'shipping', 'pitch', 'golf', 'lot', 'park', 'water',
                   'marsh', 'trail-stop', 'district'];

  function describe(f) {
    var p = f.properties || {};
    if (p.l === 'place') return p.n + (p.a && p.a !== p.n ? ' · ' + p.a : '');
    if (p.l === 'stop') return p.n;
    if (p.kind) return p.kind + ' · ' + p.place + ' · ' + p.status;
    if (p.m && p.n) return p.n;                                    // something moving: a train, a ferry, a flight
    if (p.l === 'station') return p.n + ' · ' + p.line;
    if (p.l === 'railstation') return p.n + ' · ' + p.line + ' station';
    if (p.l === 'gate') return 'At the gate';
    if (p.l === 'subway' || p.l === 'ferry' || p.l === 'lane') return p.n + (p.l === 'lane' ? ' · shipping channel' : '');
    if (p.l === 'pitch') return ({ turf: 'Playing field', dirt: 'Ball field', court: 'Courts', track: 'Running track' }[p.k] || '');
    if (p.l === 'golf') return 'Golf course';
    if (p.l === 'lot') return 'Car park';
    if (p.l === 'marsh') return 'Slaughter Swamp';
    if (p.l === 'venue') return p.n + ' · ' + ({ club: 'club', bar: 'bar', diner: 'diner', church: 'church', fire: 'fire station',
                                                school: 'school', cafe: 'café', gym: 'gym', cinema: 'cinema', hotel: 'hotel',
                                                shop: 'shop' }[p.k] || '') + ' · ' + p.a;
    if ((p.l === 'road' || p.l === 'park' || p.l === 'water' || p.l === 'district') && p.n) return p.n;
    return '';
  }

  function wireHover() {
    // A query in 3D reads pixels back from the GPU, several times: one a frame
    // at most, none while the map is being dragged, and not over a marker
    // (which has its own tip).
    var pending = null, hoverable = null;
    map.on('mousemove', function (e) {
      var target = e.originalEvent && e.originalEvent.target;
      if (map.isMoving() || (e.originalEvent && e.originalEvent.buttons) ||
          (target && target.closest && target.closest('.maplibregl-marker'))) {
        hover.hidden = true;
        return;
      }
      if (pending) { pending.e = e; return; }
      pending = { e: e };
      requestAnimationFrame(function () { var job = pending; pending = null; hoverAt(job.e); });
    });
    function hoverAt(e) {
      hoverable = hoverable || HOVERABLE.filter(function (id) { return map.getLayer(id); });
      var hits = map.queryRenderedFeatures(e.point, { layers: hoverable });
      var top = null;
      for (var i = 0; i < hits.length; i++) { if (describe(hits[i])) { top = hits[i]; break; } }
      var district = hits.filter(function (h) { return h.properties.l === 'district'; })[0];
      if (hoverId !== null && (!district || district.id !== hoverId)) {
        map.setFeatureState({ source: 'city', id: hoverId }, { hover: false });
        hoverId = null;
      }
      if (district && hoverId === null) {
        hoverId = district.id;
        map.setFeatureState({ source: 'city', id: hoverId }, { hover: true });
      }
      if (!top) { hover.hidden = true; map.getCanvas().style.cursor = dropping ? 'var(--cursor-target)' : ''; return; }
      hover.textContent = describe(top);
      hover.hidden = false;
      hover.style.left = (e.point.x + 14) + 'px';
      hover.style.top = (e.point.y + 14) + 'px';
      map.getCanvas().style.cursor = dropping ? 'var(--cursor-target)' : (top.properties.l === 'place' || top.properties.m ? 'var(--cursor-pointer)' : '');
    }
    map.getCanvas().addEventListener('mouseleave', function () { hover.hidden = true; });
    ['landmark', 'spot', 'district-label', 'quarter-label'].forEach(function (layer) {
      map.on('click', layer, function (e) {
        if (dropping) return;
        var f = e.features[0];
        map.flyTo({ center: f.geometry.coordinates, zoom: Math.max(map.getZoom(), layer === 'district-label' ? 14 : 15.4),
                    duration: 900 });
        placeCard(f.properties.n);
      });
    });
    map.on('click', 'incident', function (e) { if (!dropping) incidentCard(e.features[0].properties); });
  }

  /* --- people ----------------------------------------------------------------- */

  // With terrain on, every marker reads the GPU's depth buffer to decide
  // whether a hill hides it — ten times a second each, while the camera
  // moves. People and pins are never hidden by Gotham's few hills.
  // (_updateOpacity is MapLibre's own, as of the vendored 4.7.1.)
  function flat(marker) {
    marker._updateOpacity = function () {};
    return marker;
  }

  function visible() {
    var contacts = opts.contacts();
    return Object.keys(contacts).map(function (id) { return contacts[id]; })
      .filter(function (c) { return c.presence && c.presence.spot; });
  }

  function glide(marker, to) {
    var from = marker.getLngLat(), start = performance.now();
    if (Math.abs(from.lng - to[0]) + Math.abs(from.lat - to[1]) < 1e-7) return;
    if (marker._gliding) cancelAnimationFrame(marker._gliding);
    function step(now) {
      var t = Math.min(1, (now - start) / 1400), e = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
      marker.setLngLat([from.lng + (to[0] - from.lng) * e, from.lat + (to[1] - from.lat) * e]);
      marker._gliding = t < 1 ? requestAnimationFrame(step) : null;
    }
    marker._gliding = requestAnimationFrame(step);
  }

  // A journey as something to move along: its road in map positions, how far along each point is.
  function journey(route) {
    var pts = route.pts.map(function (p) { return ll(p[0], p[1]); }), dist = [0];
    for (var i = 1; i < pts.length; i++) dist.push(dist[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]));
    return { pts: pts, dist: dist, len: dist[dist.length - 1], start: route.start, end: route.end, from: route.from };
  }

  // Where along it they are now — and the road behind and ahead of them.
  function onJourney(j, now) {
    var f = Math.max(0, Math.min(1, (now - j.start) / Math.max(1, j.end - j.start))), d = f * j.len, i = 1;
    while (i < j.dist.length - 1 && j.dist[i] < d) i++;
    var a = j.pts[i - 1], b = j.pts[i], seg = (j.dist[i] - j.dist[i - 1]) || 1, k = (d - j.dist[i - 1]) / seg;
    return { at: [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k], i: i };
  }

  function travelling(c) {
    var r = c.presence && c.presence.route;
    return r && Date.now() / 1000 < r.end ? r : null;
  }

  function minutesLeft(route) { return Math.max(1, Math.round((route.end - Date.now() / 1000) / 60)); }

  function personMarker(c, at, ghost) {
    var marker = people[c.id];
    if (!marker) {
      var el = document.createElement('div');
      el.className = 'gm-person' + (c.id === 'bruce' ? ' gm-person--bruce' : '');
      el.innerHTML = '<span class="gm-person__ring"></span><span class="gm-person__face"></span>' +
                     (c.id === 'bruce' ? '<span class="gm-person__bat">' + ARKHAM_BAT + '</span>' : '') +
                     '<span class="gm-person__name"></span>';
      el.style.setProperty('--accent', c.accent);
      el.querySelector('.gm-person__name').textContent = c.name;
      opts.portrait(el.querySelector('.gm-person__face'), c);
      el.addEventListener('click', function (e) { e.stopPropagation(); select(c.id); });
      marker = people[c.id] = flat(new maplibregl.Marker({ element: el, anchor: 'center' })).setLngLat(at).addTo(map);
    } else if (!marker._trip) {
      glide(marker, at);
    }
    marker.getElement().classList.toggle('is-ghost', !!ghost);
    return marker;
  }

  function ago(seconds) {
    var m = Math.round(seconds / 60);
    if (m < 2) return 'just now';
    if (m < 60) return m + ' min ago';
    var h = Math.round(m / 60);
    return h < 24 ? h + 'h ago' : Math.round(h / 24) + 'd ago';
  }

  // The family, and him — his own marker among theirs.
  function everyone() {
    var all = Object.assign({}, opts.contacts());
    var me = opts.bruce && opts.bruce();
    if (me && me.presence && me.presence.spot) all.bruce = me;
    return all;
  }

  function placePeople() {
    if (!ready) return;
    var byspot = {}, moving = [], ghosts = [];
    var contacts = everyone();
    Object.keys(contacts).forEach(function (id) {
      var c = contacts[id], p = c.presence;
      if (!p) return;
      if (p.spot) {
        if (travelling(c)) moving.push(c);
        else {
          var key = p.spot.x.toFixed(1) + ',' + p.spot.y.toFixed(1);
          (byspot[key] = byspot[key] || []).push(c);
        }
      } else if (p.last_seen) {
        ghosts.push(c);
      }
    });
    var seen = {};
    var showPeople = layerShown('people');
    function finish(c, marker, tip) {
      var node = marker.getElement();
      node.dataset.status = c.presence.status;
      node.classList.toggle('is-selected', selected === c.id);
      node.setAttribute('data-tip', tip);
      node.style.display = showPeople ? '' : 'none';
      seen[c.id] = true;
    }
    Object.keys(byspot).forEach(function (key) {
      var group = byspot[key];
      group.forEach(function (c, i) {
        var s = c.presence.spot;
        var angle = (i / group.length) * Math.PI * 2, r = group.length > 1 ? 0.55 : 0;
        var marker = personMarker(c, ll(s.x + Math.cos(angle) * r, s.y + Math.sin(angle) * r));
        if (marker._trip) { marker._trip = null; marker.getElement().classList.remove('is-moving'); }
        finish(c, marker, c.name + ' · ' + (c.presence.where || ''));
      });
    });
    // On their way somewhere: along the road they're taking, at its pace (see moveTravellers).
    moving.forEach(function (c) {
      var route = travelling(c);
      var marker = personMarker(c, onJourney(journey(route), Date.now() / 1000).at);
      if (!marker._trip || marker._trip.start !== route.start) marker._trip = journey(route);
      marker.getElement().classList.add('is-moving');
      finish(c, marker, c.name + ' · on the way to ' + (c.presence.where || c.presence.spot.name) +
             (route.by ? ' · ' + route.by : '') + ' · ' + minutesLeft(route) + ' min');
    });
    // Those who keep where they are to themselves: where he last knew them to be, faded.
    ghosts.forEach(function (c) {
      var ls = c.presence.last_seen;
      var marker = personMarker(c, ll(ls.x, ls.y), true);
      finish(c, marker, c.name + ' · last seen ' + ago(Date.now() / 1000 - ls.at) + ' · ' + ls.where + ' — ' + ls.how);
    });
    Object.keys(people).forEach(function (id) {
      if (!seen[id]) { people[id].remove(); delete people[id]; }
    });
    renderRoster();
    if (selected) renderCard(selected);
    if (following && people[following]) map.easeTo({ center: people[following].getLngLat(), duration: 1200 });
  }

  // Each tick: anyone on the road moves along it — and the camera with them, if he's following.
  function moveTravellers() {
    var now = Date.now() / 1000, arrived = false;
    Object.keys(people).forEach(function (id) {
      var marker = people[id], trip = marker._trip;
      if (!trip) return;
      if (now >= trip.end) { arrived = true; return; }
      var here = onJourney(trip, now).at;
      marker.setLngLat(here);
      if (following === id && !moving) map.jumpTo({ center: here });
    });
    if (arrived) placePeople();
  }

  function renderRoster() {
    var list = $('.gm-roster');
    var contacts = everyone();
    list.innerHTML = '';
    (contacts.bruce ? ['bruce'] : []).concat(opts.order()).forEach(function (id) {
      var c = contacts[id];
      if (!c) return;
      var p = c.presence || {};
      var li = document.createElement('li');
      li.className = 'gm-roster__item' + (p.spot ? '' : p.last_seen ? ' is-ghost' : ' is-hidden') + (selected === id ? ' is-on' : '');
      li.style.setProperty('--accent', c.accent);
      var face = document.createElement('span');
      face.className = 'gm-roster__face';
      opts.portrait(face, c);
      var text = document.createElement('span');
      text.className = 'gm-roster__text';
      text.innerHTML = '<b></b><small></small>';
      text.querySelector('b').textContent = c.name;
      var company = (p['with'] || []).map(function (cid) { return (contacts[cid] || {}).name; }).filter(Boolean);
      var route = travelling(c);
      text.querySelector('small').textContent = p.spot
        ? (route ? '→ ' + (p.where || p.spot.name) + (route.by ? ' · ' + route.by : '') + ' · ' + minutesLeft(route) + ' min'
                 : (p.where || p.spot.name))
          + (company.length ? ' · with ' + company.join(', ') : '')
        : p.last_seen ? 'Last seen ' + ago(Date.now() / 1000 - p.last_seen.at) + ' · ' + p.last_seen.where : 'Location hidden';
      li.appendChild(face);
      li.appendChild(text);
      li.dataset.status = p.status || '';
      if (p.spot || p.last_seen) {
        li.tabIndex = 0;
        li.setAttribute('role', 'button');
        li.addEventListener('click', function () { select(id, true); });
      }
      list.appendChild(li);
    });
  }

  function select(id, fly) {
    // Clicking whoever's already picked lets them go: the card, their trail, following.
    if (selected === id && !fly) { deselect(); return; }
    selected = id;
    var marker = people[id];
    if (marker && fly) map.flyTo({ center: marker.getLngLat(), zoom: Math.max(map.getZoom(), 14.6), duration: 1000 });
    Object.keys(people).forEach(function (other) {
      people[other].getElement().classList.toggle('is-selected', other === id);
    });
    renderCard(id);
    renderRoster();
    showTrail(id);
  }

  function deselect() {
    selected = null; following = null;
    $('.gm-card').hidden = true;
    map.getSource('trail').setData({ type: 'FeatureCollection', features: [] });
    Object.keys(people).forEach(function (other) { people[other].getElement().classList.remove('is-selected'); });
    renderRoster();
  }

  function renderCard(id) {
    var card = $('.gm-card');
    var c = opts.contacts()[id];
    // Gone from the map — asleep, out of reach, hidden — and their trail and card go too.
    if (!c || !c.presence || (!c.presence.spot && !c.presence.last_seen)) { if (selected === id) deselect(); else card.hidden = true; return; }
    var p = c.presence;
    card.hidden = false;
    card.style.setProperty('--accent', c.accent);
    opts.portrait(card.querySelector('.gm-card__face'), c);
    card.querySelector('.gm-card__name').textContent = c.full_name;
    card.querySelector('.gm-card__status').textContent = opts.label(c);
    var company = (p['with'] || []).map(function (cid) { return (opts.contacts()[cid] || {}).name; }).filter(Boolean);
    var route = travelling(c);
    card.querySelector('.gm-card__where').textContent = !p.spot
      ? 'Last seen ' + ago(Date.now() / 1000 - p.last_seen.at) + ' · ' + p.last_seen.where + ' — ' + p.last_seen.how
      : (route ? 'On the way to ' : '') + (p.where || p.spot.name) +
        (p.spot.area && p.spot.area !== p.where && p.spot.area !== p.spot.name ? ' · ' + p.spot.area : '') +
        (route ? (route.by ? ' · ' + route.by : '') + ' · ' + minutesLeft(route) + ' min' : '') +
        (company.length ? ' · with ' + company.join(', ') : '');
    card.querySelector('[data-act="follow"]').hidden = !p.spot;
    card.querySelector('[data-act="follow"]').classList.toggle('is-on', following === id);
  }

  function showTrail(id) {
    var c = opts.contacts()[id];
    var source = map.getSource('trail');
    source.setData({ type: 'FeatureCollection', features: [] });
    if (!c || !layerShown('trails')) return;
    fetch('/api/map/trail/' + encodeURIComponent(id) + '?hours=3').then(function (r) { return r.json(); })
      .then(function (body) {
        if (selected !== id) return;
        var pts = body.trail || [], trips = body.trips || [], now = Date.now() / 1000;
        if (pts.length < 2 && !trips.length) return;
        var features = [];
        if (trips.length) {
          trips.forEach(function (tr) {
            var j = journey(tr), coords = j.pts;
            if (now < tr.end) {
              var here = onJourney(j, now);
              features.push({ type: 'Feature', properties: { color: c.accent },
                              geometry: { type: 'LineString', coordinates: coords.slice(0, here.i).concat([here.at]) } });
              features.push({ type: 'Feature', properties: { color: c.accent, k: 'ahead' },
                              geometry: { type: 'LineString', coordinates: [here.at].concat(coords.slice(here.i)) } });
            } else {
              features.push({ type: 'Feature', properties: { color: c.accent }, geometry: { type: 'LineString', coordinates: coords } });
            }
          });
        } else {
          features.push({ type: 'Feature', properties: { color: c.accent },
                          geometry: { type: 'LineString', coordinates: pts.map(function (p) { return ll(p.x, p.y); }) } });
        }
        pts.slice(0, -1).forEach(function (p) {
          var when = new Date(p.at * 1000);
          features.push({ type: 'Feature', geometry: { type: 'Point', coordinates: ll(p.x, p.y) },
                          properties: { color: c.accent, l: 'stop',
                                        n: String(when.getHours()).padStart(2, '0') + ':' + String(when.getMinutes()).padStart(2, '0') + ' · ' + p.where } });
        });
        source.setData({ type: 'FeatureCollection', features: features });
      }).catch(function () {});
  }

  /* --- a place's own card: what it is, and what he thinks of it ------------- */

  function iconUrl(kind) {
    var img = icon(kind), canvas = document.createElement('canvas');
    canvas.width = img.width; canvas.height = img.height;
    canvas.getContext('2d').putImageData(new ImageData(new Uint8ClampedArray(img.data), img.width, img.height), 0, 0);
    return canvas.toDataURL();
  }

  function showInfo(title, sub, body, note, imageUrl, tint) {
    var card = $('.gm-info');
    openReport = null;
    card.querySelector('.gm-info__log').hidden = true;
    card.querySelector('.gm-info__assign').hidden = true;
    var go = card.querySelector('.gm-info__go');
    if (go) go.hidden = true;
    card.querySelector('.gm-info__note').classList.remove('is-plain');
    card.style.setProperty('--accent', tint || C.edge);
    card.querySelector('.gm-info__icon').style.backgroundImage = imageUrl ? 'url(' + imageUrl + ')' : '';
    card.querySelector('.gm-info__name').textContent = title;
    card.querySelector('.gm-info__sub').textContent = sub || '';
    card.querySelector('.gm-info__bio').textContent = body || '';
    card.querySelector('.gm-info__bio').hidden = !body;
    card.querySelector('.gm-info__note').textContent = note || '';
    card.querySelector('.gm-info__note').hidden = !note;
    card.hidden = false;
  }

  // "Go here": he sets off for a place, the way he would at this hour.
  function offerGo(name) {
    var go = $('.gm-info__go');
    if (!go || !opts.go) return;
    go.hidden = false;
    go.querySelector('.gm-info__go-bat').innerHTML = ARKHAM_BAT;
    go.querySelector('.gm-info__go-text').textContent = 'Go here';
    go.onclick = function () {
      go.disabled = true;
      opts.go({ place: name }).then(function (where) {
        go.querySelector('.gm-info__go-text').textContent = where && where.route
          ? 'On your way — ' + Math.max(1, Math.round((where.route.end - Date.now() / 1000) / 60)) + ' min' : 'You\u2019re here';
      }).catch(function () {}).then(function () { go.disabled = false; });
    };
  }

  function placeCard(name) {
    var p = (gazetteer.places || []).filter(function (q) { return q.name === name; })[0];
    if (!p) return;
    if (p.kind === 'district') {
      // A district: what it's like, and what's in it.
      var here = (gazetteer.places || []).filter(function (q) { return q.area === p.area && q.kind !== 'district'; })
        .map(function (q) { return q.name; });
      showInfo(p.name, 'District', (gazetteer.areas || {})[p.name] || '', here.length ? 'Here: ' + here.join(' · ') : '',
               iconUrl('civic'), C.lit);
      $('.gm-info__note').classList.add('is-plain');
      return;
    }
    showInfo(p.name, p.area, p.bio, p.note, iconUrl(p.icon || ''), ICON_TINT[p.icon]);
    offerGo(p.name);
  }

  // The incident log behind a report — who called it in, what units found, where it stands.
  // Written the first time it's opened, so the first look waits a moment for it.
  var logCache = {};
  function reportLog(report) {
    var box = $('.gm-info__log'), list = box.querySelector('.gm-log');
    box.hidden = false;
    var key = report.id + ':' + report.status;        // a log grows as the case moves on
    if (logCache[key]) { drawLog(list, logCache[key]); return; }
    list.innerHTML = '<li class="gm-log__wait">Pulling the log\u2026</li>';
    fetch('/api/scanner/' + encodeURIComponent(report.id) + '/log').then(function (r) { return r.json(); })
      .then(function (body) {
        if ((body.log || []).length) logCache[key] = body;
        if (openReport === report.id) drawLog(list, body);
      }).catch(function () { if (openReport === report.id) list.innerHTML = '<li class="gm-log__wait">The log won\u2019t load.</li>'; });
  }

  function drawLog(list, body) {
    list.innerHTML = '';
    (body.log || []).forEach(function (e) {
      var li = document.createElement('li');
      li.className = 'gm-log__entry';
      li.innerHTML = '<time></time><b></b><span></span>';
      li.querySelector('time').textContent = e.time || '';
      li.querySelector('b').textContent = e.who || '';
      li.querySelector('span').textContent = e.text || '';
      list.appendChild(li);
    });
    if (!(body.log || []).length) list.innerHTML = '<li class="gm-log__wait">No log yet.</li>';
  }

  function incidentCard(report) {
    var contacts = opts.contacts();
    var who = report.assignee && contacts[report.assignee];
    var near = visible().filter(function (c) {
      var s = c.presence.spot;
      return Math.hypot(s.x - report.x, s.y - report.y) < 2.2;
    }).map(function (c) { return c.name; });
    var state = who ? (report.case === 'closed' ? who.name + ' closed it' + (report.outcome ? ': ' + report.outcome : '.')
                                              : who.name + '\u2019s on it — ' + report.case + '.')
                    : (near.length ? 'Close by: ' + near.join(', ') + '.' : 'Nobody from the family nearby.');
    var img = warning(report.severity), canvas = document.createElement('canvas');
    canvas.width = img.width; canvas.height = img.height;
    canvas.getContext('2d').putImageData(new ImageData(new Uint8ClampedArray(img.data), img.width, img.height), 0, 0);
    var toll = tollText(report.toll);
    showInfo(report.kind, report.place + ' · ' + clock(report.at) + ' · ' + report.status + (toll ? ' · ' + toll : ''),
             (report.suspect ? 'Suspect: ' + report.suspect + '. ' : report.gang ? 'Looks like ' + report.gang + '. ' : '') +
             (report.dispatch ? 'Dispatch: \u201c' + report.dispatch + '\u201d' : 'Dispatch is still coming through.'),
             '', canvas.toDataURL(), SEVERITY[report.severity]);
    openReport = report.id;          // after showInfo, which clears it for any other card
    reportLog(report);
    $('.gm-info__note').hidden = false;
    $('.gm-info__note').textContent = state;
    $('.gm-info__note').classList.add('is-plain');
    // Put someone on it: the ones who work scenes, nearest first.
    var assign = $('.gm-info__assign'), people = $('.gm-info__people');
    people.innerHTML = '';
    var team = report.team || (report.assignee ? [report.assignee] : []);
    if (report.case !== 'closed' && opts.go) {
      // Him: he goes himself — the Batmobile by night — and it's his case too.
      var me = document.createElement('button');
      me.type = 'button';
      var mine = team.indexOf('bruce') !== -1;
      me.className = 'gm-assign gm-assign--bruce' + (mine ? ' is-on' : '');
      me.disabled = mine;
      me.style.setProperty('--accent', '#e8c86a');
      me.setAttribute('data-tip', mine ? 'You\u2019re on it' : 'Go yourself');
      var myFace = document.createElement('span');
      if (opts.bruce && opts.bruce()) opts.portrait(myFace, opts.bruce());
      me.appendChild(myFace);
      me.appendChild(document.createTextNode('You'));
      me.addEventListener('click', function () {
        opts.go({ report: report.id }).then(function (where) {
          said.textContent = where && where.route ? 'On your way — ' + (where.route.by || '') + ', ' +
            Math.max(1, Math.round((where.route.end - Date.now() / 1000) / 60)) + ' min.' : 'You\u2019re on it.';
          said.hidden = false;
          loadIncidents();
        }).catch(function () {});
      });
      people.appendChild(me);
    }
    if (report.case !== 'closed') {
      FIELD.map(function (id) { return contacts[id]; }).filter(Boolean).sort(function (a, b) {
        var sa = (a.presence || {}).spot, sb = (b.presence || {}).spot;
        var da = sa ? Math.hypot(sa.x - report.x, sa.y - report.y) : 99, db = sb ? Math.hypot(sb.x - report.x, sb.y - report.y) : 99;
        return da - db;
      }).forEach(function (c) {
        var b = document.createElement('button');
        b.type = 'button';
        var onIt = (report.team || (report.assignee ? [report.assignee] : [])).indexOf(c.id) !== -1;
        b.className = 'gm-assign' + (onIt ? ' is-on' : '');
        b.disabled = onIt;
        b.style.setProperty('--accent', c.accent);
        var p = c.presence || {};
        var state = p.status === 'offline' ? 'asleep or out of reach' : p.doing || p.status || '';
        b.setAttribute('data-tip', onIt ? c.name + '\u2019s on it' : (report.assignee ? 'Send ' + c.name + ' too' : 'Put ' + c.name + ' on it')
                                   + (state && !onIt ? ' — ' + state : ''));
        b.dataset.status = p.status || '';
        var face = document.createElement('span');
        opts.portrait(face, c);
        b.appendChild(face);
        b.appendChild(document.createTextNode(c.name));
        b.addEventListener('click', function () {
          fetch('/api/cases/assign', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                       body: JSON.stringify({ report: report.id, contact: c.id }) })
            .then(function (r) { return r.json(); })
            .then(function (body) {
              // What came of it: asleep, they'll see a text; unwilling, they've said no.
              said.textContent = body.texted ? 'Texted ' + c.name + ' — they\u2019ll see it when they wake.'
                : body.declined ? c.name + ' isn\u2019t taking it.' : body.case ? c.name + ' is on the way.' : '';
              said.hidden = !said.textContent;
              loadIncidents();
            }).catch(function () {});
        });
        people.appendChild(b);
      });
    }
    var said = $('.gm-info__said');
    if (!said) {
      said = document.createElement('p');
      said.className = 'gm-info__said';
      assign.appendChild(said);
    }
    said.hidden = true;
    assign.hidden = report.case === 'closed';
  }

  /* --- the scanner ------------------------------------------------------------ */

  var reports = [], incidentStamp = '';
  function loadIncidents() {
    if (!ready || root.hidden) return;
    fetch('/api/map/incidents').then(function (r) { return r.json(); }).then(function (body) {
      reports = body.incidents || [];
      var contacts = opts.contacts();
      var stamp = JSON.stringify(reports.map(function (r) { return [r.id, r.status, r.assignee, r.case]; }));
      if (stamp !== incidentStamp) {
        incidentStamp = stamp;
        map.getSource('incidents').setData({ type: 'FeatureCollection', features: reports.map(function (r) {
          var props = Object.assign({}, r);
          if (r.assignee && contacts[r.assignee]) props.acc = contacts[r.assignee].accent;
          return { type: 'Feature', geometry: { type: 'Point', coordinates: ll(r.x, r.y) }, properties: props };
        }) });
      }
      renderLists();
      if (openReport) {
        var again = reports.filter(function (r) { return r.id === openReport; })[0];
        if (again) incidentCard(again);
      }
    }).catch(function () {});
  }

  /* --- the side panel: who's where, what's being worked, what the scanner says --- */

  var FIELD = ['nightwing', 'robin', 'batgirl', 'orphan', 'redhood', 'batwing'];
  var openReport = null;

  function showTab(name) {
    root.querySelectorAll('.gm-tab').forEach(function (t) {
      t.classList.toggle('is-on', t.dataset.tab === name);
      t.setAttribute('aria-selected', t.dataset.tab === name ? 'true' : 'false');
    });
    root.querySelectorAll('[data-pane]').forEach(function (p) { p.hidden = p.dataset.pane !== name; });
  }

  function clock(at) {
    var d = new Date(at * 1000);
    return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  }

  // "2 dead, 3 hurt" — or nothing when nobody is.
  function tollText(t) {
    if (!t) return '';
    return [t.dead ? t.dead + ' dead' : '', t.hurt ? t.hurt + ' hurt' : ''].filter(Boolean).join(', ');
  }

  function reportItem(r, withWho) {
    var li = document.createElement('li');
    li.className = 'gm-list__item';
    li.style.setProperty('--sev', SEVERITY[r.severity]);
    var book = everyone();
    if (!book.bruce && opts.bruce && opts.bruce()) book.bruce = opts.bruce();
    var crew = (r.team || (r.assignee ? [r.assignee] : [])).map(function (id) { return book[id]; }).filter(Boolean);
    var who = crew[0];
    // The time in a column of its own at the right, the face (if any) under it:
    // inside the text it sat wherever the text ended, a face's width apart.
    li.innerHTML = '<i class="gm-list__sev"></i><span class="gm-list__text"><span class="gm-list__head"><b></b></span>' +
                   '<small></small><span class="gm-list__status"></span></span><span class="gm-list__aside"><time></time></span>';
    li.querySelector('b').textContent = r.kind;
    li.querySelector('time').textContent = clock(r.at);
    var toll = tollText(r.toll);
    li.querySelector('small').textContent = r.place + (r.area && r.area !== r.place ? ', ' + r.area : '') + (toll ? ' · ' + toll : '');
    var status = li.querySelector('.gm-list__status');
    // Closed, how it went: caught, saved, got away, lost — in its colour.
    var ended = r.case === 'closed' ? (r.result || 'closed') : '';
    var names = crew.length > 2 ? crew[0].name + ' +' + (crew.length - 1) : crew.map(function (c) { return c.name; }).join(' + ');
    status.textContent = who ? names + ' · ' + (ended || r.case) : r.status;
    status.dataset.s = !who ? r.status.replace(/ /g, '-') : !ended ? 'case' : r.ok ? 'won' : /^(got away|cold|too late)$/.test(r.result) ? 'lost-trail' : 'failed';
    if (withWho && crew.length) {
      // Everyone on it, overlapping like a hand of cards, the lead on top.
      var faces = document.createElement('span');
      faces.className = 'gm-list__faces';
      crew.slice(0, 4).forEach(function (c, i) {
        var face = document.createElement('span');
        face.className = 'gm-list__face';
        face.style.setProperty('--accent', c.accent);
        face.style.zIndex = String(10 - i);
        face.dataset.tip = c.name;
        opts.portrait(face, c);
        faces.appendChild(face);
      });
      li.querySelector('.gm-list__aside').appendChild(faces);
    }
    li.tabIndex = 0;
    li.setAttribute('role', 'button');
    li.addEventListener('click', function () {
      map.flyTo({ center: ll(r.x, r.y), zoom: Math.max(map.getZoom(), 15), duration: 900 });
      incidentCard(r);
    });
    return li;
  }

  function renderLists() {
    var casesPane = $('[data-pane="cases"]'), scannerPane = $('[data-pane="scanner"]');
    casesPane.innerHTML = '';
    scannerPane.innerHTML = '';
    var worked = reports.filter(function (r) { return r.assignee; });
    if (!worked.length) casesPane.innerHTML = '<li class="gm-list__empty">Nobody\u2019s on a case.</li>';
    worked.forEach(function (r) { casesPane.appendChild(reportItem(r, true)); });
    reports.slice().sort(function (a, b) { return b.severity - a.severity || b.at - a.at; })
      .forEach(function (r) { scannerPane.appendChild(reportItem(r, true)); });
    if (!reports.length) scannerPane.innerHTML = '<li class="gm-list__empty">The scanner\u2019s quiet.</li>';
    var count = $('.gm-tab__count');
    var open = worked.filter(function (r) { return r.case !== 'closed'; }).length;
    count.textContent = open;
    count.hidden = !open;
  }

  /* --- the living map: water that twinkles, reports that pulse ------------------ */

  var frame = null, last = 0, lastStep = -1;
  var SPARKLE = [0, 1, 2, 3, 4, 5, 6, 7];
  // A dash travelling along the line: the same pattern, shifted a little each step.
  var TRAFFIC = (function () {
    var steps = [], gap = 6, n = 14;
    for (var i = 0; i < n; i++) {
      var t = (i / n) * gap;
      steps.push(t < 0.05 ? [0.1, gap] : [0, t, 0.1, gap - t]);
    }
    return steps;
  })();
  // Runs only while the map is open and the window in front: closed or
  // hidden, it stops altogether rather than ticking over doing nothing.
  // Each tick restyles the map, and in 3D a restyle re-drapes everything over
  // the terrain — so while the camera moves, the effects hold still and the
  // pan stays smooth, and nothing is restyled that isn't being drawn.
  var moving = false;
  function animate(now) {
    if (root.hidden || document.hidden || !ready) { frame = null; return; }
    frame = requestAnimationFrame(animate);
    if (moving || now - last < 90) return;
    last = now;
    var t = now / 700, zoom = map.getZoom();
    // Each group of lights catches it briefly, then not, in its own rhythm.
    SPARKLE.forEach(function (k) {
      var phase = (k + 0.5) * 2 * Math.PI / SPARKLE.length;
      map.setPaintProperty('sparkle-' + k, 'circle-opacity', 0.75 * Math.pow(Math.max(0, Math.sin(phase + t)), 14));
    });
    if (zoom >= 11.4) {
      var step = Math.floor(now / 110) % TRAFFIC.length;
      if (step !== lastStep) {
        lastStep = step;
        map.setPaintProperty('traffic', 'line-dasharray', TRAFFIC[step]);
        if (zoom >= 13.2) {
          map.setPaintProperty('traffic-avenue', 'line-dasharray', TRAFFIC[(step * 3) % TRAFFIC.length]);
          map.setPaintProperty('traffic-tail', 'line-dasharray', TRAFFIC[(TRAFFIC.length - step) % TRAFFIC.length]);
        }
      }
    }
    if (layerShown('crime') && reports.length) {
      var pulse = (now % 1800) / 1800, drift = (now % 5200) / 5200;
      map.setPaintProperty('incident-pulse', 'circle-radius', 8 + pulse * 22);
      map.setPaintProperty('incident-pulse', 'circle-opacity', 0.35 * (1 - pulse));
      map.setPaintProperty('smoke', 'circle-radius', 18 + drift * 34);
      map.setPaintProperty('smoke', 'circle-opacity', 0.38 * (1 - drift * 0.7));
    }
    if (layerShown('life') && zoom >= 12.6) {
      map.setPaintProperty('venue-glow', 'circle-opacity', (0.1 + 0.18 * night) + (0.04 + 0.06 * night) * Math.sin(now / 900));
    }
    if (layerShown('transit') || layerShown('crime')) movers();
    moveTravellers();
  }

  /* --- things that move: everything in gothamtraffic.js, drawn here ------------------- */

  function movers() {
    var features = window.GothamTraffic ? GothamTraffic.frame(Date.now(), reports) : [];
    map.getSource('movers').setData({ type: 'FeatureCollection', features: features });
  }

  function startAnimating() {
    if (!frame && ready && root && !root.hidden && !document.hidden) frame = requestAnimationFrame(animate);
  }
  document.addEventListener('visibilitychange', startAnimating);

  /* --- day and night: the same dark console, the city lit up after dusk --- */

  var night = 1;
  // The sun over Gotham — an east-coast city at New York's latitude, on the
  // console's own clock — so the evening comes as it does outside: later in
  // June, before six in December, gold first, then blue, then night.
  var LATITUDE = 40.7;
  function sunHeight(date) {
    var year = date.getFullYear(), start = new Date(year, 0, 1);
    var day = Math.floor((date - start) / 86400000) + 1;
    var standard = Math.max(start.getTimezoneOffset(), new Date(year, 6, 1).getTimezoneOffset());
    var summer = date.getTimezoneOffset() < standard ? 1 : 0;      // clocks forward: the sun's noon is at one
    var solar = date.getHours() + date.getMinutes() / 60 - summer;
    var tilt = 23.44 * Math.sin(2 * Math.PI * (284 + day) / 365) * Math.PI / 180;
    var phi = LATITUDE * Math.PI / 180, hourAngle = (solar - 12) * 15 * Math.PI / 180;
    return Math.asin(Math.sin(phi) * Math.sin(tilt) + Math.cos(phi) * Math.cos(tilt) * Math.cos(hourAngle)) * 180 / Math.PI;
  }
  function smooth(a, b, x) { var t = Math.max(0, Math.min(1, (x - a) / (b - a))); return t * t * (3 - 2 * t); }
  // 0 by day, 1 at night: dusk from the sun a few degrees up to well below the horizon.
  function nightness(date) { return 1 - smooth(-10, 4, sunHeight(date)); }
  // The low sun's gold, strongest just before it sets and just after it rises.
  function goldenness(date) { var e = sunHeight(date); return smooth(-3, 1.5, e) * (1 - smooth(4, 11, e)); }
  function mix(a, b, f) {
    var pa = [1, 3, 5].map(function (i) { return parseInt(a.substr(i, 2), 16); });
    var pb = [1, 3, 5].map(function (i) { return parseInt(b.substr(i, 2), 16); });
    return '#' + pa.map(function (v, i) { return Math.round(v + (pb[i] - v) * f).toString(16).padStart(2, '0'); }).join('');
  }
  function lighting() {
    if (!ready) return;
    var now = new Date();
    night = nightness(now);
    var n = night, gold = goldenness(now);
    // The ground a shade lighter by day and dimming through the dusk; the glow of
    // roads, coast and traffic coming up as it goes; a warm cast at the golden hour.
    var water = mix(mix('#06101a', C.void, n), '#1a1410', gold * 0.35);
    map.setPaintProperty('void', 'background-color', water);
    map.setPaintProperty('land', 'fill-color', ['match', ['get', 'k'],
                                                'mainland', mix(mix('#112131', C.mainland, n), '#2a2016', gold * 0.3),
                                                mix(mix('#13253a', C.land, n), '#2e2318', gold * 0.3)]);
    map.setPaintProperty('coast-glow', 'line-opacity', 0.07 + 0.08 * n);
    if (map.getLayer('lantern')) map.setPaintProperty('lantern', 'line-opacity', 0.35 + 0.6 * n);
    map.setPaintProperty('primary-glow', 'line-opacity', 0.18 + 0.2 * n);
    map.setPaintProperty('highway-glow', 'line-opacity', 0.16 + 0.18 * n);
    map.setPaintProperty('traffic', 'line-opacity', 0.32 + 0.28 * n);
    map.setPaintProperty('traffic-avenue', 'line-opacity', 0.18 + 0.28 * n);
    map.setPaintProperty('traffic-tail', 'line-opacity', 0.15 + 0.28 * n);
    map.setLight({ anchor: 'viewport', intensity: 0.32 + 0.18 * (1 - n),
                   color: mix(mix('#cfe6ff', '#9fc8ff', n), '#ffc58f', gold * 0.75) });
    root.classList.toggle('is-night', n > 0.5);
  }

  // The bat on the clouds, as Arkham drew it: swept horns, pointed ears, the
  // trailing edge in two scallops to a point.
  var ARKHAM_BAT = '<svg viewBox="140 570 1520 660" aria-hidden="true"><path d="M147 935C160 860 260 720 380 650' +
    'C440 615 490 595 531 580C495 612 480 650 492 690C510 740 570 775 650 795C710 808 770 814 810 815L837 662' +
    'L871 738L931 738L963 662L990 815C1030 814 1090 808 1150 795C1230 775 1290 740 1308 690C1320 650 1305 612 1269 580' +
    'C1310 595 1360 615 1420 650C1540 720 1640 860 1653 935C1560 905 1400 905 1330 955C1300 980 1290 1020 1290 1055' +
    'C1230 1015 1150 1010 1080 1035C1000 1065 940 1130 900 1220C860 1130 800 1065 720 1035C650 1010 570 1015 510 1055' +
    'C510 1020 500 980 470 955C400 905 240 905 147 935Z"/></svg>';

  var signal = null;
  function batSignal() {
    var night = nightness(new Date()) > 0.6;          // lit once the dusk is well on, as the sky goes
    var gcpd = (gazetteer.places || []).filter(function (p) { return p.name === 'GCPD Central'; })[0];
    if (!gcpd) return;
    if (night && !signal) {
      var el = document.createElement('div');
      el.className = 'gm-signal';
      el.innerHTML = '<span class="gm-signal__beam"></span><span class="gm-signal__bat">' + ARKHAM_BAT + '</span>';
      el.setAttribute('data-tip', 'The Signal — GCPD Central');
      signal = new maplibregl.Marker({ element: el, anchor: 'bottom' }).setLngLat(ll(gcpd.x, gcpd.y)).addTo(map);
    } else if (!night && signal) {
      signal.remove();
      signal = null;
    }
  }

  function fogFor() {
    // Far out, the edges fade to night: the map is about Gotham.
    var z0 = map.getZoom();
    root.style.setProperty('--gm-fog', Math.max(0, Math.min(1, (11.5 - z0) / 0.8)).toFixed(2));
  }

  /* --- pins of his own ------------------------------------------------------ */

  function loadPins() {
    fetch('/api/map/pins').then(function (r) { return r.json(); }).then(function (body) {
      Object.keys(pins).forEach(function (id) { pins[id].remove(); });
      pins = {};
      (body.pins || []).forEach(addPin);
    }).catch(function () {});
  }

  function addPin(pin) {
    var el = document.createElement('div');
    el.className = 'gm-pin';
    el.innerHTML = '<i></i><b></b>';
    el.querySelector('b').textContent = pin.label;
    el.setAttribute('data-tip', pin.label + ' — drag to move, click to edit');
    var marker = flat(new maplibregl.Marker({ element: el, anchor: 'bottom-left', draggable: true }))
      .setLngLat(ll(pin.x, pin.y)).addTo(map);
    marker._pin = pin;
    el.addEventListener('click', function (e) { e.stopPropagation(); editPin(pin, marker); });
    marker.on('dragend', function () {
      var at = unit(marker.getLngLat());
      pin.x = +at.x.toFixed(2); pin.y = +at.y.toFixed(2);
      savePin(pin);
    });
    if (!layerShown('pins')) el.style.display = 'none';
    pins[pin.id] = marker;
  }

  function savePin(pin) {
    return fetch('/api/map/pins', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                     body: JSON.stringify(pin) })
      .then(function (r) { return r.json(); }).then(function (body) { return body.pin; });
  }

  function editPin(pin, marker) {
    var form = document.createElement('form');
    form.className = 'gm-pinform';
    form.innerHTML = '<input class="gm-pinform__input" maxlength="40" aria-label="Pin name">' +
      '<div class="gm-pinform__actions"><button type="button" class="gm-btn" data-act="delete">Remove</button>' +
      '<button type="submit" class="gm-btn gm-btn--go">Save</button></div>';
    var input = form.querySelector('input');
    input.value = pin.label;
    var popup = new maplibregl.Popup({ className: 'gm-popup', closeButton: false, offset: 18, maxWidth: '260px' })
      .setLngLat(marker.getLngLat()).setDOMContent(form).addTo(map);
    setTimeout(function () { input.focus(); input.select(); }, 30);
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      pin.label = input.value.trim() || 'Pin';
      savePin(pin).then(function () {
        popup.remove();
        marker.getElement().querySelector('b').textContent = pin.label;
      });
    });
    form.querySelector('[data-act="delete"]').addEventListener('click', function () {
      fetch('/api/map/pins/' + encodeURIComponent(pin.id), { method: 'DELETE' }).then(function () {
        popup.remove();
        marker.remove();
        delete pins[pin.id];
      });
    });
  }

  function dropPin(lngLat) {
    var at = unit(lngLat);
    savePin({ label: 'Pin', x: +at.x.toFixed(2), y: +at.y.toFixed(2) }).then(function (pin) {
      if (!pin) return;
      addPin(pin);
      if (!layerShown('pins')) setLayer('pins', true);
      editPin(pin, pins[pin.id]);
    });
    setDropping(false);
  }

  function setDropping(on) {
    dropping = on;
    root.classList.toggle('is-dropping', on);
    $('[data-act="drop"]').classList.toggle('is-on', on);
  }

  /* --- search ----------------------------------------------------------------- */

  function wireSearch() {
    var input = $('.gm-search__input'), list = $('.gm-search__list');
    var options = [], chosen = 0;
    function close() { list.hidden = true; options = []; }
    function go(i) {
      var o = options[i];
      if (!o) return;
      input.value = '';
      close();
      input.blur();
      if (o.person) return select(o.person, true);
      if (o.report) incidentCard(o.report);
      map.flyTo({ center: ll(o.x, o.y), zoom: Math.max(map.getZoom(), o.district ? 14.2 : 15.6), duration: 1100 });
      flash(ll(o.x, o.y));
    }
    function draw() {
      list.innerHTML = '';
      options.forEach(function (o, i) {
        var li = document.createElement('li');
        li.className = 'gm-search__item' + (i === chosen ? ' is-on' : '');
        li.innerHTML = '<b></b><small></small>';
        li.querySelector('b').textContent = o.name;
        li.querySelector('small').textContent = o.kind;
        li.addEventListener('mousedown', function (e) { e.preventDefault(); go(i); });
        list.appendChild(li);
      });
      list.hidden = !options.length;
    }
    input.addEventListener('input', function () {
      var q = input.value.trim().toLowerCase();
      if (!q) return close();
      var contacts = opts.contacts(), found = [];
      Object.keys(contacts).forEach(function (id) {
        var c = contacts[id];
        if (c.presence && c.presence.spot && (c.name + ' ' + c.full_name).toLowerCase().indexOf(q) !== -1) {
          found.push({ name: c.full_name, kind: c.presence.where || 'Person', person: id });
        }
      });
      (gazetteer.places || []).forEach(function (p) {
        if ((p.name + ' ' + (p.match || []).join(' ')).toLowerCase().indexOf(q) !== -1) {
          found.push({ name: p.name, kind: p.kind === 'district' ? 'District' : p.area, x: p.x, y: p.y,
                       district: p.kind === 'district' });
        }
      });
      Object.keys(pins).forEach(function (id) {
        var pin = pins[id]._pin;
        if (pin.label.toLowerCase().indexOf(q) !== -1) found.push({ name: pin.label, kind: 'Your pin', x: pin.x, y: pin.y });
      });
      reports.forEach(function (r) {
        if ((r.kind + ' ' + r.place + ' ' + (r.dispatch || '')).toLowerCase().indexOf(q) !== -1) {
          found.push({ name: r.kind, kind: r.place + ' · ' + r.status, x: r.x, y: r.y, report: r });
        }
      });
      options = found.slice(0, 8);
      chosen = 0;
      draw();
    });
    input.addEventListener('keydown', function (e) {
      if (list.hidden) return;
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        chosen = (chosen + (e.key === 'ArrowDown' ? 1 : options.length - 1)) % options.length;
        draw();
      } else if (e.key === 'Enter') { e.preventDefault(); go(chosen); }
      else if (e.key === 'Escape') { e.stopPropagation(); close(); }
    });
    input.addEventListener('blur', function () { setTimeout(close, 120); });
  }

  function flash(at) {
    var el = document.createElement('div');
    el.className = 'gm-flash';
    var marker = new maplibregl.Marker({ element: el }).setLngLat(at).addTo(map);
    setTimeout(function () { marker.remove(); }, 2000);
  }

  /* --- layers, 3D, the frame ---------------------------------------------------- */

  var LAYER_GROUPS = {
    places: ['landmark', 'quarter-label', 'spot'],
    streets: ['street', 'avenue', 'road-label'],
    buildings: ['buildings', 'footprint', 'trees', 'trunks'],
    transit: ['subway', 'subway-casing', 'subway-label', 'station', 'station-label', 'ferry', 'shipping', 'train-car', 'metro', 'boat', 'ship', 'tug',
              'sail', 'patrol', 'rail', 'rail-bed', 'railbridge', 'railbridge-truss', 'railstation', 'railstation-label',
              'plane', 'plane-shadow', 'parked', 'chopper', 'beam', 'airship', 'airship-shadow', 'batwing', 'batwing-shadow'],
    crime: ['incident', 'incident-pulse', 'smoke', 'case-ring', 'heli', 'heli-light', 'heli-spot'],
    life: ['venue', 'venue-glow', 'lantern'],
    safety: ['safety'],
    trails: ['trail', 'trail-ahead', 'trail-stop']
  };

  var shownLayers = null;
  function layerShown(name) {
    shownLayers = shownLayers || recall(SHOWN_KEY, {});
    return shownLayers[name] !== false;
  }

  function setLayer(name, on) {
    if (on === undefined) on = !layerShown(name);
    shownLayers = shownLayers || recall(SHOWN_KEY, {});
    shownLayers[name] = on;
    remember(SHOWN_KEY, shownLayers);
    (LAYER_GROUPS[name] || []).forEach(function (id) {
      if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', on ? 'visible' : 'none');
    });
    if (name === 'people') Object.keys(people).forEach(function (id) { people[id].getElement().style.display = on ? '' : 'none'; });
    if (name === 'pins') Object.keys(pins).forEach(function (id) { pins[id].getElement().style.display = on ? '' : 'none'; });
    var chip = $('[data-layer="' + name + '"]');
    if (chip) { chip.classList.toggle('is-on', on); chip.setAttribute('aria-checked', on ? 'true' : 'false'); }
  }

  function relief(on) {
    // The ground rises only when the map tilts; flat, the hillshade is enough.
    try { map.setTerrain(on ? { source: 'terrain', exaggeration: 1.5 } : null); } catch (e) { /* older GPUs */ }
  }

  function setThreeD(on) {
    threeD = on;
    $('[data-act="3d"]').classList.toggle('is-on', on);
    relief(on);
    map.easeTo(on ? { pitch: 56, bearing: -16, zoom: Math.max(map.getZoom(), 13.2), duration: 1200 }
                  : { pitch: 0, bearing: 0, duration: 900 });
  }

  function fit() {
    // The whole island, tilted: the city as a model on the table.
    var camera = map.cameraForBounds([ll(11, 133), ll(90, 14)], { padding: 30 });
    relief(true);
    threeD = true;
    $('[data-act="3d"]').classList.add('is-on');
    map.easeTo({ center: camera.center, zoom: camera.zoom + 0.15, pitch: 45, bearing: -12,
                 duration: fitted ? 1100 : 0 });
  }

  function wireFrame() {
    root.querySelectorAll('[data-layer]').forEach(function (chip) {
      chip.addEventListener('click', function () { setLayer(chip.dataset.layer); });
    });
    // The layers live in a menu, not across the header.
    var layersBtn = $('.gm-layers__btn'), layersMenu = $('.gm-layers__menu');
    layersBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      layersMenu.hidden = !layersMenu.hidden;
      layersBtn.setAttribute('aria-expanded', String(!layersMenu.hidden));
    });
    document.addEventListener('pointerdown', function (e) {
      if (!layersMenu.hidden && !layersMenu.contains(e.target) && e.target !== layersBtn) layersMenu.hidden = true;
    });
    $('[data-act="zoom-in"]').addEventListener('click', function () { map.zoomIn(); });
    $('[data-act="zoom-out"]').addEventListener('click', function () { map.zoomOut(); });
    $('[data-act="fit"]').addEventListener('click', fit);
    $('[data-act="3d"]').addEventListener('click', function () { setThreeD(!threeD); });
    $('[data-act="drop"]').addEventListener('click', function () { setDropping(!dropping); });
    $('[data-act="close"]').addEventListener('click', GothamMap.close);
    $('.gm-card [data-act="message"]').addEventListener('click', function () { if (selected) opts.message(selected); });
    $('.gm-card [data-act="call"]').addEventListener('click', function () { if (selected) opts.call(selected); });
    $('.gm-card [data-act="follow"]').addEventListener('click', function () {
      following = following === selected ? null : selected;
      renderCard(selected);
      if (following) map.easeTo({ center: people[following].getLngLat(), zoom: Math.max(map.getZoom(), 15), duration: 900 });
    });
    $('.gm-info__x').addEventListener('click', function () { $('.gm-info').hidden = true; openReport = null; });
    root.querySelectorAll('.gm-tab').forEach(function (t) {
      t.addEventListener('click', function () { showTab(t.dataset.tab); });
    });
    // The side panel folds away for more map; remembered.
    function side(hidden) {
      root.classList.toggle('is-wide', hidden);
      // Folding the drawer of a squeezed map isn't a preference; folding the panel is.
      if (!root.classList.contains('is-cramped')) remember('gotham-map-side-hidden', hidden);
    }
    $('.gm-side__toggle').addEventListener('click', function () { side(!root.classList.contains('is-wide')); });
    if (recall('gotham-map-side-hidden', false)) root.classList.add('is-wide');
    // Squeezed — the chat open beside it, a small window — the side panel turns
    // into a drawer over the map, folded until asked for; given room again, it's
    // as he left it. (The canvas follows its container on its own.)
    var cramped = null;
    new ResizeObserver(function () {
      if (!root.clientWidth) return;            // closed
      var now = root.clientWidth < 820;
      if (now === cramped) return;
      cramped = now;
      root.classList.toggle('is-cramped', now);
      root.classList.toggle('is-wide', now || !!recall('gotham-map-side-hidden', false));
    }).observe(root);
    $('.gm-card [data-act="dismiss"]').addEventListener('click', function () {
      selected = null; following = null;
      $('.gm-card').hidden = true;
      map.getSource('trail').setData({ type: 'FeatureCollection', features: [] });
      placePeople();
    });
  }

  /* --- public ------------------------------------------------------------------- */

  var GothamMap = {
    init: function (options) {
      opts = options;
      root = options.root;
      gazetteer = options.data || {};
      if (!window.maplibregl) return;
      hover = document.createElement('div');
      hover.className = 'gm-hover';
      hover.hidden = true;
      $('.gm__body').appendChild(hover);
      map = new maplibregl.Map({
        container: $('.gm-canvas'), style: style(), center: ll(50, 70), zoom: 12,
        // Zoomed all the way out, the land and water still run past every edge.
        // Held near Gotham: zoomed out, the tilt eases off, so the far horizon
        // never comes into view.
        minZoom: 11, maxZoom: 18.5, maxPitch: 70, attributionControl: false,
        renderWorldCopies: false, dragRotate: true, pitchWithRotate: true,
        // The view never leaves the county the map fills: no edge, no grey beyond it.
        maxBounds: [ll(-58, 163), ll(183, -68)]
      });
      // Zoomed out, the tilt eases off — the city fills the view and the far
      // horizon, where the county ends, never comes into it.
      function tiltFor() {
        // Flat all the way out; the tilt comes back as you close in — 60° by
        // street level, 70° right down among the roofs.
        var z = map.getZoom();
        var most = z <= 11.7 ? 0 : z >= 14 ? 70 : z >= 13.2 ? 60 + (z - 13.2) / 0.8 * 10 : (z - 11.7) / 1.5 * 60;
        if (Math.abs(map.getMaxPitch() - most) > 0.5) map.setMaxPitch(most);
      }
      map.on('zoom', tiltFor);
      tiltFor();
      map.on('load', function () {
        ICON_KINDS.forEach(function (kind) {
          if (!map.hasImage('gm-' + kind)) map.addImage('gm-' + kind, icon(kind), { pixelRatio: 2 });
        });
        [1, 2, 3, 4].forEach(function (s) { map.addImage('gm-warn-' + s, warning(s), { pixelRatio: 2 }); });
        map.addImage('gm-boat', sprite(30, boat), { pixelRatio: 2 });
        map.addImage('gm-heli', sprite(30, heli), { pixelRatio: 2 });
        map.addImage('gm-train', sprite(46, train), { pixelRatio: 2 });
        map.addImage('gm-ship', sprite(50, ship), { pixelRatio: 2 });
        map.addImage('gm-tug', sprite(20, tug), { pixelRatio: 2 });
        map.addImage('gm-sail', sprite(16, sail), { pixelRatio: 2 });
        map.addImage('gm-launch', sprite(22, launch), { pixelRatio: 2 });
        map.addImage('gm-plane', sprite(32, airliner), { pixelRatio: 2 });
        map.addImage('gm-plane-shadow', sprite(32, function (ctx) { airliner(ctx, true); }), { pixelRatio: 2 });
        map.addImage('gm-heli-news', sprite(30, livery('#e8f3fb', '#ffffff')), { pixelRatio: 2 });
        map.addImage('gm-heli-med', sprite(30, livery('#f2f2f2', '#ff3b3b')), { pixelRatio: 2 });
        map.addImage('gm-heli-civ', sprite(30, livery('#16324e', '#ffd27f')), { pixelRatio: 2 });
        map.addImage('gm-airship', sprite(46, airshipSprite(false)), { pixelRatio: 2 });
        map.addImage('gm-batwing', sprite(40, batwingSprite(false)), { pixelRatio: 2 });
        map.addImage('gm-batwing-shadow', sprite(40, batwingSprite(true)), { pixelRatio: 2 });
        map.addImage('gm-airship-shadow', sprite(46, airshipSprite(true)), { pixelRatio: 2 });
        ready = true;
        loadCity();
        ['places', 'streets', 'buildings', 'transit', 'crime', 'life', 'trails', 'people', 'pins'].forEach(function (name) {
          setLayer(name, layerShown(name));
        });
        // Safety is a view you ask for, not the default.
        setLayer('safety', recall(SHOWN_KEY, {}).safety === true);
        loadIncidents();
        setInterval(loadIncidents, 60000);
        batSignal();
        lighting();
        setInterval(lighting, 60000);                      // the dusk comes on minute by minute
        setInterval(batSignal, 300000);
        startAnimating();
        loadPins();
        if (!root.hidden) { map.resize(); if (!fitted) { fit(); fitted = true; } placePeople(); }
      });
      map.on('click', function (e) {
        if (dropping) { dropPin(e.lngLat); return; }
        // A click on nothing in particular puts down whatever was picked up.
        var hit = map.queryRenderedFeatures(e.point, { layers: ['landmark', 'spot', 'district-label', 'quarter-label', 'incident']
          .filter(function (id) { return map.getLayer(id); }) });
        if (hit.length) return;
        $('.gm-info').hidden = true; openReport = null;
        if (selected) deselect();
      });
      map.on('contextmenu', function (e) { e.preventDefault(); dropPin(e.lngLat); });
      map.on('dragstart', function () { if (following) { following = null; if (selected) renderCard(selected); } });
      map.on('pitchend', function () {
        var tilted = map.getPitch() > 10;
        if (tilted !== threeD) relief(tilted);
        threeD = tilted;
        $('[data-act="3d"]').classList.toggle('is-on', threeD);
      });
      map.on('movestart', function () { moving = true; });
      map.on('moveend', function () { moving = false; });
      map.on('zoom', fogFor);
      map.on('zoom', function () {
        var most = 30 + Math.max(0, Math.min(40, (map.getZoom() - 11) * 22));
        if (Math.abs(map.getMaxPitch() - most) > 0.5) map.setMaxPitch(most);
      });
      wireHover();
      wireFrame();
      wireSearch();
    },
    open: function (focus) {
      if (!map) return;
      root.hidden = false;
      document.documentElement.dataset.map = 'open';
      if (opts.toggled) opts.toggled(true);
      setTimeout(function () {
        map.resize();
        if (ready && !fitted) { fit(); fitted = true; }
        placePeople();
        loadIncidents();
        fogFor();
        startAnimating();
        if (focus) select(focus, true);
      }, 30);
    },
    close: function () {
      if (!root) return;
      root.hidden = true;
      setDropping(false);
      if (hover) hover.hidden = true;
      delete document.documentElement.dataset.map;
      if (opts.toggled) opts.toggled(false);
    },
    isOpen: function () { return !!root && !root.hidden; },
    // Escape on the map: a card first, then whoever's picked — and only then the map.
    escape: function () {
      if (!root || root.hidden) return false;
      if (!$('.gm-info').hidden) { $('.gm-info').hidden = true; openReport = null; return true; }
      if (selected) { deselect(); return true; }
      return false;
    },
    _map: function () { return map; },
    update: function () { if (map && !root.hidden) placePeople(); },
    refreshCases: function () { if (map && !root.hidden) loadIncidents(); },
    refreshPeople: function () { if (map && !root.hidden) placePeople(); },
    search: function () { if (root && !root.hidden) $('.gm-search__input').focus(); },
    focus: function (id) { if (map && !root.hidden) select(id, true); },
    // A place from the Codex: there, close enough to see it.
    flyTo: function (x, y) { if (map && !root.hidden) map.flyTo({ center: ll(x, y), zoom: Math.max(map.getZoom(), 15.4), duration: 1200 }); }
  };

  window.GothamMap = GothamMap;
})();
