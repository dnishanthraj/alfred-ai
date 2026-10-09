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
    void: '#02050a', land: '#0a131e', mainland: '#08101a', edge: '#4fa8e0', lit: '#8fd3ff',
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
    garden: C.good, water: C.edge, cemetery: C.faint, statue: C.lit, lighthouse: C.lit, airport: '#9fd8ff'
  };
  var ICON_KINDS = ['tower', 'manor', 'police', 'hospital', 'asylum', 'prison', 'church', 'theatre', 'university',
                    'museum', 'industry', 'station', 'stadium', 'lab', 'nightlife', 'civic', 'news', 'zoo', 'marina',
                    'naval', 'dock', 'clock', 'garden', 'water', 'cemetery', 'circus', 'statue', 'lighthouse',
                    'airport', 'observatory', 'hotel'];

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
      case 'circus': p.moveTo(-9, 8); p.lineTo(0, -6); p.lineTo(9, 8); p.closePath(); p.moveTo(0, -6); p.lineTo(0, -11); p.lineTo(4, -9.5); p.lineTo(0, -8); break;
      case 'statue': p.moveTo(2, -8); p.arc(0, -8, 2, 0, Math.PI * 2); p.moveTo(0, -6); p.lineTo(0, 4); p.moveTo(0, -4); p.lineTo(6, -11); p.moveTo(0, -3); p.lineTo(-4, 1); p.moveTo(-5, 9); p.lineTo(5, 9); p.lineTo(4, 4); p.lineTo(-4, 4); p.closePath(); break;
      case 'lighthouse': p.moveTo(-3, 9); p.lineTo(-2, -5); p.lineTo(2, -5); p.lineTo(3, 9); p.closePath(); p.rect(-3, -9, 6, 4); p.moveTo(-5, -7); p.lineTo(-9, -9); p.moveTo(5, -7); p.lineTo(9, -9); break;
      case 'airport': p.moveTo(0, -10); p.lineTo(0, 9); p.moveTo(-9, 1); p.lineTo(0, -3); p.lineTo(9, 1); p.moveTo(-4, 8); p.lineTo(0, 6); p.lineTo(4, 8); break;
      case 'observatory': p.moveTo(-8, 3); p.arc(0, 3, 8, Math.PI, 0); p.moveTo(-9, 3); p.lineTo(9, 3); p.lineTo(9, 8); p.lineTo(-9, 8); p.closePath(); p.moveTo(1, -5); p.lineTo(5, -9); break;
      case 'hotel': p.rect(-9, 0, 18, 5); p.moveTo(-9, 5); p.lineTo(-9, 9); p.moveTo(9, 5); p.lineTo(9, 9); p.moveTo(-9, 0); p.lineTo(-9, -6); p.rect(-6, -3, 5, 3); break;
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
      glyphs: '/static/map/fonts/{fontstack}/{range}.pbf',
      // The night over the city when it tilts: the horizon fades into fog.
      sky: { 'sky-color': '#02050a', 'horizon-color': '#0b2236', 'fog-color': '#02050a',
             'fog-ground-blend': 0.9, 'horizon-fog-blend': 0.35, 'sky-horizon-blend': 0.6, 'atmosphere-blend': 0 },
      sources: {
        city: { type: 'geojson', data: { type: 'FeatureCollection', features: [] }, generateId: true },
        buildings: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
        trees: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
        incidents: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
        trail: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
        // The ground itself: hills shaded in 2D, raised when the map tilts.
        terrain: { type: 'raster-dem', tiles: ['/static/map/terrain/{z}/{x}/{y}.png'], tileSize: 256,
                   encoding: 'mapbox', minzoom: 9, maxzoom: 11, bounds: [-0.48, -0.36, 0.38, 0.5] }
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
        { id: 'relief', type: 'hillshade', source: 'terrain',
          paint: { 'hillshade-exaggeration': 0.5, 'hillshade-shadow-color': '#000000',
                   'hillshade-highlight-color': '#1d4d6e', 'hillshade-accent-color': '#06121c',
                   'hillshade-illumination-direction': 315 } },
        { id: 'park', type: 'fill', source: 'city', filter: is('park'), paint: { 'fill-color': C.park, 'fill-opacity': 0.9 } },
        { id: 'plaza', type: 'fill', source: 'city', filter: is('plaza'), paint: { 'fill-color': '#122131', 'fill-opacity': 0.95 } },
        { id: 'plaza-edge', type: 'line', source: 'city', filter: is('plaza'),
          paint: { 'line-color': '#2f6f9c', 'line-width': 0.6, 'line-opacity': 0.5 } },
        { id: 'apron', type: 'fill', source: 'city', filter: is('apron'), paint: { 'fill-color': '#0e1f2e' } },
        { id: 'runway', type: 'fill', source: 'city', filter: is('runway'), paint: { 'fill-color': '#15293a' } },
        { id: 'runway-line', type: 'line', source: 'city', filter: is('runway_line'),
          paint: { 'line-color': '#8fb9d8', 'line-width': z([11, 0.4, 15, 1.6]), 'line-dasharray': [4, 4], 'line-opacity': 0.7 } },
        { id: 'park-edge', type: 'line', source: 'city', filter: is('park'),
          paint: { 'line-color': C.parkEdge, 'line-width': 0.8, 'line-opacity': 0.4 } },
        { id: 'water', type: 'fill', source: 'city', filter: is('water'), paint: { 'fill-color': C.water } },
        { id: 'water-edge', type: 'line', source: 'city', filter: is('water'),
          paint: { 'line-color': C.edge, 'line-width': 1, 'line-opacity': 0.45 } },
        // Light on the water, twinkling (animated in the loop below).
        { id: 'sparkle', type: 'circle', source: 'city', filter: is('sparkle'), minzoom: 11,
          paint: { 'circle-radius': z([11, 0.6, 15, 1.6, 18, 2.6]), 'circle-color': C.lit, 'circle-opacity': 0, 'circle-blur': 0.4 } },
        { id: 'ferry', type: 'line', source: 'city', filter: is('ferry'),
          paint: { 'line-color': '#3a7aa8', 'line-width': z([10, 0.6, 15, 1.4]), 'line-dasharray': [1, 3], 'line-opacity': 0.75 } },
        { id: 'pier', type: 'fill', source: 'city', filter: is('pier'), paint: { 'fill-color': '#163650', 'fill-opacity': 0.95 } },
        { id: 'coast', type: 'line', source: 'city', filter: is('land'),
          paint: { 'line-color': C.edge, 'line-width': z([10, 0.8, 14, 1.6, 17, 3]), 'line-opacity': 0.75 } },
        // Roads, quietest first. Widths grow with the zoom, as on any good map.
        // Streets show from far out as texture — the city's grain — and widen in.
        { id: 'street', type: 'line', source: 'city', filter: road('street'), minzoom: 10.6,
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': C.street, 'line-width': z([10.6, 0.25, 13, 0.6, 14, 0.9, 16, 3, 18, 10]),
                   'line-opacity': z([10.6, 0.35, 12.5, 0.75, 13.5, 0.95]) } },
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
        { id: 'rail', type: 'line', source: 'city', filter: road('rail'),
          paint: { 'line-color': '#5f86a6', 'line-width': z([10, 0.6, 14, 1.4, 17, 3]), 'line-dasharray': [3, 2.2], 'line-opacity': 0.8 } },
        // Underground, faintly: the subway, its lines in their own colours.
        { id: 'subway', type: 'line', source: 'city', filter: is('subway'), minzoom: 11.6,
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': ['get', 'col'], 'line-width': z([11.6, 1, 14, 2.4, 17, 4]),
                   'line-opacity': z([11.6, 0, 12.4, 0.55]) } },
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
        { id: 'footprint', type: 'line', source: 'buildings', minzoom: 13.4,
          paint: { 'line-color': ['case', ['has', 'n'], C.lit, '#2f6f9c'], 'line-width': z([13.4, 0.3, 16, 1, 18, 1.6]),
                   'line-opacity': z([13.4, 0, 14.2, 0.55]) } },
        // The city in three dimensions: dark volumes, brightening with height;
        // the landmarks lit, the skyway a deck in the air.
        { id: 'buildings', type: 'fill-extrusion', source: 'buildings', minzoom: 12.6,
          paint: {
            'fill-extrusion-color': ['match', ['get', 'k'], '~landmark', '#7ccaf5', '~deck', '#2a6a94',
              ['interpolate', ['linear'], ['get', 'h'], 0, '#081521', 25, '#0b2133', 60, '#10334d', 120, '#184d73', 240, '#2f78ad']],
            'fill-extrusion-height': ['interpolate', ['linear'], ['zoom'], 12.6, 0, 13.8, ['get', 'h']],
            'fill-extrusion-base': ['interpolate', ['linear'], ['zoom'], 12.6, 0, 13.8, ['get', 'b']],
            'fill-extrusion-opacity': 0.86,
            'fill-extrusion-vertical-gradient': true
          } },
        { id: 'trees', type: 'fill-extrusion', source: 'trees', minzoom: 13,
          paint: { 'fill-extrusion-color': ['interpolate', ['linear'], ['get', 'h'], 8, '#0f3d2d', 17, '#1d6b4c'],
                   'fill-extrusion-height': ['interpolate', ['linear'], ['zoom'], 13, 0, 14, ['get', 'h']],
                   'fill-extrusion-opacity': 0.92 } },
        // Where someone has been.
        { id: 'trail', type: 'line', source: 'trail', filter: ['==', ['geometry-type'], 'LineString'],
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': ['get', 'color'], 'line-width': 2.4, 'line-dasharray': [1.5, 1.8], 'line-opacity': 0.9 } },
        { id: 'trail-stop', type: 'circle', source: 'trail', filter: ['==', ['geometry-type'], 'Point'],
          paint: { 'circle-radius': 4, 'circle-color': C.void, 'circle-stroke-color': ['get', 'color'], 'circle-stroke-width': 1.8 } },
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
        { id: 'station', type: 'circle', source: 'city', filter: is('station'), minzoom: 12.6,
          paint: { 'circle-radius': z([12.6, 2, 15, 4, 18, 6]), 'circle-color': C.void,
                   'circle-stroke-color': ['get', 'col'], 'circle-stroke-width': z([12.6, 1, 15, 2]) } },
        { id: 'station-label', type: 'symbol', source: 'city', filter: is('station'), minzoom: 14.6,
          layout: { 'text-field': ['get', 'n'], 'text-font': ['Noto Sans Regular'], 'text-size': 10, 'text-offset': [0, 1.1],
                    'text-anchor': 'top', 'text-optional': true },
          paint: { 'text-color': ['get', 'col'], 'text-halo-color': C.void, 'text-halo-width': 1.4 } },
        // Words on top, laid out so they never collide.
        { id: 'water-label', type: 'symbol', source: 'city', filter: is('water_label'),
          layout: { 'text-field': ['get', 'n'], 'text-font': ['Noto Sans Italic'], 'text-size': z([10, 11, 14, 16]),
                    'text-letter-spacing': 0.35, 'text-rotate': ['get', 'r'], 'text-max-width': 30 },
          paint: { 'text-color': '#3a7aa8', 'text-opacity': 0.85 } },
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
          paint: { 'text-color': '#b9d3e6', 'text-halo-color': C.void, 'text-halo-width': 1.6 } }
      ]
    };
  }

  function loadCity() {
    return fetch('/static/map/gotham.geojson').then(function (r) { return r.json(); }).then(function (fc) {
      fc.features.forEach(function (f) { f.geometry.coordinates = convert(f.geometry.coordinates); });
      map.getSource('city').setData(fc);
      return fetch('/static/map/gotham-buildings.json');
    }).then(function (r) { return r.json(); }).then(function (rows) {
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
      return fetch('/static/map/gotham-trees.json');
    }).then(function (r) { return r.json(); }).then(function (trees) {
      // Each tree a small hexagon, so it can stand up in 3D.
      // Each tree a round crown of its own size, so it stands up in 3D.
      var features = trees.map(function (t) {
        var x = t[0] / 100, y = t[1] / 100, r = (t[3] || 9) / 100, ring = [];
        for (var i = 0; i <= 12; i++) {
          var a = i * Math.PI / 6;
          ring.push(ll(x + Math.cos(a) * r, y + Math.sin(a) * r));
        }
        return { type: 'Feature', geometry: { type: 'Polygon', coordinates: [ring] }, properties: { h: t[2] } };
      });
      map.getSource('trees').setData({ type: 'FeatureCollection', features: features });
    }).catch(function () { /* the city still draws without its buildings */ });
  }

  /* --- hovering: the console's own tooltip, never the map's ----------------- */

  var HOVERABLE = ['incident', 'landmark', 'station', 'district-label', 'quarter-label', 'road-label', 'primary',
                   'highway', 'avenue', 'secondary', 'subway', 'ferry', 'park', 'water', 'trail-stop', 'district'];

  function describe(f) {
    var p = f.properties || {};
    if (p.l === 'place') return p.n + (p.a && p.a !== p.n ? ' · ' + p.a : '');
    if (p.l === 'stop') return p.n;
    if (p.kind) return p.kind + ' · ' + p.place + ' · ' + p.status;
    if (p.l === 'station') return p.n + ' · ' + p.line;
    if (p.l === 'subway' || p.l === 'ferry') return p.n;
    if ((p.l === 'road' || p.l === 'park' || p.l === 'water' || p.l === 'district') && p.n) return p.n;
    return '';
  }

  function wireHover() {
    map.on('mousemove', function (e) {
      var hits = map.queryRenderedFeatures(e.point, { layers: HOVERABLE.filter(function (id) { return map.getLayer(id); }) });
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
      if (!top) { hover.hidden = true; map.getCanvas().style.cursor = dropping ? 'crosshair' : ''; return; }
      hover.textContent = describe(top);
      hover.hidden = false;
      hover.style.left = (e.point.x + 14) + 'px';
      hover.style.top = (e.point.y + 14) + 'px';
      map.getCanvas().style.cursor = dropping ? 'crosshair' : (top.properties.l === 'place' ? 'pointer' : '');
    });
    map.getCanvas().addEventListener('mouseleave', function () { hover.hidden = true; });
    map.on('click', 'landmark', function (e) {
      if (dropping) return;
      var f = e.features[0];
      map.flyTo({ center: f.geometry.coordinates, zoom: Math.max(map.getZoom(), 15.4), duration: 900 });
      placeCard(f.properties.n);
    });
    map.on('click', 'incident', function (e) { if (!dropping) incidentCard(e.features[0].properties); });
  }

  /* --- people ----------------------------------------------------------------- */

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

  function placePeople() {
    if (!ready) return;
    var byspot = {};
    visible().forEach(function (c) {
      var key = c.presence.spot.x.toFixed(1) + ',' + c.presence.spot.y.toFixed(1);
      (byspot[key] = byspot[key] || []).push(c);
    });
    var seen = {};
    var showPeople = layerShown('people');
    Object.keys(byspot).forEach(function (key) {
      var group = byspot[key];
      group.forEach(function (c, i) {
        var s = c.presence.spot;
        var angle = (i / group.length) * Math.PI * 2, r = group.length > 1 ? 0.55 : 0;
        var at = ll(s.x + Math.cos(angle) * r, s.y + Math.sin(angle) * r);
        seen[c.id] = true;
        var marker = people[c.id];
        if (!marker) {
          var el = document.createElement('div');
          el.className = 'gm-person';
          el.innerHTML = '<span class="gm-person__ring"></span><span class="gm-person__face"></span>' +
                         '<span class="gm-person__name"></span>';
          el.style.setProperty('--accent', c.accent);
          el.querySelector('.gm-person__name').textContent = c.name;
          opts.portrait(el.querySelector('.gm-person__face'), c);
          el.addEventListener('click', function (e) { e.stopPropagation(); select(c.id); });
          marker = people[c.id] = new maplibregl.Marker({ element: el, anchor: 'center' }).setLngLat(at).addTo(map);
        } else {
          glide(marker, at);
        }
        var node = marker.getElement();
        node.dataset.status = c.presence.status;
        node.classList.toggle('is-selected', selected === c.id);
        node.setAttribute('data-tip', c.name + ' · ' + (c.presence.where || ''));
        node.style.display = showPeople ? '' : 'none';
      });
    });
    Object.keys(people).forEach(function (id) {
      if (!seen[id]) { people[id].remove(); delete people[id]; }
    });
    renderRoster();
    if (selected) renderCard(selected);
    if (following && people[following]) map.easeTo({ center: people[following].getLngLat(), duration: 1200 });
  }

  function renderRoster() {
    var list = $('.gm-roster');
    var contacts = opts.contacts();
    list.innerHTML = '';
    opts.order().forEach(function (id) {
      var c = contacts[id];
      if (!c) return;
      var p = c.presence || {};
      var li = document.createElement('li');
      li.className = 'gm-roster__item' + (p.spot ? '' : ' is-hidden') + (selected === id ? ' is-on' : '');
      li.style.setProperty('--accent', c.accent);
      var face = document.createElement('span');
      face.className = 'gm-roster__face';
      opts.portrait(face, c);
      var text = document.createElement('span');
      text.className = 'gm-roster__text';
      text.innerHTML = '<b></b><small></small>';
      text.querySelector('b').textContent = c.name;
      text.querySelector('small').textContent = p.spot ? (p.where || p.spot.name) : 'Location hidden';
      li.appendChild(face);
      li.appendChild(text);
      li.dataset.status = p.status || '';
      if (p.spot) li.addEventListener('click', function () { select(id, true); });
      list.appendChild(li);
    });
  }

  function select(id, fly) {
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

  function renderCard(id) {
    var card = $('.gm-card');
    var c = opts.contacts()[id];
    if (!c || !c.presence || !c.presence.spot) { card.hidden = true; return; }
    var p = c.presence;
    card.hidden = false;
    card.style.setProperty('--accent', c.accent);
    opts.portrait(card.querySelector('.gm-card__face'), c);
    card.querySelector('.gm-card__name').textContent = c.full_name;
    card.querySelector('.gm-card__status').textContent = opts.label(c);
    var company = (p['with'] || []).map(function (cid) { return (opts.contacts()[cid] || {}).name; }).filter(Boolean);
    card.querySelector('.gm-card__where').textContent = (p.where || p.spot.name) +
      (p.spot.area && p.spot.area !== p.where && p.spot.area !== p.spot.name ? ' · ' + p.spot.area : '') +
      (company.length ? ' · with ' + company.join(', ') : '');
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
        var pts = body.trail || [];
        if (pts.length < 2) return;
        var features = [{ type: 'Feature', properties: { color: c.accent },
                          geometry: { type: 'LineString', coordinates: pts.map(function (p) { return ll(p.x, p.y); }) } }];
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
    card.querySelector('.gm-info__assign').hidden = true;
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

  function placeCard(name) {
    var p = (gazetteer.places || []).filter(function (q) { return q.name === name; })[0];
    if (!p) return;
    showInfo(p.name, p.area, p.bio, p.note, iconUrl(p.icon || ''), ICON_TINT[p.icon]);
  }

  function incidentCard(report) {
    openReport = report.id;
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
    showInfo(report.kind, report.place + ' · ' + clock(report.at) + ' · ' + report.status,
             report.dispatch ? 'Dispatch: \u201c' + report.dispatch + '\u201d' : 'Dispatch is still coming through.',
             '', canvas.toDataURL(), SEVERITY[report.severity]);
    $('.gm-info__note').hidden = false;
    $('.gm-info__note').textContent = state;
    $('.gm-info__note').classList.add('is-plain');
    // Put someone on it: the ones who work scenes, nearest first.
    var assign = $('.gm-info__assign'), people = $('.gm-info__people');
    people.innerHTML = '';
    if (report.case !== 'closed') {
      FIELD.map(function (id) { return contacts[id]; }).filter(Boolean).sort(function (a, b) {
        var sa = (a.presence || {}).spot, sb = (b.presence || {}).spot;
        var da = sa ? Math.hypot(sa.x - report.x, sa.y - report.y) : 99, db = sb ? Math.hypot(sb.x - report.x, sb.y - report.y) : 99;
        return da - db;
      }).forEach(function (c) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'gm-assign' + (report.assignee === c.id ? ' is-on' : '');
        b.style.setProperty('--accent', c.accent);
        b.setAttribute('data-tip', 'Put ' + c.name + ' on it');
        var face = document.createElement('span');
        opts.portrait(face, c);
        b.appendChild(face);
        b.appendChild(document.createTextNode(c.name));
        b.addEventListener('click', function () {
          fetch('/api/cases/assign', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                       body: JSON.stringify({ report: report.id, contact: c.id }) })
            .then(function () { loadIncidents(); });
        });
        people.appendChild(b);
      });
    }
    assign.hidden = report.case === 'closed';
  }

  /* --- the scanner ------------------------------------------------------------ */

  var reports = [];
  function loadIncidents() {
    if (!ready || root.hidden) return;
    fetch('/api/map/incidents').then(function (r) { return r.json(); }).then(function (body) {
      reports = body.incidents || [];
      var contacts = opts.contacts();
      map.getSource('incidents').setData({ type: 'FeatureCollection', features: reports.filter(function (r) {
        return layerShown('crime');
      }).map(function (r) {
        var props = Object.assign({}, r);
        if (r.assignee && contacts[r.assignee]) props.acc = contacts[r.assignee].accent;
        return { type: 'Feature', geometry: { type: 'Point', coordinates: ll(r.x, r.y) }, properties: props };
      }) });
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
    root.querySelectorAll('.gm-tab').forEach(function (t) { t.classList.toggle('is-on', t.dataset.tab === name); });
    root.querySelectorAll('[data-pane]').forEach(function (p) { p.hidden = p.dataset.pane !== name; });
  }

  function clock(at) {
    var d = new Date(at * 1000);
    return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  }

  function reportItem(r, withWho) {
    var li = document.createElement('li');
    li.className = 'gm-list__item';
    li.style.setProperty('--sev', SEVERITY[r.severity]);
    var who = r.assignee && opts.contacts()[r.assignee];
    li.innerHTML = '<i class="gm-list__sev"></i><span class="gm-list__text"><b></b><small></small></span>';
    li.querySelector('b').textContent = r.kind;
    li.querySelector('small').textContent = r.place + ' · ' + clock(r.at) + ' · ' +
      (who ? who.name + ' — ' + (r.case === 'closed' ? 'closed' : r.case) : r.status);
    if (withWho && who) {
      var face = document.createElement('span');
      face.className = 'gm-list__face';
      face.style.setProperty('--accent', who.accent);
      opts.portrait(face, who);
      li.appendChild(face);
    }
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

  var frame = null, last = 0;
  function animate(now) {
    frame = requestAnimationFrame(animate);
    if (root.hidden || !ready || now - last < 90) return;
    last = now;
    var t = now / 700;
    // Each point of light catches it briefly, then not, in its own rhythm.
    map.setPaintProperty('sparkle', 'circle-opacity',
      ['*', 0.75, ['^', ['max', 0, ['sin', ['+', ['get', 'p'], t]]], 14]]);
    var pulse = (now % 1800) / 1800;
    map.setPaintProperty('incident-pulse', 'circle-radius', 8 + pulse * 22);
    map.setPaintProperty('incident-pulse', 'circle-opacity', 0.35 * (1 - pulse));
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
    var marker = new maplibregl.Marker({ element: el, anchor: 'bottom-left', draggable: true })
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
    places: ['landmark', 'quarter-label'],
    streets: ['street', 'avenue', 'road-label'],
    buildings: ['buildings', 'footprint', 'trees'],
    transit: ['subway', 'station', 'station-label', 'ferry'],
    crime: ['incident', 'incident-pulse'],
    safety: ['safety'],
    trails: ['trail', 'trail-stop']
  };

  function layerShown(name) { return recall(SHOWN_KEY, {})[name] !== false; }

  function setLayer(name, on) {
    if (on === undefined) on = !layerShown(name);
    var shown = recall(SHOWN_KEY, {});
    shown[name] = on;
    remember(SHOWN_KEY, shown);
    (LAYER_GROUPS[name] || []).forEach(function (id) {
      if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', on ? 'visible' : 'none');
    });
    if (name === 'people') Object.keys(people).forEach(function (id) { people[id].getElement().style.display = on ? '' : 'none'; });
    if (name === 'pins') Object.keys(pins).forEach(function (id) { pins[id].getElement().style.display = on ? '' : 'none'; });
    var chip = $('[data-layer="' + name + '"]');
    if (chip) chip.classList.toggle('is-on', on);
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
      remember('gotham-map-side-hidden', hidden);
      setTimeout(function () { map.resize(); }, 260);
    }
    $('.gm-side__toggle').addEventListener('click', function () { side(!root.classList.contains('is-wide')); });
    if (recall('gotham-map-side-hidden', false)) root.classList.add('is-wide');
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
        maxBounds: [ll(-130, 230), ll(230, -110)]
      });
      map.on('load', function () {
        ICON_KINDS.forEach(function (kind) {
          if (!map.hasImage('gm-' + kind)) map.addImage('gm-' + kind, icon(kind), { pixelRatio: 2 });
        });
        [1, 2, 3, 4].forEach(function (s) { map.addImage('gm-warn-' + s, warning(s), { pixelRatio: 2 }); });
        ready = true;
        loadCity();
        ['places', 'streets', 'buildings', 'transit', 'crime', 'trails', 'people', 'pins'].forEach(function (name) {
          setLayer(name, layerShown(name));
        });
        // Safety is a view you ask for, not the default.
        setLayer('safety', recall(SHOWN_KEY, {}).safety === true);
        loadIncidents();
        setInterval(loadIncidents, 60000);
        frame = requestAnimationFrame(animate);
        loadPins();
        if (!root.hidden) { map.resize(); if (!fitted) { fit(); fitted = true; } placePeople(); }
      });
      map.on('click', function (e) { if (dropping) dropPin(e.lngLat); });
      map.on('contextmenu', function (e) { e.preventDefault(); dropPin(e.lngLat); });
      map.on('dragstart', function () { if (following) { following = null; if (selected) renderCard(selected); } });
      map.on('pitchend', function () {
        var tilted = map.getPitch() > 10;
        if (tilted !== threeD) relief(tilted);
        threeD = tilted;
        $('[data-act="3d"]').classList.toggle('is-on', threeD);
      });
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
    _map: function () { return map; },
    update: function () { if (map && !root.hidden) placePeople(); },
    refreshCases: function () { if (map && !root.hidden) loadIncidents(); },
    search: function () { if (root && !root.hidden) $('.gm-search__input').focus(); },
    focus: function (id) { if (map && !root.hidden) select(id, true); }
  };

  window.GothamMap = GothamMap;
})();
