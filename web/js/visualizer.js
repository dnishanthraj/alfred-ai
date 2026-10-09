/* ==========================================================================
   Radial spectrum ring.

   Three sources feed it, and all three are real signal rather than decoration:
     speaking  — FFT of the audio actually coming out of the speakers
     listening — RMS of the live microphone capture
     idle      — a slow breathing sine, the only synthetic source

   Bars are smoothed toward their targets each frame so the ring settles
   instead of strobing, and the colour is pulled from the current contact's
   accent so switching correspondent visibly changes the instrument.
   ========================================================================== */

(function (global) {
  'use strict';

  var BARS = 128;
  // Approach rates per 60 Hz frame, scaled by the real frame time so a 120 Hz
  // display does not animate twice as fast.
  var SMOOTHING = 0.28;   // toward a louder target
  var DECAY = 0.055;      // back down
  // The band a voice actually occupies. Bins were sampled up to ~13 kHz on a
  // near-linear scale, so four-fifths of the ring showed sibilance and hiss
  // and barely moved; on a log scale over this band every bar carries speech.
  var LOW_HZ = 80;
  var HIGH_HZ = 8000;

  function hexToRgb(hex, fallback) {
    var match = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec((hex || '').trim());
    if (!match) return fallback;
    return [parseInt(match[1], 16), parseInt(match[2], 16), parseInt(match[3], 16)];
  }

  /* `options.accent` fixes the colour — a seat on a group call stays its
     person's colour rather than following whoever's on the line. */
  function Visualizer(canvas, options) {
    this.fixedAccent = options && options.accent ? hexToRgb(options.accent, null) : null;
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.values = new Float32Array(BARS);
    this.targets = new Float32Array(BARS);
    this.analyser = null;
    this.freq = null;
    this.mode = 'idle';
    this.level = 0;
    this.phase = 0;
    this.lastFrame = 0;
    this.bands = null;
    this.dpr = 1;
    this.accent = [79, 168, 224];
    this.alert = [224, 87, 79];
    this._accentTick = 0;

    this._resize = this._resize.bind(this);
    this._frame = this._frame.bind(this);

    global.addEventListener('resize', this._resize);
    this._resize();
    requestAnimationFrame(this._frame);
  }

  Visualizer.prototype.setMode = function (mode) { this.mode = mode; };
  Visualizer.prototype.setLevel = function (level) { this.level = level; };

  Visualizer.prototype._resize = function () {
    var rect = this.canvas.getBoundingClientRect();
    this.dpr = Math.min(global.devicePixelRatio || 1, 2);
    this.canvas.width = Math.max(1, Math.round(rect.width * this.dpr));
    this.canvas.height = Math.max(1, Math.round(rect.height * this.dpr));
  };

  /* The analyser only exists once an AudioContext has been created, which
     doesn't happen until the operator authenticates. Pick it up when it
     appears rather than requiring the page to wire them together. */
  Visualizer.prototype._ensureAnalyser = function () {
    if (this.analyser) return;
    var audio = global.ConsoleAudio;
    if (audio && audio.analyser) {
      this.analyser = audio.analyser;
      this.freq = new Uint8Array(this.analyser.frequencyBinCount);
      this.bands = this._bands(this.analyser.context.sampleRate, this.freq.length);
    }
  };

  Visualizer.prototype._refreshAccent = function () {
    // Reading computed style every frame is wasteful; twice a second is plenty
    // to catch a contact switch.
    if (this._accentTick++ % 30) return;
    if (this.fixedAccent) { this.accent = this.fixedAccent; return; }
    var value = getComputedStyle(document.documentElement)
      .getPropertyValue('--contact-accent');
    this.accent = hexToRgb(value, this.accent);
  };

  /* Bin ranges for one quadrant of bars, log-spaced across the voice band.
     Each bar takes the loudest bin in its range rather than a single sample,
     so narrow harmonics are not skipped between frames — that skipping was
     the flicker in the old ring. */
  Visualizer.prototype._bands = function (sampleRate, binCount) {
    var quarter = BARS / 4;
    var hzPerBin = sampleRate / 2 / binCount;
    var bands = [];
    for (var i = 0; i < quarter; i++) {
      var lo = LOW_HZ * Math.pow(HIGH_HZ / LOW_HZ, i / quarter);
      var hi = LOW_HZ * Math.pow(HIGH_HZ / LOW_HZ, (i + 1) / quarter);
      var a = Math.max(1, Math.floor(lo / hzPerBin));
      var b = Math.max(a + 1, Math.ceil(hi / hzPerBin));
      bands.push([a, Math.min(b, binCount)]);
    }
    return bands;
  };

  /* Map the FFT onto the ring.

     The spectrum is drawn into one quadrant and mirrored across both axes.
     Mirroring on the vertical axis alone left every loud vowel bunched into
     the bottom of the ring; four-fold symmetry keeps it balanced whatever the
     voice is doing, which is what makes it read as an instrument. */
  Visualizer.prototype._readSpectrum = function () {
    this.analyser.getByteFrequencyData(this.freq);
    var quarter = BARS / 4;
    var half = BARS / 2;

    for (var i = 0; i < quarter; i++) {
      var band = this.bands[i];
      var peak = 0;
      for (var j = band[0]; j < band[1]; j++) if (this.freq[j] > peak) peak = this.freq[j];
      // Speech is louder low than high; lift the top end so the ring is even.
      var value = Math.min(1, (peak / 255) * (0.85 + 0.35 * i / quarter));
      this.targets[i] = value;
      this.targets[BARS - 1 - i] = value;
      this.targets[half - 1 - i] = value;
      this.targets[half + i] = value;
    }
  };

  Visualizer.prototype._readIdle = function () {
    // Two waves at different rates so standby drifts rather than marching in
    // a single obvious loop.
    for (var i = 0; i < BARS; i++) {
      var t = i / BARS;
      var slow = Math.sin(t * Math.PI * 4 + this.phase) * 0.5 + 0.5;
      var fast = Math.sin(t * Math.PI * 10 - this.phase * 1.7) * 0.5 + 0.5;
      this.targets[i] = 0.06 + slow * 0.05 + fast * 0.02;
    }
  };

  /* Thinking or looking something up: a slow bright arc travelling the ring.
     Not a spinner — it moves at the pace of someone considering — but it
     tells you the line is alive while he works out what to say. */
  Visualizer.prototype._readThinking = function () {
    var head = (this.phase * 0.35) % 1;
    for (var i = 0; i < BARS; i++) {
      var d = Math.abs(i / BARS - head);
      d = Math.min(d, 1 - d);
      var arc = Math.exp(-(d * d) / 0.006);
      this.targets[i] = 0.06 + arc * 0.16;
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

    var now = performance.now();
    // Frames since the last one at 60 Hz; capped so a backgrounded tab does
    // not jump the ring when it comes back.
    var frames = this.lastFrame ? Math.min((now - this.lastFrame) / (1000 / 60), 4) : 1;
    this.lastFrame = now;

    this._ensureAnalyser();
    this._refreshAccent();
    this.phase += 0.017 * frames;

    if (this.mode === 'speaking' && this.analyser) {
      this._readSpectrum();
    } else if (this.mode === 'listening') {
      this._readLevel();
    } else if (this.mode === 'thinking' || this.mode === 'searching') {
      this._readThinking();
    } else {
      this._readIdle();
    }

    var rise = 1 - Math.pow(1 - SMOOTHING, frames);
    var fall = 1 - Math.pow(1 - DECAY, frames);
    for (var i = 0; i < BARS; i++) {
      var target = this.targets[i];
      this.values[i] += (target - this.values[i]) * (target > this.values[i] ? rise : fall);
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
    var colour = this.mode === 'listening' ? this.alert : this.accent;
    var rgb = colour[0] + ',' + colour[1] + ',' + colour[2];

    // Mean amplitude drives the core, so the centre breathes with the voice.
    var sum = 0;
    for (var k = 0; k < BARS; k++) sum += this.values[k];
    var mean = sum / BARS;

    var glow = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius);
    glow.addColorStop(0, 'rgba(' + rgb + ',' + (0.09 + mean * 0.4) + ')');
    glow.addColorStop(1, 'rgba(' + rgb + ',0)');
    ctx.fillStyle = glow;
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.fill();

    ctx.strokeStyle = 'rgba(255,255,255,0.07)';
    ctx.lineWidth = Math.max(1, this.dpr);
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.stroke();

    ctx.lineCap = 'round';
    ctx.lineWidth = Math.max(1, 2.1 * this.dpr);
    for (var i = 0; i < BARS; i++) {
      var angle = (i / BARS) * Math.PI * 2 - Math.PI / 2;
      var value = this.values[i];
      var length = 3 * this.dpr + value * maxBar;
      var cos = Math.cos(angle);
      var sin = Math.sin(angle);

      ctx.strokeStyle = 'rgba(' + rgb + ',' + (0.2 + Math.min(value * 1.9, 1) * 0.75) + ')';
      ctx.beginPath();
      ctx.moveTo(cx + cos * radius, cy + sin * radius);
      ctx.lineTo(cx + cos * (radius + length), cy + sin * (radius + length));
      ctx.stroke();
    }

    ctx.fillStyle = 'rgba(' + rgb + ',' + (0.35 + mean * 0.55) + ')';
    ctx.beginPath();
    ctx.arc(cx, cy, (2.2 + mean * 4) * this.dpr, 0, Math.PI * 2);
    ctx.fill();
  };

  global.Visualizer = Visualizer;
})(window);
