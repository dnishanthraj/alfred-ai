/* ==========================================================================
   Audio playback.

   Clips arrive one sentence at a time and must play gapless and in order, so
   they queue here rather than firing as they land. Everything routes through a
   single AnalyserNode, which is what lets the visualizer read the real
   spectrum of the voice instead of animating a guess.

   `onSentenceStart` fires the moment a clip begins, with its duration — that
   is the hook the transcript uses to reveal words in time with the speech
   rather than dumping the whole reply on screen before it is spoken.
   ========================================================================== */

(function (global) {
  'use strict';

  var ctx = null;
  var analyser = null;
  var queue = [];
  var current = null;
  var playing = false;
  // Bumped by stop(). A clip still loading when he was cut off belongs to the
  // old generation and must never start once it lands.
  var generation = 0;

  var handlers = {
    onSentenceStart: null,   // (text, key, durationMs, words, speaker)
    onIdle: null,
    onBusy: null
  };

  function context() {
    if (ctx) return ctx;
    var Ctx = global.AudioContext || global.webkitAudioContext;
    ctx = new Ctx();
    analyser = ctx.createAnalyser();
    analyser.fftSize = 2048;
    analyser.smoothingTimeConstant = 0.75;
    analyser.connect(ctx.destination);
    return ctx;
  }

  function resume() {
    var c = context();
    return c.state === 'suspended' ? c.resume() : Promise.resolve();
  }

  /* Fetched and decoded the moment it is queued, not when its turn comes.
     Waiting until the clip before it had ended put a network round trip and a
     decode into the gap between every pair of sentences. */
  function load(clipId) {
    return fetch('/api/audio/' + clipId)
      .then(function (r) { return r.arrayBuffer(); })
      .then(function (buf) { return context().decodeAudioData(buf); });
  }

  /* Someone in the room with whoever he called, heard through their phone on
     speaker: the phone's band, a little of the room, quieter than the one
     holding it. */
  var distant = {}, room = null;
  function impulse(c, seconds) {
    var n = Math.floor(c.sampleRate * seconds), buf = c.createBuffer(2, n, c.sampleRate);
    for (var ch = 0; ch < 2; ch++) {
      var d = buf.getChannelData(ch);
      for (var i = 0; i < n; i++) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / n, 3);
    }
    return buf;
  }
  function roomChain() {
    if (room) return room;
    var c = context();
    var input = c.createGain();
    var low = c.createBiquadFilter(); low.type = 'highpass'; low.frequency.value = 280;
    var high = c.createBiquadFilter(); high.type = 'lowpass'; high.frequency.value = 2700;
    var dry = c.createGain(); dry.gain.value = 0.5;
    var wet = c.createGain(); wet.gain.value = 0.24;
    var verb = c.createConvolver(); verb.buffer = impulse(c, 0.45);
    input.connect(low); low.connect(high); high.connect(dry); high.connect(verb); verb.connect(wet);
    dry.connect(analyser); wet.connect(analyser);
    room = input;
    return room;
  }
  function setDistant(ids) {
    distant = {};
    (ids || []).forEach(function (id) { distant[id] = true; });
  }

  function enqueue(clipId, text, key, words, speaker) {
    var decoded = load(clipId);
    decoded.catch(function () { /* handled when its turn comes */ });
    queue.push({ decoded: decoded, text: text, key: key, words: words || [],
                 speaker: speaker, generation: generation });
    if (!playing) next();
  }

  function next() {
    if (!queue.length) {
      playing = false;
      current = null;
      if (handlers.onIdle) handlers.onIdle();
      return;
    }
    if (!playing && handlers.onBusy) handlers.onBusy();
    playing = true;

    var item = queue.shift();

    item.decoded
      .then(function (decoded) {
        if (item.generation !== generation) return;   // stopped while loading
        var source = ctx.createBufferSource();
        source.buffer = decoded;
        source.connect(distant[item.speaker] ? roomChain() : analyser);
        source.onended = function () {
          if (current === source) { current = null; next(); }
        };
        current = source;
        if (handlers.onSentenceStart) {
          handlers.onSentenceStart(item.text, item.key, decoded.duration * 1000, item.words,
                                   item.speaker);
        }
        source.start();
      })
      .catch(function () {
        // A clip that won't decode shouldn't strand the rest of the reply.
        if (item.generation === generation) next();
      });
  }

  function stop() {
    generation += 1;
    queue.length = 0;
    if (current) {
      var source = current;
      current = null;
      try { source.onended = null; source.stop(); } catch (e) { /* already ended */ }
    }
    playing = false;
    if (handlers.onIdle) handlers.onIdle();
  }

  /* --- the room behind the voice --------------------------------------------
     A loop of where they are, low under the call: straight to the speakers, not
     through the analyser, so the instrument only ever shows the voice. Each
     bed fades in and out rather than switching, and rises a little while its
     speaker talks — on a call with company only then, as a phone's noise gate
     lets a room through only with a voice. */
  var beds = {};
  var BED_QUIET = 0.05, BED_TALKING = 0.11;

  // A one-off sound over a bed — a siren passing, a punch landing — now and
  // then, at random, through the bed's own gain so it's heard as theirs.
  var shotBuffers = {};
  function shotBuffer(name) {
    if (!shotBuffers[name]) {
      shotBuffers[name] = fetch('/api/ambience/file/' + encodeURIComponent(name))
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.arrayBuffer(); })
        .then(function (buf) { return context().decodeAudioData(buf); });
      shotBuffers[name].catch(function () { delete shotBuffers[name]; });
    }
    return shotBuffers[name];
  }
  function scheduleShots(id, entry, shots) {
    (shots || []).forEach(function (shot) {
      function next() {
        // Exponential gaps: sometimes two close together, sometimes a long quiet.
        var wait = -Math.log(1 - Math.random()) * shot.every * 1000;
        entry.timers.push(setTimeout(function () {
          if (beds[id] !== entry) return;
          shotBuffer(shot.name).then(function (decoded) {
            if (beds[id] !== entry) return;
            var source = context().createBufferSource(), lift = context().createGain();
            lift.gain.value = 1.6;          // a shade above the room it's in
            source.buffer = decoded;
            source.connect(lift).connect(entry.gain);
            source.start();
          }).catch(function () {});
          next();
        }, wait));
      }
      next();
    });
  }

  function bed(id, url, gated, shots) {
    var c = context(), current = beds[id];
    if (current && current.url === url) { current.gated = gated; level(id, false); return; }
    unbed(id);
    var entry = beds[id] = { url: url, gated: gated, gain: c.createGain(), source: null, timers: [] };
    scheduleShots(id, entry, shots);
    entry.gain.gain.value = 0;
    entry.gain.connect(c.destination);
    fetch(url).then(function (r) { if (!r.ok) throw new Error(r.status); return r.arrayBuffer(); })
      .then(function (buf) { return c.decodeAudioData(buf); })
      .then(function (decoded) {
        if (beds[id] !== entry) return;           // replaced or gone while it loaded
        var source = c.createBufferSource();
        source.buffer = decoded;
        source.loop = true;
        source.connect(entry.gain);
        source.start();
        entry.source = source;
        level(id, false);
      }).catch(function () { /* no room to hear: the call goes on without it */ });
  }

  function level(id, talking) {
    var entry = beds[id];
    if (!entry) return;
    var to = talking ? BED_TALKING : (entry.gated ? 0 : BED_QUIET);
    var now = context().currentTime;
    entry.gain.gain.cancelScheduledValues(now);
    entry.gain.gain.setTargetAtTime(to, now, talking ? 0.25 : 0.9);
  }

  function unbed(id) {
    var entry = beds[id];
    if (!entry) return;
    delete beds[id];
    (entry.timers || []).forEach(clearTimeout);
    var now = context().currentTime;
    entry.gain.gain.cancelScheduledValues(now);
    entry.gain.gain.setTargetAtTime(0, now, 0.6);
    setTimeout(function () {
      try { if (entry.source) entry.source.stop(); } catch (e) { /* already stopped */ }
      entry.gain.disconnect();
    }, 2500);
  }

  global.ConsoleAudio = {
    setDistant: setDistant,
    context: context,
    resume: resume,
    enqueue: enqueue,
    stop: stop,
    bed: bed,
    unbed: unbed,
    bedLevel: level,
    beds: function () { return Object.keys(beds); },
    on: function (name, fn) { handlers[name] = fn; },
    get analyser() { return analyser; },
    get isPlaying() { return playing; }
  };
})(window);
