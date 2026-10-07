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
    onSentenceStart: null,   // (text, index, durationMs)
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

  function enqueue(clipId, text, index) {
    var decoded = load(clipId);
    decoded.catch(function () { /* handled when its turn comes */ });
    queue.push({ decoded: decoded, text: text, index: index, generation: generation });
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
        source.connect(analyser);
        source.onended = function () {
          if (current === source) { current = null; next(); }
        };
        current = source;
        if (handlers.onSentenceStart) {
          handlers.onSentenceStart(item.text, item.index, decoded.duration * 1000);
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

  global.ConsoleAudio = {
    context: context,
    resume: resume,
    enqueue: enqueue,
    stop: stop,
    on: function (name, fn) { handlers[name] = fn; },
    get analyser() { return analyser; },
    get isPlaying() { return playing; }
  };
})(window);
