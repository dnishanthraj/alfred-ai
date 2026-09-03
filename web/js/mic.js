/* ==========================================================================
   Microphone capture, in two modes.

   Push       — hold a key or button, speak, release. Unambiguous, and the only
                thing that works reliably in a noisy room.
   Ambient    — the channel stays open. A voice-activity detector decides when
                an utterance starts and ends, and speaking over a reply cuts it
                off, the way interrupting a person does.

   Ambient mode has two problems that decide its design:

     Echo — the contact's voice leaves the speakers and comes back in through
     the mic. `echoCancellation` is what makes ambient mode viable at all; the
     browser's AEC references the system output and subtracts it. It is not
     perfect, so the VAD threshold is also raised while audio is playing.

     Clipping — by the time energy crosses the threshold, the first syllable
     has already happened. A rolling pre-roll buffer is retained at all times
     and prepended to every utterance, so takes start slightly *before* the
     speech does.
   ========================================================================== */

(function (global) {
  'use strict';

  var TARGET_RATE = 16000;
  var MIN_TAKE_MS = 250;

  // Frames of ~128 samples arrive continuously; these are in frames, resolved
  // against the real sample rate at capture time.
  var PREROLL_MS = 320;
  var SILENCE_HANGOVER_MS = 1100;  // silence before an utterance is considered over
  var MIN_SPEECH_MS = 320;         // shorter than this is a cough, not a sentence
  var MAX_UTTERANCE_MS = 30000;

  // The noise floor tracks the room asymmetrically: it drops to a quieter
  // floor readily, but rises very slowly. A symmetric tracker fast enough to
  // follow the room is also fast enough to follow *speech* — at ~375 frames a
  // second it climbed to meet each utterance within the onset window, so the
  // threshold outran the voice and the detector never fired at all.
  var NOISE_ADAPT_DOWN = 0.02;     // ~0.13s to settle onto a quieter room
  var NOISE_ADAPT_UP = 0.0008;     // ~3s to accept a louder one
  var SPEECH_FACTOR = 2.6;         // energy above floor that counts as speech
  var SPEECH_FACTOR_DUCKED = 5.0;  // stricter while the contact is talking
  var ABSOLUTE_FLOOR = 0.0025;
  var ONSET_MS = 80;               // sustained energy before a take opens
  // How far below the opening threshold the room must fall before a take is
  // considered over. Deliberately forgiving: ending early truncates the
  // sentence and Whisper transcribes half a thought, while ending late costs a
  // moment of silence on the end of a clip nobody listens to. Err long.
  var EXIT_RATIO = 0.45;

  var WORKLET = [
    'class Capture extends AudioWorkletProcessor {',
    '  process(inputs) {',
    '    const ch = inputs[0] && inputs[0][0];',
    '    if (ch) this.port.postMessage(new Float32Array(ch));',
    '    return true;',
    '  }',
    '}',
    'registerProcessor("capture", Capture);'
  ].join('\n');

  var state = {
    stream: null,
    node: null,
    source: null,
    sink: null,
    running: false,
    mode: 'ptt',
    // push mode
    pushing: false,
    chunks: [],
    startedAt: 0,
    // ambient mode
    preroll: [],
    prerollFrames: 0,
    speaking: false,
    silenceMs: 0,
    speechMs: 0,
    envelope: 0,
    noiseFloor: 0.01
  };

  var handlers = {
    onLevel: null,        // (0..1)
    onUtterance: null,    // (Float32Array pcm at TARGET_RATE)
    onSpeechStart: null,
    onSpeechEnd: null,
    onBargeIn: null,
    onError: null
  };

  function rate() { return global.ConsoleAudio.context().sampleRate; }

  /* Box-average decimation. Averaging over the window rather than picking
     every Nth sample gives a crude low-pass, which keeps a 48 kHz mic from
     aliasing hiss into the band Whisper cares about. */
  function resample(input, inRate) {
    if (inRate === TARGET_RATE) return input;
    var ratio = inRate / TARGET_RATE;
    var outLength = Math.floor(input.length / ratio);
    var out = new Float32Array(outLength);
    for (var i = 0; i < outLength; i++) {
      var start = Math.floor(i * ratio);
      var end = Math.min(Math.floor((i + 1) * ratio), input.length);
      var sum = 0;
      for (var j = start; j < end; j++) sum += input[j];
      out[i] = end > start ? sum / (end - start) : 0;
    }
    return out;
  }

  function merge(chunks) {
    var total = chunks.reduce(function (n, c) { return n + c.length; }, 0);
    var merged = new Float32Array(total);
    var offset = 0;
    chunks.forEach(function (c) { merged.set(c, offset); offset += c.length; });
    return merged;
  }

  function rms(frame) {
    var sum = 0;
    for (var i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
    return Math.sqrt(sum / frame.length);
  }

  /* --- lifecycle --------------------------------------------------------- */

  function open() {
    if (state.running) return Promise.resolve();
    var ctx = global.ConsoleAudio.context();

    return navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        // Echo cancellation is what makes ambient mode possible at all: it
        // references the system output and subtracts the reply coming back in
        // through the speakers.
        echoCancellation: true,
        noiseSuppression: true,
        // Automatic gain control is deliberately off. It ramps gain up during
        // pauses, which lifts room noise above any level-based threshold — a
        // take opened and then never closed, because the "silence" kept
        // getting amplified into something the detector read as speech.
        autoGainControl: false
      }
    }).then(function (stream) {
      state.stream = stream;
      var blob = new Blob([WORKLET], { type: 'application/javascript' });
      return ctx.audioWorklet.addModule(URL.createObjectURL(blob)).then(function () {
        state.source = ctx.createMediaStreamSource(stream);
        state.node = new AudioWorkletNode(ctx, 'capture');
        state.node.port.onmessage = function (e) { onFrame(e.data); };
        state.source.connect(state.node);
        // Worklets are only pulled when connected to the graph; a zero-gain
        // sink keeps it running without feeding the mic back to the speakers.
        state.sink = ctx.createGain();
        state.sink.gain.value = 0;
        state.node.connect(state.sink).connect(ctx.destination);
        state.running = true;
      });
    }).catch(function (err) {
      state.running = false;
      if (handlers.onError) handlers.onError(err);
      throw err;
    });
  }

  function close() {
    if (state.node) { try { state.node.port.onmessage = null; state.node.disconnect(); } catch (e) {} }
    if (state.source) { try { state.source.disconnect(); } catch (e) {} }
    if (state.sink) { try { state.sink.disconnect(); } catch (e) {} }
    if (state.stream) state.stream.getTracks().forEach(function (t) { t.stop(); });
    state.node = state.source = state.sink = state.stream = null;
    state.running = false;
    resetAmbient();
  }

  function resetAmbient() {
    state.preroll = [];
    state.prerollFrames = 0;
    state.speaking = false;
    state.silenceMs = 0;
    state.speechMs = 0;
    state.chunks = [];
  }

  /* --- frame handling ---------------------------------------------------- */

  function onFrame(frame) {
    var level = rms(frame);
    var frameMs = (frame.length / rate()) * 1000;

    // Detection runs on an envelope, not on raw frame energy. A 128-sample
    // frame is under 3ms — far shorter than the gaps between words, so raw
    // RMS dips below any sensible threshold constantly and a take gets cut
    // mid-sentence. Fast attack keeps onset responsive; slow release bridges
    // the gaps. The visualizer still gets the unsmoothed level.
    state.envelope += (level - state.envelope) * (level > state.envelope ? 0.5 : 0.012);

    if (handlers.onLevel) handlers.onLevel(Math.min(level * 5, 1));

    if (state.mode === 'ptt') {
      if (state.pushing) state.chunks.push(frame);
      return;
    }

    ambientFrame(frame, state.envelope, frameMs);
  }

  function ambientFrame(frame, level, frameMs) {
    // Track the room's noise floor only while nobody is talking *and* no take
    // is building — adapting through an onset lets the detector normalise away
    // the very speech it is trying to catch.
    if (!state.speaking && state.speechMs === 0) {
      var alpha = level < state.noiseFloor ? NOISE_ADAPT_DOWN : NOISE_ADAPT_UP;
      state.noiseFloor += (level - state.noiseFloor) * alpha;
    }

    // Two thresholds, not one. Opening a take takes real energy; closing it
    // requires falling well *below* that bar. With a single threshold the
    // detector chatters around it — a sentence would end and immediately
    // reopen, splitting one utterance into two half-transcribed fragments.
    var ducked = global.ConsoleAudio.isPlaying;
    var factor = ducked ? SPEECH_FACTOR_DUCKED : SPEECH_FACTOR;
    var enter = Math.max(state.noiseFloor * factor, ABSOLUTE_FLOOR);
    // The exit bar is also clamped above the measured room, because an exit
    // threshold below the noise floor can never be crossed — the take opens
    // and then stays open forever, which is the other way this fails.
    var exit = Math.max(enter * EXIT_RATIO, state.noiseFloor * 1.8);
    var voiced = state.speaking ? level > exit : level > enter;

    if (!state.speaking) {
      // Always retain a little history so a take never starts mid-syllable.
      state.preroll.push(frame);
      state.prerollFrames += frame.length;
      var keep = (PREROLL_MS / 1000) * rate();
      while (state.prerollFrames - state.preroll[0].length > keep) {
        state.prerollFrames -= state.preroll.shift().length;
      }

      if (voiced) {
        state.speechMs += frameMs;
        if (state.speechMs >= ONSET_MS) {   // sustained, not a click
          state.speaking = true;
          state.silenceMs = 0;
          state.chunks = state.preroll.slice();
          state.preroll = [];
          state.prerollFrames = 0;
          if (ducked && handlers.onBargeIn) handlers.onBargeIn();
          if (handlers.onSpeechStart) handlers.onSpeechStart();
        }
      } else {
        // Decay rather than reset. Speech is full of micro-gaps — plosives,
        // the space between words — and zeroing the counter on any quiet frame
        // meant the onset restarted continuously and a normal sentence never
        // accumulated the sustained energy needed to open a take.
        state.speechMs = Math.max(0, state.speechMs - frameMs * 2);
      }
      return;
    }

    state.chunks.push(frame);
    state.speechMs += frameMs;
    state.silenceMs = voiced ? 0 : state.silenceMs + frameMs;

    if (state.silenceMs >= SILENCE_HANGOVER_MS || state.speechMs >= MAX_UTTERANCE_MS) {
      finishUtterance();
    }
  }

  function finishUtterance() {
    var chunks = state.chunks;
    var spoke = state.speechMs - state.silenceMs;
    resetAmbient();
    if (handlers.onSpeechEnd) handlers.onSpeechEnd();
    if (spoke < MIN_SPEECH_MS || !chunks.length) return;
    if (handlers.onUtterance) handlers.onUtterance(resample(merge(chunks), rate()));
  }

  /* --- public API -------------------------------------------------------- */

  function setMode(mode) {
    if (state.mode === mode) return Promise.resolve();
    state.mode = mode;
    if (mode === 'ambient') return open().then(resetAmbient);
    close();
    return Promise.resolve();
  }

  function pushStart() {
    if (state.mode !== 'ptt') return Promise.resolve();
    state.pushing = true;
    state.chunks = [];
    state.startedAt = Date.now();
    return open();
  }

  function pushStop() {
    if (state.mode !== 'ptt' || !state.pushing) return;
    state.pushing = false;
    var tooShort = Date.now() - state.startedAt < MIN_TAKE_MS;
    var chunks = state.chunks;
    state.chunks = [];
    var captureRate = rate();
    close();
    if (handlers.onLevel) handlers.onLevel(0);
    if (tooShort || !chunks.length) return;
    if (handlers.onUtterance) handlers.onUtterance(resample(merge(chunks), captureRate));
  }

  /**
   * End the current ambient take immediately rather than waiting out the
   * silence hangover. Bound to the mic button in ambient mode, so there is
   * always a way to say "that's it, go" without sitting through the pause.
   */
  function cut() {
    if (state.mode !== 'ambient' || !state.speaking) return false;
    finishUtterance();
    return true;
  }

  global.ConsoleMic = {
    setMode: setMode,
    pushStart: pushStart,
    pushStop: pushStop,
    cut: cut,
    close: close,
    on: function (name, fn) { handlers[name] = fn; },
    get mode() { return state.mode; },
    get isPushing() { return state.pushing; },
    get isSpeaking() { return state.speaking; },
    // The detector is a handful of thresholds against a room that varies; being
    // able to read what it currently believes is the difference between tuning
    // it and guessing at it.
    get vad() {
      return {
        noiseFloor: state.noiseFloor,
        threshold: Math.max(
          state.noiseFloor * (global.ConsoleAudio.isPlaying ? SPEECH_FACTOR_DUCKED : SPEECH_FACTOR),
          ABSOLUTE_FLOOR
        ),
        speaking: state.speaking,
        speechMs: Math.round(state.speechMs),
        silenceMs: Math.round(state.silenceMs)
      };
    },
    TARGET_RATE: TARGET_RATE
  };
})(window);
