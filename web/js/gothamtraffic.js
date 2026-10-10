/* ==========================================================================
   Gotham's traffic: what moves on the map, and when.

   The subway under the streets and the trains on their lines, stopping at
   every station; ferries between their piers; ships in from the sea and the
   tugs that bring them alongside; planes landing and taking off; the police,
   news, medevac, tour and company helicopters; sailboats off the marina; the
   harbour launch; the lighthouse at night.

   Everything here is worked out from the clock, not stored: a train is where
   its timetable puts it, so every frame agrees with the last, and a page
   opened at the same moment sees the same city. The timetable follows the
   hour — trains every few minutes at the rush, a handful in the small hours,
   no flights after one in the morning.

   Map time runs fast — a crossing that takes a quarter of an hour takes a
   minute here — so the city looks busy at a glance, as it is.
   ========================================================================== */
(function () {
  'use strict';

  var K = 0.0013475;                // degrees in a layout unit (~150 m), as in gothammap.js
  // Speeds in layout units a second of map time: the subway end to end in a
  // minute and a half, a ferry crossing in under a minute, the trains quicker.
  var METRO = 0.75 * K, TRAIN = 1.15 * K, FERRY = 0.6 * K;
  var subway = [], rails = [], ferries = [], lanes = [], runways = [], taxi = null;
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
  // of each, standing at each for `dwell` seconds. Times in map seconds.
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

  // Departures every `head` seconds of map time; at quieter hours only every
  // nth runs (`every[band]`, 0 for none) — decided by when each one left, so a
  // train already out doesn't vanish when the hour turns.
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

  /* --- what's on the map ------------------------------------------------------ */

  function init(fc, places) {
    subway = []; rails = []; ferries = []; lanes = []; runways = []; spots = {};
    fc.features.forEach(function (f) {
      var p = f.properties;
      if (p.l === 'subway') {
        var r = line(longest(f.geometry));
        r.n = p.n; r.col = p.col;
        r.tt = timetable(r.len, p.st, METRO, 2.2);
        subway.push(r);
      } else if (p.l === 'railroute') {
        var q = line(longest(f.geometry));
        q.n = p.n; q.tun = p.tun || []; q.st = p.st || [];
        q.tt = timetable(q.len, q.st.map(function (s) { return s[1]; }), TRAIN, 3);
        q.names = [q.st.length ? q.st[0][0] : '', q.st.length ? q.st[q.st.length - 1][0] : ''];
        rails.push(q);
      } else if (p.l === 'ferry') {
        var c = longest(f.geometry), fr = line(c);
        fr.n = p.n;
        fr.loop = Math.hypot(c[0][0] - c[c.length - 1][0], c[0][1] - c[c.length - 1][1]) < 2 * K;
        fr.tt = timetable(fr.len, p.st, FERRY, 5);
        ferries.push(fr);
        if (p.n === 'Harbor Water Taxi') taxi = fr;
      } else if (p.l === 'lane') {
        var ln = line(longest(f.geometry));
        ln.n = p.n;
        lanes.push(ln);
      } else if (p.l === 'runway_line') {
        runways.push(f.geometry.coordinates);
      }
    });
    (places || []).forEach(function (p) { spots[p.name] = ll(p.x, p.y); });
  }

  /* --- the frame ------------------------------------------------------------- */

  function trains(s, out) {
    subway.forEach(function (r, n) {
      var T = r.tt.T;
      [0, 1].forEach(function (back) {
        departures(s + n * 7.3 + back * 11.1, 22, T, [5, 3, 2, 1], function (tau) {
          var pr = progress(r.tt, tau), u = back ? 1 - pr.u : pr.u, here = at(r, u);
          out.push(point(here.p, { m: 'metro', col: r.col, r: back ? here.r + 180 : here.r, n: r.n + ' · subway' }));
        });
      });
    });
    rails.forEach(function (r, n) {
      var T = r.tt.T;
      [0, 1].forEach(function (back) {
        departures(s + n * 13.7 + back * 19.3, 40, T, [0, 3, 2, 1], function (tau) {
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
    var day = hour >= 6 && hour < 24;
    ferries.forEach(function (fr, n) {
      if (!day && fr.n !== 'Blackgate Ferry') return;
      if (fr.n === 'Harbor Water Taxi' && (hour < 8 || hour >= 22)) return;
      var T = fr.tt.T, cycle = fr.loop ? T : 2 * T, boatsOn = band(hour) === 3 ? 2 : 1;
      for (var b = 0; b < boatsOn; b++) {
        var tau = ((s + n * 37 + b * cycle / boatsOn) % cycle + cycle) % cycle, back = !fr.loop && tau > T;
        var pr = progress(fr.tt, back ? tau - T : tau), u = back ? 1 - pr.u : pr.u, here = at(fr, u);
        out.push(point(here.p, { m: 'ferry', r: back ? here.r + 180 : here.r, n: fr.n }));
      }
    });
    lanes.forEach(function (ln, n) {
      var cycle = 600;
      [0, 300].forEach(function (offset) {
        var tau = ((s + n * 97 + offset) % cycle + cycle) % cycle, u, dir, phase;
        if (tau < 220) { phase = tau / 220; u = 1 - Math.pow(1 - phase, 2); dir = 0; }            // in, slowing
        else if (tau < 340) { u = 1; dir = 0; }                                                       // alongside
        else if (tau < 560) { phase = (tau - 340) / 220; u = 1 - phase * phase; dir = 1; }            // out, gathering way
        else return;                                                                                   // over the horizon
        var here = at(ln, u), r = dir ? here.r + 180 : here.r;
        out.push(point(here.p, { m: 'ship', r: r, n: 'Container ship · ' + (dir ? 'outbound from ' : 'bound for ') + ln.n }));
        if (u > 0.82) {
          var side = (r + 90) * Math.PI / 180, off = 0.22 * K;
          out.push(point([here.p[0] + Math.sin(side) * off, here.p[1] + Math.cos(side) * off],
                         { m: 'tug', r: r, n: 'Tug' }));
        }
      });
    });
    if (taxi) {
      // The harbour launch on its rounds, never quite to a timetable.
      var lap = 260, tau = (s % (2 * lap) + 2 * lap) % (2 * lap), u = tau < lap ? tau / lap : 2 - tau / lap;
      var here = at(taxi, 0.06 + 0.88 * u);
      out.push(point(here.p, { m: 'patrol', r: tau < lap ? here.r : here.r + 180, n: 'GCPD Harbor Patrol' }));
    }
    if (hour >= 8 && hour < 19) {
      // Sailboats off the marina, tacking about in the river.
      [[12.2, 96.5, 1.1], [12.0, 102.2, 1.3], [11.6, 107.8, 1.0], [13.0, 99.4, 0.8], [12.4, 105.0, 0.9]].forEach(function (c, i) {
        var period = 90 + i * 17, a = (s / period) * 2 * Math.PI + i * 1.7;
        var p = ll(c[0] + Math.cos(a) * c[2] * 0.5, c[1] + Math.sin(a) * c[2]);
        var r = heading(p, ll(c[0] + Math.cos(a + 0.05) * c[2] * 0.5, c[1] + Math.sin(a + 0.05) * c[2]));
        out.push(point(p, { m: 'sail', r: r, n: 'Sailboat' }));
      });
    }
  }

  function planes(s, hour, out) {
    if (runways.length < 2) return;
    var b = band(hour);
    if (!b) return;                                                 // no flights in the small hours
    var every = [0, 130, 70, 45][b];
    // Arrivals on the second runway, in from over the harbour; departures off the first, out to sea.
    var land = runways[1], dep = runways[0];
    var lux = (land[1][0] - land[0][0]), luy = (land[1][1] - land[0][1]);
    var ln = Math.hypot(lux, luy); lux /= ln; luy /= ln;
    var dux = (dep[1][0] - dep[0][0]), duy = (dep[1][1] - dep[0][1]);
    var dn = Math.hypot(dux, duy); dux /= dn; duy /= dn;
    function shadowed(p, alt, r, label) {
      if (alt > 0.02) out.push(point([p[0] + 0.8 * alt * K, p[1] - 0.8 * alt * K], { m: 'plane-shadow', r: r, s: alt }));
      out.push(point(p, { m: 'plane', r: r, s: alt, n: label }));
    }
    // An arrival: down the approach, touchdown, the roll-out, off onto a taxiway.
    var a = ((s % every) + every) % every, flight = 100 + Math.floor(s / every) % 800;
    if (a < 30) {
      var start = [land[0][0] - lux * 28 * K, land[0][1] - luy * 28 * K], r = heading(land[0], land[1]);
      if (a < 18) shadowed(towards(start, [land[0][0] + lux * 0.12 * ln, land[0][1] + luy * 0.12 * ln], a / 18), 1 - a / 18, r,
                           'Flight GA ' + flight + ' · arriving');
      else if (a < 27) {
        var f = (a - 18) / 9;
        shadowed([land[0][0] + lux * ln * (0.12 + 0.5 * (1 - Math.pow(1 - f, 2))), land[0][1] + luy * ln * (0.12 + 0.5 * (1 - Math.pow(1 - f, 2)))],
                 0, r, 'Flight GA ' + flight + ' · landed');
      }
    }
    // A departure: lined up, the roll, rotation, the climb and the turn out to sea.
    var d = (((s + every / 2) % every) + every) % every, out_ = 300 + Math.floor((s + every / 2) / every) % 600;
    if (d < 32) {
      var r2 = heading(dep[0], dep[1]);
      if (d < 3) shadowed([dep[0][0] + dux * dn * 0.04, dep[0][1] + duy * dn * 0.04], 0, r2, 'Flight GA ' + out_ + ' · holding');
      else if (d < 12) {
        var g = (d - 3) / 9, u = 0.04 + 0.58 * g * g;
        shadowed([dep[0][0] + dux * dn * u, dep[0][1] + duy * dn * u], 0, r2, 'Flight GA ' + out_ + ' · departing');
      } else {
        var c = (d - 12) / 20, lift = [dep[0][0] + dux * dn * 0.62, dep[0][1] + duy * dn * 0.62];
        // Straight out, then a long right turn to the south.
        var turn = Math.max(0, c - 0.35) / 0.65, ang = Math.atan2(dux, duy) + turn * 1.3;
        var dist = c * 34 * K;
        var p = [lift[0] + Math.sin(Math.atan2(dux, duy) + turn * 0.65) * dist, lift[1] + Math.cos(Math.atan2(dux, duy) + turn * 0.65) * dist];
        shadowed(p, Math.min(1, c * 1.4), ang * 180 / Math.PI, 'Flight GA ' + out_ + ' · climbing out');
      }
    }
  }

  function choppers(s, hour, reports, out) {
    var open = (reports || []).filter(function (r) { return r.status !== 'resolved'; })
      .sort(function (a, b) { return b.severity - a.severity || b.at - a.at; });
    var night = hour >= 20 || hour < 5;
    function orbit(c, rad, speed, phase) {
      var a = s * speed + phase;
      return { p: [c[0] + Math.cos(a) * rad, c[1] + Math.sin(a) * rad], r: -a * 180 / Math.PI };
    }
    // GCPD Air: over the worst thing on the scanner — or, at night, over Crime Alley anyway.
    var worst = open[0] && open[0].severity >= 3 ? ll(open[0].x, open[0].y) : (night ? spots['Crime Alley'] : null);
    if (worst) {
      var o = orbit(worst, 0.0011, 0.45, 0);
      out.push(point([worst[0] + (o.p[0] - worst[0]) * 0.3, worst[1] + (o.p[1] - worst[1]) * 0.3], { m: 'light' }));
      out.push(point(o.p, { m: 'heli', r: o.r, n: 'GCPD Air Support' }));
    }
    // GBC News: the next story along, or the game at the Knightsdome.
    if (hour >= 7 && hour < 23) {
      var story = open[1] && open[1].severity >= 2 ? ll(open[1].x, open[1].y)
        : (hour >= 18 && hour < 22 ? spots['Knightsdome'] : null);
      if (story) { var nw = orbit(story, 0.0017, 0.3, 2); out.push(point(nw.p, { m: 'heli-news', r: nw.r, n: 'GBC News chopper' })); }
    }
    // Medevac: in from wherever, down on a hospital's roof, gone again.
    var hospitals = [spots['Gotham General Hospital'], spots['Mercy Hospital']].filter(Boolean);
    var from = [spots['The Narrows'], spots['Burnley'], spots['Tricorner'], spots['Ironworks'], spots['Burnside'], spots['Coventry']].filter(Boolean);
    if (hospitals.length && from.length) {
      var cyc = 150, k = Math.floor(s / cyc), m = s - k * cyc;
      var to = hospitals[k % hospitals.length], src = from[Math.floor(hash(k) * from.length)];
      if (m < 40) out.push(point(towards(src, to, m / 40), { m: 'heli-med', r: heading(src, to), n: 'Medevac · inbound' }));
      else if (m < 56) out.push(point(to, { m: 'heli-med', r: heading(src, to), n: 'Medevac · on the pad' }));
    }
    // A tour round the Statue of Justice, by day.
    if (hour >= 10 && hour < 18 && spots['Statue of Justice']) {
      var tour = orbit(spots['Statue of Justice'], 2.2 * K, 0.1, 1);
      out.push(point(tour.p, { m: 'heli-civ', r: tour.r - 90, n: 'Harbour tour' }));
    }
    // Wayne Enterprises: the tower roof to the airport and back.
    if (hour >= 8 && hour < 20 && spots['Wayne Tower'] && spots['Hangar 9']) {
      var leg = 320, w = ((s % leg) + leg) % leg, outbound = Math.floor(s / leg) % 2 === 0;
      var A = outbound ? spots['Wayne Tower'] : spots['Hangar 9'], B = outbound ? spots['Hangar 9'] : spots['Wayne Tower'];
      if (w < 46) out.push(point(towards(A, B, w / 46), { m: 'heli-civ', r: heading(A, B), n: 'Wayne Enterprises' }));
    }
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
    beam(s, hour, out);
    return out;
  }

  window.GothamTraffic = { init: init, frame: frame, band: band };
})();
