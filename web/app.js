/* ==========================================================================
   Console client.

   Owns everything presentational: the socket, the transcript, microphone
   capture, and audio playback. The server sends engine events and raw mp3
   bytes; nothing here decides what Alfred says.

   Audio is played here rather than on the machine's own output so the
   visualizer can tap a real AnalyserNode. That also means playback stops the
   instant the operator interrupts, with no process to kill.
   ========================================================================== */

(function () {
  'use strict';

  var TARGET_RATE = 16000;   // what Whisper expects, server-side
  var MIN_TAKE_MS = 250;     // shorter than this is a mis-tap, not speech

  var el = {
    gate: document.getElementById('gate'),
    gateBtn: document.getElementById('gate-btn'),
    gateHint: document.getElementById('gate-hint'),
    log: document.getElementById('log'),
    input: document.getElementById('input'),
    compose: document.getElementById('compose'),
    ptt: document.getElementById('ptt'),
    clock: document.getElementById('clock'),
    linkState: document.getElementById('link-state'),
    assistantName: document.getElementById('assistant-name'),
    vizState: document.getElementById('viz-state'),
    vizSub: document.getElementById('viz-sub'),
    rdModel: document.getElementById('rd-model'),
    rdStt: document.getElementById('rd-stt'),
    rdVoice: document.getElementById('rd-voice'),
    rdUser: document.getElementById('rd-user')
  };

  var STATE_COPY = {
    idle:         ['Standby',      'Awaiting input'],
    listening:    ['Receiving',    'Channel open'],
    transcribing: ['Resolving',    'Parsing speech'],
    searching:    ['Scanning',     'Querying the grid'],
    thinking:     ['Processing',   'Composing reply'],
    speaking:     ['Transmitting', 'Voice synthesis active']
  };

  var names = { assistant: 'Alfred', user: 'Operator' };
  var socket = null;
  var audioCtx = null;
  var analyser = null;
  var viz = null;
  var streamingTurn = null;

  /* --- transcript ------------------------------------------------------- */

  function atBottom() {
    return el.log.scrollHeight - el.log.scrollTop - el.log.clientHeight < 90;
  }

  function scroll(force) {
    if (force || atBottom()) el.log.scrollTop = el.log.scrollHeight;
  }

  function addTurn(kind, who, text, level) {
    var stick = atBottom();
    var turn = document.createElement('article');
    turn.className = 'turn turn--' + kind;
    if (level) turn.dataset.level = level;

    var label = document.createElement('span');
    label.className = 'log__who';
    label.textContent = who;

    var body = document.createElement('p');
    body.className = 'log__text';
    body.textContent = text || '';

    turn.appendChild(label);
    turn.appendChild(body);
    el.log.appendChild(turn);
    scroll(stick);
    return turn;
  }

  function closeStreamingTurn(finalText) {
    if (!streamingTurn) return false;
    streamingTurn.classList.remove('turn--streaming');
    if (typeof finalText === 'string' && finalText.length) {
      // The guards trim the tail after generation, so the streamed text and the
      // text actually spoken can differ. The final wins.
      streamingTurn.querySelector('.log__text').textContent = finalText;
    }
    streamingTurn = null;
    scroll(true);
    return true;
  }

  /* --- state ------------------------------------------------------------ */

  function setState(state) {
    document.documentElement.dataset.state = state;
    var copy = STATE_COPY[state] || STATE_COPY.idle;
    el.vizState.textContent = copy[0];
    el.vizSub.textContent = copy[1];
    if (viz) viz.setMode(state);
  }

  /* --- audio playback --------------------------------------------------- */

  var queue = [];
  var playing = false;
  var currentSource = null;

  function ensureAudio() {
    if (audioCtx) return audioCtx;
    var Ctx = window.AudioContext || window.webkitAudioContext;
    audioCtx = new Ctx();
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 2048;
    analyser.smoothingTimeConstant = 0.75;
    analyser.connect(audioCtx.destination);
    if (viz) viz.attach(analyser);
    return audioCtx;
  }

  function enqueueAudio(clipId) {
    queue.push(clipId);
    if (!playing) playNext();
  }

  function playNext() {
    if (!queue.length) {
      playing = false;
      currentSource = null;
      setState('idle');
      return;
    }
    playing = true;
    var clipId = queue.shift();

    fetch('/api/audio/' + clipId)
      .then(function (r) { return r.arrayBuffer(); })
      .then(function (buf) { return ensureAudio().decodeAudioData(buf); })
      .then(function (decoded) {
        var source = audioCtx.createBufferSource();
        source.buffer = decoded;
        source.connect(analyser);
        source.onended = function () {
          if (currentSource === source) { currentSource = null; playNext(); }
        };
        currentSource = source;
        setState('speaking');
        source.start();
      })
      .catch(function () { playNext(); });
  }

  function stopAudio() {
    queue.length = 0;
    if (currentSource) {
      var source = currentSource;
      currentSource = null;
      try { source.onended = null; source.stop(); } catch (e) { /* already ended */ }
    }
    playing = false;
    setState('idle');
  }

  /* --- microphone ------------------------------------------------------- */

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

  var mic = { stream: null, node: null, source: null, chunks: [], active: false, startedAt: 0 };

  /* Box-average decimation to 16 kHz. Averaging across the window rather than
     picking every Nth sample gives a crude low-pass, which keeps a 48 kHz mic
     from aliasing hiss into the band Whisper cares about. */
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

  function startCapture() {
    if (mic.active) return Promise.resolve();
    mic.active = true;
    mic.chunks = [];
    mic.startedAt = Date.now();
    el.ptt.dataset.live = '1';
    setState('listening');

    var ctx = ensureAudio();
    if (ctx.state === 'suspended') ctx.resume();

    return navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }
    }).then(function (stream) {
      if (!mic.active) { stream.getTracks().forEach(function (t) { t.stop(); }); return; }
      mic.stream = stream;
      var blob = new Blob([WORKLET], { type: 'application/javascript' });
      return ctx.audioWorklet.addModule(URL.createObjectURL(blob)).then(function () {
        if (!mic.active) { stream.getTracks().forEach(function (t) { t.stop(); }); return; }
        mic.source = ctx.createMediaStreamSource(stream);
        mic.node = new AudioWorkletNode(ctx, 'capture');
        mic.node.port.onmessage = function (event) {
          var chunk = event.data;
          mic.chunks.push(chunk);
          var sum = 0;
          for (var i = 0; i < chunk.length; i++) sum += chunk[i] * chunk[i];
          if (viz) viz.setLevel(Math.min(Math.sqrt(sum / chunk.length) * 5, 1));
        };
        mic.source.connect(mic.node);
        // Worklets are only pulled when connected to the graph; a zero-gain
        // sink keeps it running without feeding the mic back to the speakers.
        var mute = ctx.createGain();
        mute.gain.value = 0;
        mic.node.connect(mute).connect(ctx.destination);
      });
    }).catch(function (err) {
      mic.active = false;
      el.ptt.dataset.live = '0';
      setState('idle');
      addTurn('system', 'System', 'Microphone unavailable: ' + err.message, 'error');
    });
  }

  function stopCapture() {
    if (!mic.active) return;
    mic.active = false;
    el.ptt.dataset.live = '0';
    if (viz) viz.setLevel(0);

    var tooShort = Date.now() - mic.startedAt < MIN_TAKE_MS;

    if (mic.node) { try { mic.node.port.onmessage = null; mic.node.disconnect(); } catch (e) {} }
    if (mic.source) { try { mic.source.disconnect(); } catch (e) {} }
    if (mic.stream) mic.stream.getTracks().forEach(function (t) { t.stop(); });
    mic.node = mic.source = mic.stream = null;

    var chunks = mic.chunks;
    mic.chunks = [];

    if (tooShort || !chunks.length) { setState('idle'); return; }

    var total = chunks.reduce(function (n, c) { return n + c.length; }, 0);
    var merged = new Float32Array(total);
    var offset = 0;
    chunks.forEach(function (c) { merged.set(c, offset); offset += c.length; });

    var pcm = resample(merged, audioCtx.sampleRate);
    setState('transcribing');

    fetch('/api/transcribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: pcm.buffer
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var text = (data.text || '').trim();
        if (!text) { setState('idle'); return; }
        send({ type: 'prompt', text: text });
      })
      .catch(function (err) {
        setState('idle');
        addTurn('system', 'System', 'Transcription failed: ' + err.message, 'error');
      });
  }

  /* --- socket ----------------------------------------------------------- */

  function send(payload) {
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(payload));
  }

  function handle(event) {
    switch (event.type) {
      case 'state':
        // Playback owns the speaking state; the engine going idle mid-clip
        // shouldn't cut the visualizer short.
        if (!(playing && event.value === 'idle')) setState(event.value);
        break;
      case 'message':
        if (event.role === 'user') {
          closeStreamingTurn();
          addTurn('user', names.user, event.text);
        } else {
          closeStreamingTurn();
          addTurn('assistant', names.assistant, event.text);
        }
        break;
      case 'reply_start':
        streamingTurn = addTurn('assistant', names.assistant, '');
        streamingTurn.classList.add('turn--streaming');
        break;
      case 'token':
        if (streamingTurn) {
          streamingTurn.querySelector('.log__text').textContent += event.text;
          scroll(false);
        }
        break;
      case 'reply_end':
        closeStreamingTurn(event.text);
        break;
      case 'speak':
        enqueueAudio(event.audio_id);
        break;
      case 'notice':
        addTurn('system', 'System', event.text, event.level);
        break;
    }
  }

  function connect() {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(proto + '//' + location.host + '/ws');

    socket.onopen = function () {
      el.linkState.textContent = 'Link Active';
      el.linkState.dataset.up = '1';
    };
    socket.onmessage = function (message) {
      try { handle(JSON.parse(message.data)); } catch (e) { /* ignore malformed frame */ }
    };
    socket.onclose = function () {
      el.linkState.textContent = 'Reconnecting';
      el.linkState.dataset.up = '0';
      setState('idle');
      setTimeout(connect, 1500);
    };
    socket.onerror = function () { try { socket.close(); } catch (e) {} };
  }

  /* --- session ---------------------------------------------------------- */

  function loadSession() {
    return fetch('/api/session').then(function (r) { return r.json(); }).then(function (info) {
      names.assistant = info.assistant || 'Alfred';
      names.user = info.user || 'Operator';
      el.assistantName.textContent = names.assistant;
      el.rdModel.textContent = info.model || '—';
      el.rdUser.textContent = names.user;
      el.rdStt.textContent = info.stt || '—';
      el.rdVoice.textContent = info.voice ? 'ElevenLabs' : 'Unavailable';
      el.rdVoice.dataset.ok = info.voice ? '1' : '0';
      document.title = names.assistant + ' · B.A.T. Console';

      // Replay anything said before this tab connected.
      (info.transcript || []).forEach(function (turn) {
        if (turn.role === 'system') {
          addTurn('system', 'System', turn.text, turn.level);
        } else {
          addTurn(turn.role, turn.role === 'user' ? names.user : names.assistant, turn.text);
        }
      });
      scroll(true);
    }).catch(function () { /* header stays on defaults */ });
  }

  /* --- input ------------------------------------------------------------ */

  el.compose.addEventListener('submit', function (e) {
    e.preventDefault();
    var text = el.input.value.trim();
    if (!text) return;
    stopAudio();
    el.input.value = '';
    send({ type: 'prompt', text: text });
  });

  el.ptt.addEventListener('pointerdown', function (e) {
    e.preventDefault();
    stopAudio();
    startCapture();
  });
  ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (name) {
    el.ptt.addEventListener(name, function () { stopCapture(); });
  });

  // Hold Space to talk, but never while the operator is typing.
  var spaceDown = false;
  window.addEventListener('keydown', function (e) {
    if (e.code !== 'Space' || spaceDown) return;
    if (document.activeElement === el.input) return;
    if (el.gate.hasAttribute('hidden') === false) return;
    e.preventDefault();
    spaceDown = true;
    stopAudio();
    startCapture();
  });
  window.addEventListener('keyup', function (e) {
    if (e.code !== 'Space' || !spaceDown) return;
    spaceDown = false;
    stopCapture();
  });

  // Escape silences playback without ending the session.
  window.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') stopAudio();
  });

  /* --- clock ------------------------------------------------------------ */

  setInterval(function () {
    var now = new Date();
    el.clock.textContent = [now.getHours(), now.getMinutes(), now.getSeconds()]
      .map(function (n) { return String(n).padStart(2, '0'); }).join(':');
  }, 1000);

  /* --- boot ------------------------------------------------------------- */

  viz = new Visualizer(document.getElementById('viz'));
  setState('idle');
  loadSession();

  el.gateBtn.addEventListener('click', function () {
    var ctx = ensureAudio();
    var start = ctx.state === 'suspended' ? ctx.resume() : Promise.resolve();
    start.then(function () {
      el.gate.setAttribute('hidden', '');
      el.input.focus();
      connect();
    }).catch(function () {
      el.gateHint.textContent = 'Audio subsystem refused to start';
    });
  });
})();
