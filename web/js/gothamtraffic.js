/* ==========================================================================
   Gotham's traffic: what moves on the map, and when.

   The subway under the streets and the trains on their lines, stopping at
   every station; ferries between their piers; ships in from the sea, berthing
   off the pier heads with a tug alongside; planes landing and taking off; the
   police, news, medevac, tour and company helicopters; Stagg's airship; sail-
   boats off the marina; the harbour launch; the lighthouse at night.

   Everything here is worked out from the clock, not stored: a train is where
   its timetable puts it, so every frame agrees with the last. And it all runs
   at its real speed — Gotham is a big city, and a container ship crossing the
   harbour takes the half hour it would. The timetables follow the hour:
   trains every few minutes at the rush, a handful in the small hours, no
   flights after one in the morning.
   ========================================================================== */
(function () {
  'use strict';

  var K = 0.0013475;                // degrees in a layout unit (~150 m), as in gothammap.js
  // Real speeds, in layout units a second: a unit is 150 m.
  var METRO = 0.055 * K;            // the subway, ~30 km/h with its stops
  var TRAIN = 0.11 * K;             // the railways, ~60 km/h
  var FERRY = 0.05 * K;             // ~15 knots
  var SHIP = 0.034 * K;             // ~10 knots in the harbour
  var SAIL = 0.017 * K;             // ~5 knots
  var LAUNCH = 0.07 * K;            // the harbour patrol
  var CHOPPER = 0.37 * K;           // ~200 km/h cruising
  var CIRCLING = 0.25 * K;          // circling over something
  var AIRSHIP = 0.1 * K;            // ~55 km/h
  var APPROACH = 0.48 * K;          // ~260 km/h on final
  var BATWING = 1.3 * K;            // ~700 km/h, low and fast

  var subway = [], rails = [], ferries = [], lanes = [], runways = [], sails = [], taxi = null;
  var spots = {};

  /* --- lines to travel along ----------------------------------------------- */

  function line(coords) {
    var dist = [0];
    for (var i = 1; i < coords.length; i++) {
      dist.push(dist[i - 1] + Math.hypot(coords[i][0] - coords[i - 1][0], coords[i][1] - coords[i - 1][1]));
    }
    return { pts: coords, dist: dist, len: dist[dist.length - 1] || 0 };
  }

  function longest(geometry) {
    if (geometry.type === 'LineString') return geometry.coordinates;
    return geometry.coordinates.reduce(function (a, b) { return b.length > a.length ? b : a; }, []);
  }

  // Where a fraction u of the way along is, and the heading there (degrees from north).
  function at(r, u) {
    var d = Math.max(0, Math.min(1, u)) * r.len, lo = 1, hi = r.dist.length - 1;
    while (lo < hi) { var mid = (lo + hi) >> 1; if (r.dist[mid] < d) lo = mid + 1; else hi = mid; }
    var a = r.pts[lo - 1], b = r.pts[lo], seg = (r.dist[lo] - r.dist[lo - 1]) || 1, k = (d - r.dist[lo - 1]) / seg;
    return { p: [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k],
             r: Math.atan2(b[0] - a[0], b[1] - a[1]) * 180 / Math.PI };
  }

  /* --- timetables ------------------------------------------------------------ */

  // A run from one end to the other: moving between stops, easing in and out
  // of each, standing at each for `dwell` seconds.
  function timetable(len, stops, speed, dwell) {
    var marks = (stops || []).filter(function (u) { return u > 0.002 && u < 0.998; }).sort(function (a, b) { return a - b; });
    marks = [0].concat(marks, [1]);
    var seg = [], t = 0;
    for (var i = 0; i < marks.length; i++) {
      if (i > 0) {
        var dt = (marks[i] - marks[i - 1]) * len / speed;
        seg.push({ t0: t, t1: t + dt, u0: marks[i - 1], u1: marks[i] });
        t += dt;
      }
      if (dwell) { seg.push({ t0: t, t1: t + dwell, u0: marks[i], u1: marks[i] }); t += dwell; }
    }
    return { T: t, seg: seg };
  }

  function progress(tt, tau) {
    var seg = tt.seg, lo = 0, hi = seg.length - 1;
    while (lo < hi) { var mid = (lo + hi) >> 1; if (seg[mid].t1 < tau) lo = mid + 1; else hi = mid; }
    var s = seg[lo], f = s.t1 > s.t0 ? (tau - s.t0) / (s.t1 - s.t0) : 1;
    f = Math.max(0, Math.min(1, f));
    return { u: s.u0 + (s.u1 - s.u0) * f * f * (3 - 2 * f), standing: s.u0 === s.u1 };
  }

  // How much is running at a given hour: 0 the small hours, 1 late, 2 day, 3 the rush.
  function band(hour) {
    if (hour >= 1 && hour < 5) return 0;
    if (hour >= 22 || hour < 1 || hour < 6.5) return 1;
    if ((hour >= 7 && hour < 10) || (hour >= 16 && hour < 19)) return 3;
    return 2;
  }
  var bands = {};
  function bandAt(s) {
    var key = Math.floor(s / 600);
    if (!(key in bands)) {
      var d = new Date(key * 600000);
      bands[key] = band(d.getHours() + d.getMinutes() / 60);
    }
    return bands[key];
  }

  // Departures every `head` seconds; at quieter hours only every nth runs
  // (`every[band]`, 0 for none) — decided by when each one left, so a train
  // already out doesn't vanish when the hour turns.
  function departures(s, head, total, every, fn) {
    var first = Math.ceil((s - total) / head), last = Math.floor(s / head);
    for (var k = first; k <= last; k++) {
      var n = every[bandAt(k * head)];
      if (!n || ((k % n) + n) % n) continue;
      fn(s - k * head, k);
    }
  }

  function hash(k) { var x = Math.sin(k * 127.1 + 311.7) * 43758.5453; return x - Math.floor(x); }
  function point(p, props) { return { type: 'Feature', geometry: { type: 'Point', coordinates: p }, properties: props }; }
  function ll(x, y) { return [(x - 55) * K, (65 - y) * K]; }
  function towards(a, b, f) { return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f]; }
  function heading(a, b) { return Math.atan2(b[0] - a[0], b[1] - a[1]) * 180 / Math.PI; }
  function dist(a, b) { return Math.hypot(b[0] - a[0], b[1] - a[1]); }

  // A circuit through waypoints, flown at `speed` along a smooth curve through
  // them (Catmull-Rom) — banking round, not turning on a ruled corner.
  var curves = {};
  function smoothLoop(points) {
    var key = points.map(function (p) { return p[0].toFixed(5) + ',' + p[1].toFixed(5); }).join(';');
    if (curves[key]) return curves[key];
    var out = [], n = points.length;
    for (var i = 0; i < n; i++) {
      var p0 = points[(i - 1 + n) % n], p1 = points[i], p2 = points[(i + 1) % n], p3 = points[(i + 2) % n];
      for (var k = 0; k < 8; k++) {
        var t = k / 8, t2 = t * t, t3 = t2 * t;
        out.push([0.5 * (2 * p1[0] + (-p0[0] + p2[0]) * t + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2 + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3),
                  0.5 * (2 * p1[1] + (-p0[1] + p2[1]) * t + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2 + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)]);
      }
    }
    out.push(out[0]);
    curves[key] = line(out);
    return curves[key];
  }
  function circuit(points, speed, s, offset) {
    var loop = smoothLoop(points);
    var along = (((s + (offset || 0)) * speed) % loop.len + loop.len) % loop.len;
    return at(loop, along / loop.len);
  }

  /* --- what's on the map ------------------------------------------------------ */

  function init(fc, places) {
    subway = []; rails = []; ferries = []; lanes = []; runways = []; sails = []; spots = {}; taxi = null;
    fc.features.forEach(function (f) {
      var p = f.properties;
      if (p.l === 'subway') {
        var r = line(longest(f.geometry));
        r.n = p.n; r.col = p.col;
        r.tt = timetable(r.len, p.st, METRO, 25);
        subway.push(r);
      } else if (p.l === 'railroute') {
        var q = line(longest(f.geometry));
        q.n = p.n; q.tun = p.tun || []; q.st = p.st || [];
        q.tt = timetable(q.len, q.st.map(function (s) { return s[1]; }), TRAIN, 45);
        q.names = [q.st.length ? q.st[0][0] : '', q.st.length ? q.st[q.st.length - 1][0] : ''];
        rails.push(q);
      } else if (p.l === 'ferry') {
        var c = longest(f.geometry), fr = line(c);
        fr.n = p.n;
        fr.loop = Math.hypot(c[0][0] - c[c.length - 1][0], c[0][1] - c[c.length - 1][1]) < 2 * K;
        fr.tt = timetable(fr.len, p.st, FERRY, 300);
        ferries.push(fr);
        if (p.n === 'Harbor Water Taxi') taxi = fr;
      } else if (p.l === 'lane') {
        var ln = line(longest(f.geometry));
        ln.n = p.n;
        lanes.push(ln);
      } else if (p.l === 'runway_line') {
        runways.push(f.geometry.coordinates);
      } else if (p.l === 'sailing') {
        var loop = line(f.geometry.coordinates);
        loop.b = p.b || 1;
        sails.push(loop);
      }
    });
    (places || []).forEach(function (p) { spots[p.name] = ll(p.x, p.y); });
  }

  /* --- the frame ------------------------------------------------------------- */

  function trains(s, out) {
    subway.forEach(function (r, n) {
      [0, 1].forEach(function (back) {
        departures(s + n * 61 + back * 97, 180, r.tt.T, [5, 3, 2, 1], function (tau) {
          var pr = progress(r.tt, tau), u = back ? 1 - pr.u : pr.u, here = at(r, u);
          out.push(point(here.p, { m: 'metro', col: r.col, r: back ? here.r + 180 : here.r, n: r.n + ' · subway' }));
        });
      });
    });
    rails.forEach(function (r, n) {
      [0, 1].forEach(function (back) {
        departures(s + n * 233 + back * 311, 600, r.tt.T, [0, 3, 2, 1], function (tau) {
          var pr = progress(r.tt, tau), u = back ? 1 - pr.u : pr.u, here = at(r, u);
          var under = r.tun.some(function (span) { return u > span[0] && u < span[1]; });
          var to = r.names[back ? 0 : 1];
          out.push(point(here.p, { m: under ? 'metro' : 'train', col: '#e8f6ff', r: back ? here.r + 180 : here.r,
                                   n: r.n + (to ? ' · to ' + to : '') }));
        });
      });
    });
  }

  function boats(s, hour, out) {
    ferries.forEach(function (fr, n) {
      if (hour < 6 && fr.n !== 'Blackgate Ferry') return;            // the ferries sleep; the prison's doesn't
      if (fr.n === 'Harbor Water Taxi' && (hour < 8 || hour >= 22)) return;
      var T = fr.tt.T, cycle = fr.loop ? T : 2 * T, many = band(hour) === 3 && fr.len > 20 * K ? 2 : 1;
      for (var b = 0; b < many; b++) {
        var tau = ((s + n * 397 + b * cycle / many) % cycle + cycle) % cycle, back = !fr.loop && tau > T;
        var pr = progress(fr.tt, back ? tau - T : tau), u = back ? 1 - pr.u : pr.u, here = at(fr, u);
        out.push(point(here.p, { m: 'ferry', r: back ? here.r + 180 : here.r, n: fr.n }));
      }
    });
    lanes.forEach(function (ln, n) {
      // In from the sea, alongside for an hour, out again; then a gap before the next.
      var sail = ln.len / SHIP, dock = 3600, cycle = 2 * sail + dock + 2400;
      [0, cycle / 2].forEach(function (offset) {
        var tau = ((s + n * 1777 + offset) % cycle + cycle) % cycle, u, out_;
        if (tau < sail) { var f = tau / sail; u = f < 0.9 ? f / 0.9 * 0.93 : 0.93 + 0.07 * (1 - Math.pow(1 - (f - 0.9) / 0.1, 2)); out_ = false; }
        else if (tau < sail + dock) { u = 1; out_ = false; }
        else if (tau < 2 * sail + dock) { var g = (tau - sail - dock) / sail; u = 1 - (g < 0.1 ? 0.07 * g * g / 0.01 : 0.07 + (g - 0.1) / 0.9 * 0.93); out_ = true; }
        else return;
        var here = at(ln, Math.max(0, Math.min(1, u))), r = out_ ? here.r + 180 : here.r;
        out.push(point(here.p, { m: 'ship', r: r, n: 'Container ship · ' + (out_ ? 'outbound from ' : u >= 1 ? 'berthed at ' : 'bound for ') + ln.n }));
        if (u > 0.9) {
          var side = (r + 90) * Math.PI / 180, off = 0.25 * K;
          out.push(point([here.p[0] + Math.sin(side) * off, here.p[1] + Math.cos(side) * off], { m: 'tug', r: r, n: 'Tug' }));
        }
      });
    });
    if (taxi) {
      // The harbour launch on its rounds, never quite to a timetable.
      var lap = taxi.len / LAUNCH, tau = (s % (2 * lap) + 2 * lap) % (2 * lap), u = tau < lap ? tau / lap : 2 - tau / lap;
      var here = at(taxi, 0.04 + 0.92 * u);
      out.push(point(here.p, { m: 'patrol', r: tau < lap ? here.r : here.r + 180, n: 'GCPD Harbor Patrol' }));
    }
    // Sailboats all round the water: most out by day, a few staying for the
    // sunset, none in the dark — each on its own loop of open water (see the map
    // build), some sailing it one way and some the other, never in step.
    var out_sailing = hour >= 8 && hour < 19 ? 1 : hour === 7 || hour === 19 ? 0.5 : hour === 20 ? 0.25 : 0;
    sails.forEach(function (loop, i) {
      var boats = Math.round(loop.b * out_sailing + (i % 3 === 0 && out_sailing ? 0.4 : 0));
      for (var k = 0; k < boats; k++) {
        var lap = loop.len / (SAIL * (0.85 + ((i * 7 + k * 3) % 5) * 0.08));
        var u = ((s / lap + k / Math.max(1, loop.b) + i * 0.137) % 1 + 1) % 1;
        if (i % 2) u = 1 - u;                                   // round the other way
        var here = at(loop, u);
        out.push(point(here.p, { m: 'sail', r: i % 2 ? here.r + 180 : here.r, n: 'Sailboat' }));
      }
    });
  }

  function planes(s, hour, out) {
    if (runways.length < 2) return;
    var b = band(hour);
    if (!b) return;                                                 // no flights in the small hours
    var every = [0, 420, 200, 120][b];
    // Arrivals on the second runway, in from over the harbour; departures off the first, out to sea.
    var land = runways[1], dep = runways[0];
    var lux = land[1][0] - land[0][0], luy = land[1][1] - land[0][1], ln = Math.hypot(lux, luy);
    lux /= ln; luy /= ln;
    var dux = dep[1][0] - dep[0][0], duy = dep[1][1] - dep[0][1], dn = Math.hypot(dux, duy);
    dux /= dn; duy /= dn;
    function shadowed(p, alt, r, label) {
      if (alt > 0.02) out.push(point([p[0] + 0.8 * alt * K, p[1] - 0.8 * alt * K], { m: 'plane-shadow', r: r, s: alt }));
      out.push(point(p, { m: 'plane', r: r, s: alt, n: label }));
    }
    // An arrival: down the approach, touchdown, the roll-out, off onto a taxiway.
    var approach = 28 * K / APPROACH, rollout = 34;
    var a = ((s % every) + every) % every, flight = 100 + Math.floor(s / every) % 800;
    if (a < approach + rollout) {
      var start = [land[0][0] - lux * 28 * K, land[0][1] - luy * 28 * K], r = heading(land[0], land[1]);
      var touch = [land[0][0] + lux * 0.12 * ln, land[0][1] + luy * 0.12 * ln];
      if (a < approach) shadowed(towards(start, touch, a / approach), 1 - a / approach, r, 'Flight GA ' + flight + ' · arriving');
      else {
        var f = (a - approach) / rollout, u = 0.12 + 0.5 * (1 - Math.pow(1 - f, 2));
        shadowed([land[0][0] + lux * ln * u, land[0][1] + luy * ln * u], 0, r, 'Flight GA ' + flight + ' · landed');
      }
    }
    // A departure: lined up, the roll, rotation, the climb and the long turn out to sea.
    var d = (((s + every / 2) % every) + every) % every, out_ = 300 + Math.floor((s + every / 2) / every) % 600;
    if (d < 130) {
      var r2 = heading(dep[0], dep[1]);
      if (d < 20) shadowed([dep[0][0] + dux * dn * 0.04, dep[0][1] + duy * dn * 0.04], 0, r2, 'Flight GA ' + out_ + ' · holding');
      else if (d < 55) {
        var g = (d - 20) / 35, ug = 0.04 + 0.58 * g * g;
        shadowed([dep[0][0] + dux * dn * ug, dep[0][1] + duy * dn * ug], 0, r2, 'Flight GA ' + out_ + ' · departing');
      } else {
        var c = (d - 55) / 75, lift = [dep[0][0] + dux * dn * 0.62, dep[0][1] + duy * dn * 0.62];
        var turn = Math.max(0, c - 0.35) / 0.65, base = Math.atan2(dux, duy);
        var far = c * 75 * 0.4 * K;
        var p = [lift[0] + Math.sin(base + turn * 0.65) * far, lift[1] + Math.cos(base + turn * 0.65) * far];
        shadowed(p, Math.min(1, c * 1.4), (base + turn * 1.3) * 180 / Math.PI, 'Flight GA ' + out_ + ' · climbing out');
      }
    }
  }

  function choppers(s, hour, reports, out) {
    var open = (reports || []).filter(function (r) { return r.status !== 'resolved'; })
      .sort(function (a, b) { return b.severity - a.severity || b.at - a.at; });
    var night = hour >= 20 || hour < 5;
    function orbit(c, rad, phase) {
      var a = s * (CIRCLING / rad) + phase;
      return { p: [c[0] + Math.cos(a) * rad, c[1] + Math.sin(a) * rad], r: -a * 180 / Math.PI };
    }
    function light(p, c) { out.push(point(towards(c || p, p, 0.3), { m: 'light' })); }
    // GCPD Air: over the worst thing on the scanner; at night, a patrol over the rough districts.
    var worst = open[0] && open[0].severity >= 3 ? ll(open[0].x, open[0].y) : null;
    if (worst) {
      var o = orbit(worst, 0.0011, 0);
      light(o.p, worst);
      out.push(point(o.p, { m: 'heli', r: o.r, n: 'GCPD Air Support · over ' + open[0].kind.toLowerCase() }));
    } else if (night) {
      var beat = ['Crime Alley', 'The Bowery', 'Burnley', 'The Narrows', 'Tricorner', 'Chinatown', 'Old Gotham']
        .map(function (n) { return spots[n]; }).filter(Boolean);
      if (beat.length > 2) {
        var pat = circuit(beat, CHOPPER * 0.6, s, 0);
        out.push(point(towards(pat.p, pat.p, 0), { m: 'light' }));
        out.push(point(pat.p, { m: 'heli', r: pat.r, n: 'GCPD Air Support · patrol' }));
      }
    }
    // GBC News: the next story along, or the traffic at the rush, or the game.
    if (hour >= 6 && hour < 23) {
      var story = open[1] && open[1].severity >= 2 ? ll(open[1].x, open[1].y) : null;
      if (story) { var nw = orbit(story, 0.0017, 2); out.push(point(nw.p, { m: 'heli-news', r: nw.r, n: 'GBC News · on the story' })); }
      else if (band(hour) === 3) {
        var roads = ['Union Station', 'Gotham Harbor Bridge', 'Pioneers Bridge', 'Robert Kane Memorial Bridge', 'Knightsdome']
          .map(function (n) { return spots[n]; }).filter(Boolean);
        if (roads.length > 2) { var tw = circuit(roads, CHOPPER * 0.5, s, 900); out.push(point(tw.p, { m: 'heli-news', r: tw.r, n: 'GBC News · traffic watch' })); }
      } else if (hour >= 18 && hour < 22 && spots['Knightsdome']) {
        var g = orbit(spots['Knightsdome'], 0.0017, 1); out.push(point(g.p, { m: 'heli-news', r: g.r, n: 'GBC News · at the game' }));
      }
    }
    // Medevac: in from wherever, down on a hospital's roof for a while, home again.
    var hospitals = [spots['Gotham General Hospital'], spots['Mercy Hospital'], spots['Elliot Memorial Hospital']].filter(Boolean);
    var from = [spots['The Narrows'], spots['Burnley'], spots['Tricorner'], spots['Ironworks'], spots['Burnside'],
                spots['Coventry'], spots['Kane Heights'], spots['Bristol']].filter(Boolean);
    if (hospitals.length && from.length) {
      var cyc = 1500, k = Math.floor(s / cyc), m = s - k * cyc;
      var to = hospitals[k % hospitals.length], src = from[Math.floor(hash(k) * from.length)];
      var fly = dist(src, to) / CHOPPER;
      if (m < fly) out.push(point(towards(src, to, m / fly), { m: 'heli-med', r: heading(src, to), n: 'Medevac · inbound' }));
      else if (m < fly + 420) out.push(point(to, { m: 'heli-med', r: heading(src, to), n: 'Medevac · on the pad' }));
    }
    // A tour round the Statue of Justice and up the harbour, by day.
    if (hour >= 10 && hour < 18 && spots['Statue of Justice']) {
      var tour = orbit(spots['Statue of Justice'], 2.2 * K, 1);
      out.push(point(tour.p, { m: 'heli-civ', r: tour.r - 90, n: 'Harbour tour' }));
    }
    // Wayne Enterprises: the tower roof to the hangar and back.
    if (hour >= 8 && hour < 20 && spots['Wayne Tower'] && spots['Hangar 9']) {
      var leg = 1800, w = ((s % leg) + leg) % leg, outbound = Math.floor(s / leg) % 2 === 0;
      var A = outbound ? spots['Wayne Tower'] : spots['Hangar 9'], B = outbound ? spots['Hangar 9'] : spots['Wayne Tower'];
      var hop = dist(A, B) / CHOPPER;
      if (w < hop) out.push(point(towards(A, B, w / hop), { m: 'heli-civ', r: heading(A, B), n: 'Wayne Enterprises' }));
    }
  }

  // Stagg's airship: a slow circuit of the city as an advertisement, then an hour at its mast.
  function airship(s, out) {
    var mast = spots['Stagg Enterprises'];
    if (!mast) return;
    var route = [mast, spots['New Town'], spots['Upper East Side'], spots['Fashion District'], spots['Diamond District'],
                 spots['Old Gotham'], spots['Chinatown'], spots['Coventry'], spots['Robinson Park']].filter(Boolean);
    var flying = smoothLoop(route).len / AIRSHIP, cycle = flying + 3600, tau = ((s % cycle) + cycle) % cycle;
    var here = tau < flying ? circuit(route, AIRSHIP, tau, 0) : { p: mast, r: 30 };
    out.push(point([here.p[0] + 1.4 * K, here.p[1] - 1.4 * K], { m: 'airship-shadow', r: here.r }));
    out.push(point(here.p, { m: 'airship', r: here.r, n: 'Stagg Enterprises airship' + (tau < flying ? '' : ' · moored') }));
  }

  // The Batwing: Bruce's jet, out of the Manor's hangar on a few low passes a
  // night — over three or four districts and home, fast and dark, gone before
  // anyone's sure what they saw. Not every night has one; none come by day.
  var OVERFLY = ['Diamond District', 'Old Gotham', 'The Narrows', 'Crime Alley', 'Burnside', 'Otisburg', 'Amusement Mile',
                 'Upper East Side', 'Tricorner', 'Robinson Park', 'Chinatown', 'Coventry', 'New Town'];
  // Once the console says where the jet is — on a job, waiting where it set someone down,
  // on a pass, in the hangar — that's where it's drawn: the one Batwing, flying what it was asked to.
  var JET = null;
  function setJet(state) { JET = state; }
  function realJet(s, out) {
    var leg = (JET.legs || []).filter(function (l) { return l.start <= s && s < l.end; })[0], p, r;
    if (leg) {
      var a = ll(leg.pts[0][0], leg.pts[0][1]), b = ll(leg.pts[1][0], leg.pts[1][1]);
      p = towards(a, b, (s - leg.start) / Math.max(1, leg.end - leg.start));
      r = heading(a, b);
    } else if (!JET.parked) {
      p = ll(JET.x, JET.y); r = JET.r || 0;
    } else {
      return;                                   // in the hangar under the Manor
    }
    var job = leg ? { pickup: ' · coming down for a pickup', carry: ' · carrying ' + (leg.riders || []).length,
                      home: ' · heading home', pass: '' }[leg.kind] || '' : ' · waiting';
    out.push(point([p[0] + 0.8 * K, p[1] - 0.8 * K], { m: 'batwing-shadow', r: r }));
    out.push(point(p, { m: 'batwing', r: r, n: 'The Batwing' + job }));
  }
  function batwing(s, hour, out) {
    if (JET) { realJet(s, out); return; }
    var manor = spots['Wayne Manor'];
    if (!manor || !(hour >= 21 || hour < 4)) return;
    var slot = 2700, k = Math.floor(s / slot), tau = s - k * slot - hash(k * 3 + 1) * 900;
    if (hash(k) > 0.5 || tau < 0) return;
    var stops = OVERFLY.filter(function (n) { return spots[n]; });
    var route = [manor];
    for (var i = 0; i < 3 + Math.floor(hash(k * 7 + 2) * 2); i++) {
      var pick = stops[Math.floor(hash(k * 11 + i * 5) * stops.length)];
      if (route.indexOf(spots[pick]) === -1) route.push(spots[pick]);
    }
    var flying = smoothLoop(route).len / BATWING;
    if (tau > flying) return;
    var here = circuit(route, BATWING, tau, 0);
    out.push(point([here.p[0] + 0.8 * K, here.p[1] - 0.8 * K], { m: 'batwing-shadow', r: here.r }));
    out.push(point(here.p, { m: 'batwing', r: here.r, n: 'The Batwing' }));
  }

  // The lighthouse at Cape Carmine, its beam turning over the water at night.
  function beam(s, hour, out) {
    var lh = spots['Cape Carmine Lighthouse'];
    if (!lh || !(hour >= 19 || hour < 6)) return;
    var a = (s / 12) * 2 * Math.PI, len = 5.5 * K, spread = 0.09;
    [[1, spread], [0.7, spread * 2.2]].forEach(function (w, i) {
      var ring = [lh];
      for (var k = -4; k <= 4; k++) {
        var t = a + (k / 4) * w[1];
        ring.push([lh[0] + Math.sin(t) * len * w[0], lh[1] + Math.cos(t) * len * w[0]]);
      }
      ring.push(lh);
      out.push({ type: 'Feature', geometry: { type: 'Polygon', coordinates: [ring] }, properties: { m: 'beam', o: i ? 0.06 : 0.14 } });
    });
  }

  function frame(nowMs, reports) {
    var s = nowMs / 1000, d = new Date(nowMs), hour = d.getHours() + d.getMinutes() / 60, out = [];
    trains(s, out);
    boats(s, hour, out);
    planes(s, hour, out);
    choppers(s, hour, reports, out);
    airship(s, out);
    batwing(s, hour, out);
    beam(s, hour, out);
    return out;
  }

  window.GothamTraffic = { init: init, frame: frame, band: band, jet: setJet };
})();
