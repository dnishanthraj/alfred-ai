/* ==========================================================================
   Radial spectrum ring.

   Three sources feed it, and all three are real signal rather than decoration:
     speaking  — FFT of the ElevenLabs audio actually coming out of the speakers
     listening — RMS of the live microphone capture
     idle      — a slow breathing sine, the only synthetic source

   Bars are smoothed toward their targets each frame so the ring settles
   instead of strobing.
   ========================================================================== */

(function (global) {
  'use strict';

  var BARS = 128;
  var SMOOTHING = 0.28;   // per-frame approach rate toward the target value
  var DECAY = 0.055;      // how fast the ring falls back to idle

  function Visualizer(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.values = new Float32Array(BARS);
    this.targets = new Float32Array(BARS);
    this.analyser = null;
    this.freq = null;
    this.mode = 'idle';
    this.level = 0;         // external scalar, used by listening mode
    this.phase = 0;
    this.dpr = 1;

    this._resize = this._resize.bind(this);
    this._frame = this._frame.bind(this);

    window.addEventListener('resize', this._resize);
    this._resize();
    requestAnimationFrame(this._frame);
  }

  Visualizer.prototype.attach = function (analyser) {
    this.analyser = analyser;
    this.freq = analyser ? new Uint8Array(analyser.frequencyBinCount) : null;
  };

  Visualizer.prototype.setMode = function (mode) {
    this.mode = mode;
  };

  Visualizer.prototype.setLevel = function (level) {
    this.level = level;
  };

  Visualizer.prototype._resize = function () {
    var rect = this.canvas.getBoundingClientRect();
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.canvas.width = Math.max(1, Math.round(rect.width * this.dpr));
    this.canvas.height = Math.max(1, Math.round(rect.height * this.dpr));
  };

  /* Map the FFT onto the ring. Only the lower ~60% of bins is sampled — the
     top of the spectrum is near-empty for speech — on a mild curve that gives
     the voiced low-mids more of the ring than the hiss above them.

     The spectrum is drawn into one quadrant and mirrored across both axes.
     Mirroring on the vertical axis alone left every loud vowel bunched into
     the bottom of the ring; four-fold symmetry keeps it balanced whatever the
     voice is doing, which is what makes it read as an instrument. */
  Visualizer.prototype._readSpectrum = function () {
    this.analyser.getByteFrequencyData(this.freq);
    var usable = Math.floor(this.freq.length * 0.6);
    var quarter = BARS / 4;
    var half = BARS / 2;

    for (var i = 0; i < quarter; i++) {
      var t = i / quarter;
      var value = this.freq[Math.floor(Math.pow(t, 1.35) * usable)] / 255;
      this.targets[i] = value;                 // top, clockwise
      this.targets[BARS - 1 - i] = value;      // top, counter-clockwise
      this.targets[half - 1 - i] = value;      // bottom, clockwise
      this.targets[half + i] = value;          // bottom, counter-clockwise
    }
  };

  Visualizer.prototype._readIdle = function () {
    // Two waves at different rates so the standby ring drifts rather than
    // marching in a single obvious loop.
    for (var i = 0; i < BARS; i++) {
      var t = i / BARS;
      var slow = Math.sin(t * Math.PI * 4 + this.phase) * 0.5 + 0.5;
      var fast = Math.sin(t * Math.PI * 10 - this.phase * 1.7) * 0.5 + 0.5;
      this.targets[i] = 0.06 + slow * 0.05 + fast * 0.02;
    }
  };

  Visualizer.prototype._readLevel = function () {
    for (var i = 0; i < BARS; i++) {
      var wave = Math.sin((i / BARS) * Math.PI * 6 + this.phase * 2.2) * 0.5 + 0.5;
      this.targets[i] = 0.05 + this.level * (0.35 + wave * 0.65);
    }
  };

  Visualizer.prototype._frame = function () {
    requestAnimationFrame(this._frame);

    this.phase += 0.017;

    if (this.mode === 'speaking' && this.analyser) {
      this._readSpectrum();
    } else if (this.mode === 'listening') {
      this._readLevel();
    } else {
      this._readIdle();
    }

    for (var i = 0; i < BARS; i++) {
      var target = this.targets[i];
      if (target > this.values[i]) {
        this.values[i] += (target - this.values[i]) * SMOOTHING;
      } else {
        this.values[i] += (target - this.values[i]) * DECAY;
      }
    }

    this._draw();
  };

  Visualizer.prototype._draw = function () {
    var ctx = this.ctx;
    var w = this.canvas.width;
    var h = this.canvas.height;
    var cx = w / 2;
    var cy = h / 2;

    ctx.clearRect(0, 0, w, h);

    var radius = Math.min(w, h) * 0.28;
    var maxBar = Math.min(w, h) * 0.185;
    var lineWidth = Math.max(1, 2.1 * this.dpr);

    var warm = this.mode === 'listening'
      ? [200, 98, 79]
      : [224, 163, 62];

    // Average amplitude drives the core's opacity, so the centre breathes with
    // the voice rather than sitting at a constant brightness.
    var sum = 0;
    for (var k = 0; k < BARS; k++) sum += this.values[k];
    var mean = sum / BARS;

    // Inner core.
    var glow = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius);
    glow.addColorStop(0, 'rgba(' + warm[0] + ',' + warm[1] + ',' + warm[2] + ',' + (0.1 + mean * 0.42) + ')');
    glow.addColorStop(1, 'rgba(' + warm[0] + ',' + warm[1] + ',' + warm[2] + ',0)');
    ctx.fillStyle = glow;
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.fill();

    // Hairline reference ring.
    ctx.strokeStyle = 'rgba(255,255,255,0.075)';
    ctx.lineWidth = Math.max(1, this.dpr);
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.stroke();

    // Spectrum bars.
    ctx.lineCap = 'round';
    ctx.lineWidth = lineWidth;
    for (var i = 0; i < BARS; i++) {
      var angle = (i / BARS) * Math.PI * 2 - Math.PI / 2;
      var value = this.values[i];
      var length = 3 * this.dpr + value * maxBar;

      var cos = Math.cos(angle);
      var sin = Math.sin(angle);
      var x1 = cx + cos * radius;
      var y1 = cy + sin * radius;
      var x2 = cx + cos * (radius + length);
      var y2 = cy + sin * (radius + length);

      var alpha = 0.2 + Math.min(value * 1.9, 1) * 0.75;
      ctx.strokeStyle = 'rgba(' + warm[0] + ',' + warm[1] + ',' + warm[2] + ',' + alpha + ')';
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
    }

    // Core dot.
    ctx.fillStyle = 'rgba(' + warm[0] + ',' + warm[1] + ',' + warm[2] + ',' + (0.35 + mean * 0.55) + ')';
    ctx.beginPath();
    ctx.arc(cx, cy, (2.2 + mean * 4) * this.dpr, 0, Math.PI * 2);
    ctx.fill();
  };

  global.Visualizer = Visualizer;
})(window);
