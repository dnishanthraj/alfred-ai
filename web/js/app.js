/* ==========================================================================
   Console client.

   Owns everything presentational: the socket, the directory, the link, and the
   two input modes. The server sends engine events and mp3 bytes; nothing here
   decides what a contact says.

   Two deliberate absences shape this file:

   No transcript. A conversation you are having out loud does not need a log of
   itself, and a scrollback is the strongest possible reminder that you are
   typing at software. Only the last thing said to you stays on screen.

   No auto-connect. Nobody is on the line until you call them. The instrument
   materialises when the link opens and dissolves when it closes, so the
   console is visibly empty when it is empty.

   What the contact says is revealed *in time with the speech*, word by word
   across each sentence's real audio duration. A reply printed the instant the
   model finishes writing reads as a chat log with a voice bolted on.
   ========================================================================== */

(function () {
  'use strict';

  var el = {};
  var state = {
    contacts: {},
    order: [],
    currentId: null,     // who the console is showing
    connectedId: null,   // who is actually on the line
    pending: {},         // sentences awaiting audio, by index
    rendered: {},        // sentence indices already shown
    fresh: true,         // next sentence starts a new utterance
    socket: null,
    viz: null,
    mode: 'ptt',
    spaceDown: false,
    generationDone: true,
    flushTimer: null
  };

  // Deliberately free of machinery. "Composing reply" and "voice synthesis"
  // describe a program; the point of this console is that it doesn't feel like
  // one. What is left is what a person on a radio link would say.
  var STATE_COPY = {
    idle:         '',
    listening:    'Listening',
    transcribing: 'One moment',
    searching:    'Checking',
    thinking:     '',
    speaking:     ''
  };

  function $(id) { return document.getElementById(id); }

  function cacheElements() {
    ['input', 'compose', 'ptt', 'clock', 'bar-title', 'book', 'lock',
     'heard', 'utterance', 'status', 'empty', 'viz-wrap',
     'mode-ptt', 'mode-ambient'].forEach(function (id) { el[id] = $(id); });
  }

  /* --- the spoken line ---------------------------------------------------- */

  function clearUtterance() {
    el.utterance.textContent = '';
    state.pending = {};
    state.rendered = {};
  }

  function showHeard(text) {
    el.heard.textContent = text ? '“' + text + '”' : '';
    el.heard.dataset.show = text ? '1' : '0';
  }

  /**
   * Release a sentence's words across the duration of its audio. Word length
   * is a decent proxy for how long it takes to say, which keeps the text from
   * drifting ahead of or behind the voice over a long sentence.
   */
  function revealSentence(text, index, durationMs) {
    if (state.rendered[index]) return;
    state.rendered[index] = true;
    delete state.pending[index];

    // The first sentence of a reply replaces whatever was said before it.
    if (state.fresh) {
      el.utterance.textContent = '';
      state.fresh = false;
    }

    var span = document.createElement('span');
    span.className = 'said';
    if (el.utterance.textContent) span.textContent = ' ';
    el.utterance.appendChild(span);

    var words = text.split(/\s+/).filter(Boolean);
    if (!words.length) return;

    var weights = words.map(function (w) { return w.length + 1; });
    var total = weights.reduce(function (a, b) { return a + b; }, 0);
    // Aim slightly short of the clip so the last word lands before silence.
    var budget = Math.max(durationMs * 0.92, 200);
    var elapsed = 0;

    words.forEach(function (word, i) {
      var at = elapsed;
      setTimeout(function () {
        span.textContent += (i === 0 ? '' : ' ') + word;
      }, at);
      elapsed += (weights[i] / total) * budget;
    });
  }

  /**
   * The flush is always deferred by a beat, so it can still be in flight when
   * the next turn begins — any new content cancels a pending one.
   */
  function scheduleFlush(delay) {
    cancelFlush();
    state.flushTimer = setTimeout(flushUnspoken, delay);
  }

  function cancelFlush() {
    if (state.flushTimer) { clearTimeout(state.flushTimer); state.flushTimer = null; }
  }

  /**
   * Show anything that will never be spoken — no voice configured, or
   * synthesis failed. Waits on playback, not just generation: `turn_complete`
   * means every clip has been made, but the first may not have started, and
   * flushing then would print the reply for the audio to reveal again.
   */
  function flushUnspoken() {
    state.flushTimer = null;
    if (ConsoleAudio.isPlaying) return;
    Object.keys(state.pending)
      .map(Number)
      .sort(function (a, b) { return a - b; })
      .forEach(function (index) { revealSentence(state.pending[index], index, 0); });
  }

  /* --- status ------------------------------------------------------------- */

  function setState(value) {
    document.documentElement.dataset.state = value;
    el.status.textContent = STATE_COPY[value] || '';
    if (state.viz) state.viz.setMode(value);
  }

  function setLink(value) {
    document.documentElement.dataset.link = value;
    renderDirectory();
  }

  /* --- directory ---------------------------------------------------------- */

  function renderDirectory() {
    el.book.innerHTML = '';
    state.order.forEach(function (id) {
      var contact = state.contacts[id];
      var live = state.connectedId === id;

      var li = document.createElement('li');
      li.className = 'book__item';
      li.dataset.live = live ? '1' : '0';
      li.style.setProperty('--contact-accent', contact.accent);

      var row = document.createElement('div');
      row.className = 'book__row';

      var dot = document.createElement('span');
      dot.className = 'book__dot';

      var text = document.createElement('span');
      var name = document.createElement('span');
      name.className = 'book__name';
      name.textContent = contact.name;
      var role = document.createElement('span');
      role.className = 'book__role';
      role.textContent = live ? 'Connected'
                              : (contact.available ? contact.role : 'Unavailable');
      text.appendChild(name);
      text.appendChild(role);

      row.appendChild(dot);
      row.appendChild(text);

      var call = document.createElement('button');
      call.type = 'button';
      call.className = 'book__call';
      call.dataset.live = live ? '1' : '0';
      call.textContent = live ? 'End' : 'Call';
      call.disabled = !contact.available && !live;
      call.addEventListener('click', function () {
        if (live) hangUp(); else placeCall(id);
      });

      li.appendChild(row);
      li.appendChild(call);
      el.book.appendChild(li);
    });
  }

  function placeCall(contactId) {
    var contact = state.contacts[contactId];
    if (!contact) return;
    state.currentId = contactId;
    state.connectedId = contactId;
    document.documentElement.style.setProperty('--contact-accent', contact.accent);
    el['bar-title'].textContent = contact.full_name;
    document.title = contact.name + ' · WayneTech Console';

    clearUtterance();
    showHeard('');
    state.fresh = true;
    setLink('connecting');
    setState('thinking');
    send({ type: 'connect', id: contactId });
    el.input.focus();
  }

  function hangUp() {
    ConsoleAudio.stop();
    ConsoleMic.close();
    setMode('ptt');
    send({ type: 'disconnect' });
    state.connectedId = null;
    el['bar-title'].textContent = '';
    document.title = 'WayneTech Console';
    clearUtterance();
    showHeard('');
    state.fresh = true;
    setLink('off');
    setState('idle');
  }

  /* --- socket ------------------------------------------------------------- */

  function send(payload) {
    if (state.socket && state.socket.readyState === WebSocket.OPEN) {
      state.socket.send(JSON.stringify(payload));
    }
  }

  function handle(event) {
    // Nothing on the line means nothing to render. Without this, hanging up
    // mid-reply leaves the rest of the turn still arriving: sentences queue,
    // audio plays, and a contact you just cut off keeps talking.
    var conversational = event.type !== 'notice';
    if (!state.connectedId && conversational) return;

    switch (event.type) {
      case 'state':
        // Playback owns the speaking state; the engine going idle mid-clip
        // must not cut the visualizer short.
        if (!(ConsoleAudio.isPlaying && event.value === 'idle')) setState(event.value);
        break;

      case 'message':
        if (event.role === 'user') {
          cancelFlush();
          showHeard(event.text);
          // Clear his last line straight away. Leaving it up pairs your new
          // question with his answer to the previous one, which reads as a
          // non-sequitur for however long he takes to reply.
          clearUtterance();
          state.fresh = true;
        }
        break;

      case 'reply_start':
        cancelFlush();
        state.generationDone = false;
        break;

      case 'sentence':
        // Held, not shown: it appears when its audio starts.
        cancelFlush();
        state.generationDone = false;
        state.pending[event.index] = event.text;
        if (document.documentElement.dataset.link === 'connecting') setLink('on');
        break;

      case 'speak':
        ConsoleAudio.enqueue(event.audio_id, event.text, event.index);
        break;

      case 'reply_end':
        break;   // audio may still be in flight; wait for turn_complete

      case 'turn_complete':
        state.generationDone = true;
        if (document.documentElement.dataset.link === 'connecting') setLink('on');
        scheduleFlush(60);
        break;

      case 'notice':
        // System messages share the utterance line rather than a log, and are
        // rare by design — a missing voice key, a degraded link.
        el.status.textContent = event.text;
        break;
    }
  }

  function connectSocket() {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    state.socket = new WebSocket(proto + '//' + location.host + '/ws');

    // Note: no auto-connect here. The socket being up is not the same as
    // someone being on the line.
    state.socket.onmessage = function (message) {
      try { handle(JSON.parse(message.data)); } catch (e) { /* malformed frame */ }
    };
    state.socket.onclose = function () {
      setState('idle');
      setTimeout(connectSocket, 1500);
    };
    state.socket.onerror = function () { try { state.socket.close(); } catch (e) {} };
  }

  /* --- speech in ---------------------------------------------------------- */

  function submitAudio(pcm) {
    if (!state.connectedId) return;
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
        // Flagged as spoken so the server can reject it if it is the contact's
        // own voice arriving back through the microphone.
        send({ type: 'prompt', text: text, spoken: true });
      })
      .catch(function () { setState('idle'); });
  }

  function setMode(mode) {
    state.mode = mode;
    el['mode-ptt'].classList.toggle('is-on', mode === 'ptt');
    el['mode-ambient'].classList.toggle('is-on', mode === 'ambient');
    el.ptt.dataset.ambient = mode === 'ambient' ? '1' : '0';
    el.ptt.title = mode === 'ambient'
      ? 'Listening — click to send now'
      : 'Hold to speak (or hold Space)';
    ConsoleMic.setMode(mode).catch(function () {
      if (mode === 'ambient') setMode('ptt');
    });
  }

  /* --- wiring ------------------------------------------------------------- */

  function wireInput() {
    el.compose.addEventListener('submit', function (e) {
      e.preventDefault();
      var text = el.input.value.trim();
      if (!text || !state.connectedId) return;
      ConsoleAudio.stop();
      el.input.value = '';
      send({ type: 'prompt', text: text });
    });

    el.ptt.addEventListener('pointerdown', function (e) {
      e.preventDefault();
      if (!state.connectedId) return;
      // In ambient mode the button means "that's it, go".
      if (state.mode === 'ambient') { ConsoleMic.cut(); return; }
      ConsoleAudio.stop();
      setState('listening');
      ConsoleMic.pushStart();
    });
    ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (name) {
      el.ptt.addEventListener(name, function () {
        if (state.mode !== 'ambient') ConsoleMic.pushStop();
      });
    });

    el['mode-ptt'].addEventListener('click', function () { setMode('ptt'); });
    el['mode-ambient'].addEventListener('click', function () {
      if (state.connectedId) setMode('ambient');
    });

    el.lock.addEventListener('click', function () {
      if (state.connectedId) hangUp();
      ConsoleBoot.lock();
      startBoot();
    });

    window.addEventListener('keydown', function (e) {
      if (document.documentElement.dataset.phase !== 'live') return;
      if (e.key === 'Escape') { ConsoleAudio.stop(); return; }
      if (e.code !== 'Space' || state.spaceDown) return;
      if (document.activeElement === el.input) return;
      if (state.mode !== 'ptt' || !state.connectedId) return;
      e.preventDefault();
      state.spaceDown = true;
      ConsoleAudio.stop();
      setState('listening');
      ConsoleMic.pushStart();
    });
    window.addEventListener('keyup', function (e) {
      if (e.code !== 'Space' || !state.spaceDown) return;
      state.spaceDown = false;
      ConsoleMic.pushStop();
    });
  }

  function wireAudio() {
    ConsoleAudio.on('onSentenceStart', function (text, index, durationMs) {
      setState('speaking');
      revealSentence(text, index, durationMs);
    });
    ConsoleAudio.on('onIdle', function () {
      if (document.documentElement.dataset.state === 'speaking') setState('idle');
      if (state.generationDone) scheduleFlush(40);
    });
  }

  function wireMic() {
    ConsoleMic.on('onLevel', function (level) { if (state.viz) state.viz.setLevel(level); });
    ConsoleMic.on('onUtterance', submitAudio);
    ConsoleMic.on('onSpeechStart', function () { setState('listening'); });
    ConsoleMic.on('onSpeechEnd', function () {
      if (document.documentElement.dataset.state === 'listening') setState('idle');
    });
    ConsoleMic.on('onBargeIn', function () {
      // Talking over a reply cuts it off, the way interrupting a person does.
      ConsoleAudio.stop();
    });
    ConsoleMic.on('onError', function () {
      el.status.textContent = 'Microphone unavailable';
    });
  }

  /* --- session ------------------------------------------------------------ */

  function loadSession() {
    return fetch('/api/session').then(function (r) { return r.json(); }).then(function (info) {
      (info.contacts || []).forEach(function (contact) {
        state.contacts[contact.id] = contact;
        state.order.push(contact.id);
      });
      renderDirectory();
      return info;
    });
  }

  function startBoot() {
    ConsoleBoot.start({
      contactCount: state.order.length,
      onAuthenticated: function () {
        el.input.focus();
        if (!state.socket || state.socket.readyState > WebSocket.OPEN) connectSocket();
      }
    });
  }

  /* --- clock -------------------------------------------------------------- */

  setInterval(function () {
    var now = new Date();
    el.clock.textContent = [now.getHours(), now.getMinutes(), now.getSeconds()]
      .map(function (n) { return String(n).padStart(2, '0'); }).join(':');
  }, 1000);

  /* --- go ----------------------------------------------------------------- */

  cacheElements();
  state.viz = new Visualizer($('viz'));
  setState('idle');
  setLink('off');
  wireInput();
  wireAudio();
  wireMic();
  loadSession().then(startBoot, startBoot);
})();
