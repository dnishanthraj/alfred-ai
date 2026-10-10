/* ==========================================================================
   Call tones.

   Synthesised, not sampled. Three short sounds do not justify shipping audio
   files, a fetch on the critical path, or a licence to worry about — and
   oscillators let the ring stop dead on the beat it is answered, which a
   looping file cannot do cleanly.

   They are deliberately quiet and un-telephonic. This is a console in a
   basement, not a handset: a low double pulse for ringing, a rising pair on
   connect, a falling pair on disconnect. Everything sits under a soft envelope
   because a bare oscillator gate clicks.
   ========================================================================== */

(function (global) {
  'use strict';

  var GAIN = 0.055;      // quiet enough to talk over, present enough to notice
  var ringTimer = null;

  function ctx() { return global.ConsoleAudio.context(); }

  /**
   * One shaped note. The attack and release are what stop it clicking — an
   * oscillator switched on at full gain pops, every time.
   */
  function note(freq, startAt, duration, peak) {
    var c = ctx();
    var osc = c.createOscillator();
    var gain = c.createGain();

    osc.type = 'sine';
    osc.frequency.setValueAtTime(freq, startAt);

    gain.gain.setValueAtTime(0.0001, startAt);
    gain.gain.exponentialRampToValueAtTime(peak || GAIN, startAt + 0.02);
    gain.gain.setValueAtTime(peak || GAIN, startAt + duration - 0.04);
    gain.gain.exponentialRampToValueAtTime(0.0001, startAt + duration);

    // Straight to the destination, bypassing the analyser: the visualizer
    // should react to a voice, not to the console's own furniture.
    osc.connect(gain).connect(c.destination);
    osc.start(startAt);
    osc.stop(startAt + duration + 0.02);
  }

  /** A double pulse, the shape of a ring without imitating a telephone. */
  function ringBurst() {
    var t = ctx().currentTime;
    note(392, t, 0.38);
    note(294, t + 0.02, 0.38);
    note(392, t + 0.52, 0.38);
    note(294, t + 0.54, 0.38);
  }

  function startRinging() {
    stopRinging();
    ringBurst();
    ringTimer = setInterval(ringBurst, 2600);
  }

  function stopRinging() {
    if (ringTimer) { clearInterval(ringTimer); ringTimer = null; }
  }

  /** Answered: two notes rising. */
  function connected() {
    stopRinging();
    var t = ctx().currentTime;
    note(392, t, 0.13, 0.05);
    note(587, t + 0.11, 0.26, 0.05);
  }

  /** Hung up: the same interval, falling. */
  function disconnected() {
    stopRinging();
    var t = ctx().currentTime;
    note(494, t, 0.13, 0.045);
    note(330, t + 0.11, 0.3, 0.045);
  }

  /* --- the lock screen -----------------------------------------------------
     The gate is stagecraft, and stagecraft with no sound is half-built: a
     passcode that lands in silence gives you nothing to feel. These are the
     same three-note vocabulary as the call tones so it reads as one instrument.
     ---------------------------------------------------------------------- */

  /** Accepted: a rising major triad — resolved, and obviously so. */
  function granted() {
    var t = ctx().currentTime;
    note(392, t, 0.11, 0.05);
    note(523, t + 0.09, 0.11, 0.05);
    note(784, t + 0.18, 0.34, 0.05);
  }

  /** Rejected: a flat two-note buzz on a tritone. Deliberately unpleasant. */
  function denied() {
    var t = ctx().currentTime;
    note(233, t, 0.16, 0.05);
    note(220, t + 0.13, 0.22, 0.05);
  }

  /** Locked out: lower, slower, and it does not resolve. */
  function lockedOut() {
    var t = ctx().currentTime;
    note(196, t, 0.3, 0.055);
    note(185, t + 0.26, 0.5, 0.055);
  }

  /** One tick per second of a lockout, so the wait is audible rather than dead. */
  function tick() {
    var t = ctx().currentTime;
    note(147, t, 0.05, 0.02);
  }

  /** A text arrived: one soft, high blip — noticed, not alarming. */
  function message() {
    var t = ctx().currentTime;
    note(880, t, 0.09, 0.03);
    note(1175, t + 0.07, 0.14, 0.025);
  }

  /** Tagged — "@Bruce", "@everyone": the message blip with a third note on
      top, brighter, so a ping reads apart from the chat going by. */
  function ping() {
    var t = ctx().currentTime;
    note(988, t, 0.07, 0.035);
    note(1319, t + 0.07, 0.07, 0.035);
    note(1760, t + 0.14, 0.2, 0.03);
  }

  /* --- the interface ------------------------------------------------------
     The rest of the console's furniture, in the same instrument: tiny, high
     and dry, so a pointer moving across the directory sounds like a machine
     paying attention rather than a toy. Hover ticks are rate-limited — a
     sweep across seven rows should be a flutter, not a drumroll.
     ---------------------------------------------------------------------- */

  /** A click-length tone with a near-instant envelope: too short for note(). */
  function blip(freq, startAt, duration, peak) {
    var c = ctx();
    if (c.state !== 'running') return;     // before any gesture: stay silent
    var osc = c.createOscillator();
    var gain = c.createGain();
    osc.type = 'triangle';
    osc.frequency.setValueAtTime(freq, startAt);
    gain.gain.setValueAtTime(0.0001, startAt);
    gain.gain.exponentialRampToValueAtTime(peak, startAt + 0.004);
    gain.gain.exponentialRampToValueAtTime(0.0001, startAt + duration);
    osc.connect(gain).connect(c.destination);
    osc.start(startAt);
    osc.stop(startAt + duration + 0.01);
  }

  var lastHover = 0;
  /** The pointer settles on something you can press. */
  function hover() {
    var now = performance.now();
    if (now - lastHover < 70) return;
    lastHover = now;
    blip(2350, ctx().currentTime, 0.025, 0.012);
  }

  /** Pressed: two ticks, down then up. */
  function press() {
    var t = ctx().currentTime;
    blip(1600, t, 0.03, 0.02);
    blip(2400, t + 0.035, 0.035, 0.016);
  }

  /** Sent: a short rising sweep, the message leaving. */
  function sent() {
    var c = ctx();
    if (c.state !== 'running') return;
    var t = c.currentTime;
    var osc = c.createOscillator();
    var gain = c.createGain();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(660, t);
    osc.frequency.exponentialRampToValueAtTime(1320, t + 0.12);
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(0.03, t + 0.015);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.16);
    osc.connect(gain).connect(c.destination);
    osc.start(t);
    osc.stop(t + 0.18);
  }

  /** A missed call: the message blip, falling — something you didn't get to. */
  function missed() {
    var t = ctx().currentTime;
    note(784, t, 0.1, 0.03);
    note(523, t + 0.09, 0.18, 0.028);
  }

  /** Someone calling him: a rising three-note phrase — unmistakably inbound,
      where the outgoing ring is a low double pulse. */
  function incomingBurst() {
    var t = ctx().currentTime;
    note(523, t, 0.16, 0.05);
    note(659, t + 0.16, 0.16, 0.05);
    note(784, t + 0.32, 0.3, 0.05);
  }

  function startIncoming() {
    stopRinging();
    incomingBurst();
    ringTimer = setInterval(incomingBurst, 2200);
  }

  global.ConsoleTones = {
    hover: hover,
    press: press,
    sent: sent,
    missed: missed,
    startIncoming: startIncoming,
    message: message,
    ping: ping,
    startRinging: startRinging,
    stopRinging: stopRinging,
    connected: connected,
    disconnected: disconnected,
    granted: granted,
    denied: denied,
    lockedOut: lockedOut,
    tick: tick
  };
})(window);
