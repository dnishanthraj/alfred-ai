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

  global.ConsoleTones = {
    startRinging: startRinging,
    stopRinging: stopRinging,
    connected: connected,
    disconnected: disconnected
  };
})(window);
