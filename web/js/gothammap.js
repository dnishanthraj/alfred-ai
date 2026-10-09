/* ==========================================================================
   The map of Gotham — the whole city, live.

   Drawn from the same gazetteer the server places people with
   (wayne/engine/gotham.json): Eliot R. Brown's canon three islands and the
   rivers between them, the mainland and Gotham County, the outer islands,
   Blüdhaven up the coast. Coastlines, parks, avenues, bridges and a street
   grid are vector layers in the console's own palette — nothing is a picture,
   so it themes and stays sharp at any zoom.

   On it: everyone who shares where they are, as their portrait in their own
   colour, moving as their presence changes; where they've been lately; pins
   of his own. Hover anything for its name, click someone for who and where,
   search to fly somewhere, toggle what's shown, follow someone as they move.

   Leaflet (vendored, web/vendor/leaflet) does the panning and zooming.
   ========================================================================== */
(function () {
  'use strict';

  var map = null, opts = null, data = null, root = null;
  var layers = {}, people = {}, pins = {}, selected = null, following = null;
  var trailLayer = null;
  var dropping = false, fitted = false;
  var SHOWN_KEY = 'gotham-map-layers';

  // Map units: the gazetteer's 0–100, ten to a unit, north up.
  function ll(x, y) { return [(100 - y) * 10, x * 10]; }
  function xy(latlng) { return { x: latlng.lng / 10, y: 100 - latlng.lat / 10 }; }
  function $(sel) { return root.querySelector(sel); }

  function remember(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* private window */ } }
  function recall(key, fallback) {
    try { var v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch (e) { return fallback; }
  }

  function tip(layer, text) {
    // The console's own tooltip, never Leaflet's or the browser's.
    var node = layer.getElement && layer.getElement();
    if (node) node.setAttribute('data-tip', text);
  }

  function label(cls, html) {
    // Leaflet positions a marker with its own transform; centring goes on the inside.
    return L.divIcon({ className: 'gm-label ' + cls, html: '<span class="gm-in">' + html + '</span>', iconSize: [0, 0] });
  }

  /* --- the city ---------------------------------------------------------- */

  function streetGrid() {
    // A grid per landmass, clipped to its coast; Downtown's is set at an
    // angle, like an old city that grew before the grid did.
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 1000 1000');
    var defs = '', body = '';
    data.land.forEach(function (land, i) {
      var pts = land.coast.map(function (p) { return (p[0] * 10) + ',' + (p[1] * 10); }).join(' ');
      defs += '<clipPath id="gm-clip-' + i + '"><polygon points="' + pts + '"/></clipPath>';
      var gap = land.mainland ? 26 : 13, cross = land.mainland ? 40 : 22;
      var angle = land.name === 'Downtown' ? -9 : (land.name === 'Uptown' ? 4 : 0);
      var d = '';
      for (var y = -200; y < 1200; y += gap) d += 'M-200 ' + y + 'H1200';
      for (var x = -200; x < 1200; x += cross) d += 'M' + x + ' -200V1200';
      body += '<g clip-path="url(#gm-clip-' + i + ')"><path class="gm-street" d="' + d +
              '" transform="rotate(' + angle + ' 500 500)"/></g>';
    });
    svg.innerHTML = '<defs>' + defs + '</defs>' + body;
    return L.svgOverlay(svg, [[0, 0], [1000, 1000]], { interactive: false, className: 'gm-streets' });
  }

  function buildCity() {
    var city = L.layerGroup(), streets = L.layerGroup(), places = L.layerGroup();
    data.land.forEach(function (land) {
      var shape = L.polygon(land.coast.map(function (p) { return ll(p[0], p[1]); }),
                            { className: 'gm-land' + (land.mainland ? ' gm-land--main' : ''), weight: 1 });
      shape.on('add', function () { tip(shape, land.name); });
      city.addLayer(shape);
    });
    (data.parks || []).forEach(function (park) {
      var shape = L.polygon(park.coast.map(function (p) { return ll(p[0], p[1]); }),
                            { className: 'gm-park', weight: 1 });
      shape.on('add', function () { tip(shape, park.name); });
      city.addLayer(shape);
    });
    streets.addLayer(streetGrid());
    (data.avenues || []).forEach(function (road) {
      var line = L.polyline(road.line.map(function (p) { return ll(p[0], p[1]); }),
                            { className: 'gm-avenue', weight: 2.2 });
      line.on('add', function () { tip(line, road.name); });
      streets.addLayer(line);
    });
    (data.bridges || []).forEach(function (bridge) {
      var line = L.polyline(bridge.line.map(function (p) { return ll(p[0], p[1]); }),
                            { className: bridge.rail ? 'gm-rail' : 'gm-bridge', weight: bridge.rail ? 1.4 : 3 });
      line.on('add', function () { tip(line, bridge.name); });
      city.addLayer(line);
    });
    (data.water || []).forEach(function (w) {
      city.addLayer(L.marker(ll(w.at[0], w.at[1]), {
        icon: label('gm-label--water', '<span style="transform:rotate(' + (w.rotate || 0) + 'deg)">' + w.name + '</span>'),
        interactive: false, keyboard: false
      }));
    });
    data.land.forEach(function (land) {
      if (!land.label) return;
      city.addLayer(L.marker(ll(land.label[0], land.label[1]), {
        icon: label('gm-label--area', land.name), interactive: false, keyboard: false
      }));
    });
    (data.places || []).forEach(function (place) {
      var landmark = place.kind !== 'district';
      var marker = L.marker(ll(place.x, place.y), {
        icon: L.divIcon({
          className: 'gm-place gm-place--' + (landmark ? 'landmark' : 'district'),
          html: '<span class="gm-in">' + (landmark ? '<i></i>' : '') + '<b>' + place.name + '</b></span>',
          iconSize: [0, 0]
        }),
        keyboard: false, riseOnHover: true
      });
      marker.on('add', function () { tip(marker, place.name + ' · ' + place.area); });
      marker.on('click', function () { map.flyTo(ll(place.x, place.y), Math.max(map.getZoom(), 1.6), { duration: 0.6 }); });
      places.addLayer(marker);
    });
    layers.city = city.addTo(map);
    layers.streets = streets;
    layers.places = places;
    layers.people = L.layerGroup();
    layers.trails = L.layerGroup();
    layers.pins = L.layerGroup();
  }

  /* --- people ------------------------------------------------------------ */

  function visible() {
    var contacts = opts.contacts();
    return Object.keys(contacts).map(function (id) { return contacts[id]; })
      .filter(function (c) { return c.presence && c.presence.spot; });
  }

  function placePeople() {
    // Two at the same spot stand side by side, not on top of each other.
    var byspot = {};
    visible().forEach(function (c) {
      var key = c.presence.spot.x.toFixed(1) + ',' + c.presence.spot.y.toFixed(1);
      (byspot[key] = byspot[key] || []).push(c);
    });
    var seen = {};
    Object.keys(byspot).forEach(function (key) {
      var group = byspot[key];
      group.forEach(function (c, i) {
        var s = c.presence.spot;
        var angle = (i / group.length) * Math.PI * 2;
        var r = group.length > 1 ? 2.4 : 0;
        var at = ll(s.x + Math.cos(angle) * r, s.y + Math.sin(angle) * r);
        seen[c.id] = true;
        var marker = people[c.id];
        if (!marker) {
          marker = people[c.id] = L.marker(at, {
            icon: L.divIcon({
              className: 'gm-person',
              html: '<span class="gm-person__ring"></span><span class="gm-person__face"></span>' +
                    '<span class="gm-person__name">' + c.name + '</span>',
              iconSize: [34, 34], iconAnchor: [17, 17]
            }),
            zIndexOffset: 1000, keyboard: false, riseOnHover: true
          });
          marker.on('add', function () {
            var node = marker.getElement();
            node.style.setProperty('--accent', c.accent);
            opts.portrait(node.querySelector('.gm-person__face'), c);
            node.setAttribute('data-tip', c.name);
          });
          marker.on('click', function () { select(c.id); });
          layers.people.addLayer(marker);
        } else {
          var node = marker.getElement();
          var was = marker.getLatLng();
          if (node && (Math.abs(was.lat - at[0]) > 0.01 || Math.abs(was.lng - at[1]) > 0.01)) {
            // Moving: glide there, rather than jump.
            node.classList.add('is-moving');
            setTimeout(function () { node.classList.remove('is-moving'); }, 1300);
          }
          marker.setLatLng(at);
        }
        var el = marker.getElement();
        if (el) {
          el.dataset.status = c.presence.status;
          el.classList.toggle('is-selected', selected === c.id);
          el.setAttribute('data-tip', c.name + ' · ' + (c.presence.where || ''));
        }
      });
    });
    Object.keys(people).forEach(function (id) {
      if (!seen[id]) { layers.people.removeLayer(people[id]); delete people[id]; }
    });
    renderRoster();
    if (selected) renderCard(selected);
    if (following && people[following]) map.panTo(people[following].getLatLng(), { animate: true, duration: 1 });
  }

  function renderRoster() {
    var list = $('.gm-roster');
    if (!list) return;
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
    if (marker && fly) map.flyTo(marker.getLatLng(), Math.max(map.getZoom(), 1.4), { duration: 0.7 });
    Object.keys(people).forEach(function (other) {
      var el = people[other].getElement();
      if (el) el.classList.toggle('is-selected', other === id);
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
    card.querySelector('.gm-card__status').dataset.status = p.status;
    var company = (p['with'] || []).map(function (cid) { return (opts.contacts()[cid] || {}).name; }).filter(Boolean);
    card.querySelector('.gm-card__where').textContent = (p.where || p.spot.name) +
      (p.spot.area && p.spot.area !== p.where ? ' · ' + p.spot.area : '') +
      (company.length ? ' · with ' + company.join(', ') : '');
    card.querySelector('[data-act="follow"]').classList.toggle('is-on', following === id);
  }

  function showTrail(id) {
    layers.trails.clearLayers();
    var c = opts.contacts()[id];
    if (!c) return;
    fetch('/api/map/trail/' + encodeURIComponent(id) + '?hours=3').then(function (r) { return r.json(); })
      .then(function (body) {
        if (selected !== id) return;
        var pts = (body.trail || []);
        if (pts.length < 2) return;
        var line = L.polyline(pts.map(function (p) { return ll(p.x, p.y); }),
                              { className: 'gm-trail', color: c.accent, weight: 2, dashArray: '4 6' });
        layers.trails.addLayer(line);
        pts.slice(0, -1).forEach(function (p) {
          var when = new Date(p.at * 1000);
          var dot = L.circleMarker(ll(p.x, p.y), { radius: 3, className: 'gm-trail__stop', color: c.accent, weight: 1.5 });
          dot.on('add', function () {
            tip(dot, String(when.getHours()).padStart(2, '0') + ':' + String(when.getMinutes()).padStart(2, '0') + ' · ' + p.where);
          });
          layers.trails.addLayer(dot);
        });
      }).catch(function () { /* the map still works without a trail */ });
  }

  /* --- pins of his own --------------------------------------------------- */

  function loadPins() {
    fetch('/api/map/pins').then(function (r) { return r.json(); }).then(function (body) {
      layers.pins.clearLayers();
      pins = {};
      (body.pins || []).forEach(addPin);
    }).catch(function () {});
  }

  function addPin(pin) {
    var marker = L.marker(ll(pin.x, pin.y), {
      icon: L.divIcon({ className: 'gm-pin', html: '<span class="gm-in"><i></i><b></b></span>', iconSize: [0, 0] }),
      draggable: true, keyboard: false, zIndexOffset: 500
    });
    marker.on('add', function () {
      var node = marker.getElement();
      node.querySelector('b').textContent = pin.label;
      node.setAttribute('data-tip', pin.label + ' — drag to move, click to edit');
    });
    marker.on('click', function () { editPin(pin, marker); });
    marker.on('dragend', function () {
      var at = xy(marker.getLatLng());
      pin.x = +at.x.toFixed(2); pin.y = +at.y.toFixed(2);
      savePin(pin);
    });
    pins[pin.id] = marker;
    layers.pins.addLayer(marker);
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
    var popup = L.popup({ className: 'gm-popup', closeButton: false, offset: [0, -14] })
      .setLatLng(marker.getLatLng()).setContent(form).openOn(map);
    setTimeout(function () { input.focus(); input.select(); }, 30);
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      pin.label = input.value.trim() || 'Pin';
      savePin(pin).then(function () {
        map.closePopup(popup);
        var node = marker.getElement();
        if (node) node.querySelector('b').textContent = pin.label;
      });
    });
    form.querySelector('[data-act="delete"]').addEventListener('click', function () {
      fetch('/api/map/pins/' + encodeURIComponent(pin.id), { method: 'DELETE' }).then(function () {
        map.closePopup(popup);
        layers.pins.removeLayer(marker);
        delete pins[pin.id];
      });
    });
  }

  function dropPin(latlng) {
    var at = xy(latlng);
    savePin({ label: 'Pin', x: +at.x.toFixed(2), y: +at.y.toFixed(2) }).then(function (pin) {
      if (!pin) return;
      addPin(pin);
      if (!map.hasLayer(layers.pins)) toggleLayer('pins', true);
      editPin(pin, pins[pin.id]);
    });
    setDropping(false);
  }

  function setDropping(on) {
    dropping = on;
    root.classList.toggle('is-dropping', on);
    $('[data-act="drop"]').classList.toggle('is-on', on);
  }

  /* --- search ------------------------------------------------------------ */

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
      map.flyTo(ll(o.x, o.y), Math.max(map.getZoom(), 1.8), { duration: 0.7 });
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
      var contacts = opts.contacts();
      var found = [];
      Object.keys(contacts).forEach(function (id) {
        var c = contacts[id];
        if (c.presence && c.presence.spot && (c.name + ' ' + c.full_name).toLowerCase().indexOf(q) !== -1) {
          found.push({ name: c.full_name, kind: c.presence.where || 'Person', person: id });
        }
      });
      (data.places || []).forEach(function (p) {
        if ((p.name + ' ' + (p.match || []).join(' ')).toLowerCase().indexOf(q) !== -1) {
          found.push({ name: p.name, kind: p.area, x: p.x, y: p.y });
        }
      });
      Object.keys(pins).forEach(function (id) {
        var m = pins[id], text = m.getElement() && m.getElement().textContent;
        if (text && text.toLowerCase().indexOf(q) !== -1) {
          var at = xy(m.getLatLng());
          found.push({ name: text, kind: 'Your pin', x: at.x, y: at.y });
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
    var ring = L.circleMarker(at, { radius: 18, className: 'gm-flash', weight: 2 }).addTo(map);
    setTimeout(function () { map.removeLayer(ring); }, 1800);
  }

  /* --- layers and zoom --------------------------------------------------- */

  function toggleLayer(name, on) {
    var layer = layers[name];
    if (!layer) return;
    if (on === undefined) on = !map.hasLayer(layer);
    if (on) map.addLayer(layer); else map.removeLayer(layer);
    var chip = $('[data-layer="' + name + '"]');
    if (chip) chip.classList.toggle('is-on', on);
    var shown = recall(SHOWN_KEY, {});
    shown[name] = on;
    remember(SHOWN_KEY, shown);
  }

  function zoomClass() {
    var z = map.getZoom();
    root.dataset.zoom = z < 0 ? 'far' : (z < 1.1 ? 'mid' : 'near');
  }

  /* --- the frame --------------------------------------------------------- */

  function wireFrame() {
    root.querySelectorAll('[data-layer]').forEach(function (chip) {
      chip.addEventListener('click', function () { toggleLayer(chip.dataset.layer); });
    });
    $('[data-act="zoom-in"]').addEventListener('click', function () { map.zoomIn(); });
    $('[data-act="zoom-out"]').addEventListener('click', function () { map.zoomOut(); });
    $('[data-act="fit"]').addEventListener('click', fit);
    $('[data-act="drop"]').addEventListener('click', function () { setDropping(!dropping); });
    $('[data-act="close"]').addEventListener('click', GothamMap.close);
    $('.gm-card [data-act="message"]').addEventListener('click', function () { if (selected) opts.message(selected); });
    $('.gm-card [data-act="call"]').addEventListener('click', function () { if (selected) opts.call(selected); });
    $('.gm-card [data-act="follow"]').addEventListener('click', function () {
      following = following === selected ? null : selected;
      renderCard(selected);
      if (following) map.panTo(people[following].getLatLng());
    });
    $('.gm-card [data-act="dismiss"]').addEventListener('click', function () {
      selected = null; following = null;
      $('.gm-card').hidden = true;
      layers.trails.clearLayers();
      placePeople();
    });
  }

  function fit() { map.fitBounds([[0, 0], [1000, 1000]], { padding: [16, 16] }); }

  /* --- public ------------------------------------------------------------ */

  var GothamMap = {
    init: function (options) {
      opts = options;
      root = options.root;
      data = options.data;
      if (!data || !window.L) return;
      map = L.map($('.gm-canvas'), {
        crs: L.CRS.Simple, minZoom: -1.5, maxZoom: 3.2, zoomSnap: 0.25, zoomDelta: 0.5,
        wheelPxPerZoomLevel: 110, attributionControl: false, zoomControl: false,
        maxBounds: [[-250, -250], [1250, 1250]], maxBoundsViscosity: 0.7
      });
      buildCity();
      var shown = recall(SHOWN_KEY, { streets: true, places: true, people: true, trails: true, pins: true });
      ['streets', 'places', 'people', 'trails', 'pins'].forEach(function (name) {
        toggleLayer(name, shown[name] !== false);
      });
      map.on('zoomend', zoomClass);
      map.on('click', function (e) { if (dropping) dropPin(e.latlng); });
      map.on('contextmenu', function (e) { dropPin(e.latlng); });
      map.on('dragstart', function () { following = null; if (selected) renderCard(selected); });
      wireFrame();
      wireSearch();
      zoomClass();
      loadPins();
    },
    open: function (focus) {
      if (!map) return;
      root.hidden = false;
      document.documentElement.dataset.map = 'open';
      if (opts.toggled) opts.toggled(true);
      setTimeout(function () {
        map.invalidateSize();
        // Built while hidden, it had no size to fit to: the first look is the whole city.
        if (!fitted) { fit(); fitted = true; }
        placePeople();
        if (focus) select(focus, true);
      }, 30);
    },
    close: function () {
      if (!root) return;
      root.hidden = true;
      setDropping(false);
      delete document.documentElement.dataset.map;
      if (opts.toggled) opts.toggled(false);
    },
    isOpen: function () { return !!root && !root.hidden; },
    update: function () { if (map && !root.hidden) placePeople(); },
    focus: function (id) { if (map && !root.hidden) select(id, true); }
  };

  window.GothamMap = GothamMap;
})();
