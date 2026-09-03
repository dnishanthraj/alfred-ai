/* ==========================================================================
   Console client.

   Owns everything presentational: the socket, the directory, the transcript,
   and the two input modes. The server sends engine events and mp3 bytes;
   nothing here decides what a contact says.

   The transcript is revealed *in time with the speech* rather than printed the
   moment the model finishes writing. A reply that appears on screen before it
   is spoken reads as a chat log with a voice bolted on; one that appears as it
   is said reads as someone talking. Words are released across each sentence's
   real audio duration, weighted by length so longer words take longer.
   ========================================================================== */

(function () {
  'use strict';

  var el = {};
  var state = {
    operator: 'Operator',
    contacts: {},
    currentId: null,
    turn: null,          // the assistant turn element being spoken into
    pending: {},         // sentences awaiting audio, by index
    rendered: {},        // sentence indices already on screen
    socket: null,
    viz: null,
    mode: 'ptt',
    spaceDown: false,
    generationDone: true,
    flushTimer: null
  };

  var STATE_COPY = {
    idle:         ['Standby',      'Awaiting input'],
    listening:    ['Receiving',    'Channel open'],
    transcribing: ['Resolving',    'Parsing speech'],
    searching:    ['Scanning',     'Querying the grid'],
    thinking:     ['Processing',   'Composing reply'],
    speaking:     ['Transmitting', 'Voice synthesis active']
  };

  function $(id) { return document.getElementById(id); }

  function cacheElements() {
    ['log', 'input', 'compose', 'ptt', 'clock', 'link-state', 'bar-title', 'book',
     'lock', 'viz-state', 'viz-sub', 'rd-operator', 'rd-model', 'rd-stt', 'rd-voice',
     'now-name', 'now-role', 'now-tag', 'mode-ptt', 'mode-ambient'
    ].forEach(function (id) { el[id] = $(id); });
  }

  /* --- transcript -------------------------------------------------------- */

  function atBottom() {
    return el.log.scrollHeight - el.log.scrollTop - el.log.clientHeight < 90;
  }

  function scroll(force) {
    if (force || atBottom()) el.log.scrollTop = el.log.scrollHeight;
  }

  function contactName() {
    var c = state.contacts[state.currentId];
    return c ? c.name : 'Contact';
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

  function addSources(items) {
    if (!items || !items.length) return;
    var stick = atBottom();
    var wrap = document.createElement('div');
    wrap.className = 'sources';

    var label = document.createElement('span');
    label.className = 'sources__label';
    label.textContent = 'Sources';

    var list = document.createElement('ul');
    list.className = 'sources__list';
    items.slice(0, 4).forEach(function (item) {
      if (!item.url) return;
      var li = document.createElement('li');
      var a = document.createElement('a');
      a.href = item.url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.textContent = item.title || item.url;
      li.appendChild(a);
      list.appendChild(li);
    });

    if (!list.children.length) return;
    wrap.appendChild(label);
    wrap.appendChild(list);
    el.log.appendChild(wrap);
    scroll(stick);
  }

  function assistantTurn() {
    if (!state.turn) {
      state.turn = addTurn('assistant', contactName(), '');
      state.turn.classList.add('turn--speaking');
    }
    return state.turn;
  }

  function closeTurn() {
    if (state.turn) state.turn.classList.remove('turn--speaking');
    state.turn = null;
    state.pending = {};
    state.rendered = {};
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

    var body = assistantTurn().querySelector('.log__text');
    var span = document.createElement('span');
    span.className = 'said';
    if (body.textContent) span.textContent = ' ';
    body.appendChild(span);

    var words = text.split(/\s+/).filter(Boolean);
    if (!words.length) return;

    var weights = words.map(function (w) { return w.length + 1; });
    var total = weights.reduce(function (a, b) { return a + b; }, 0);
    // Aim slightly short of the clip so the last word lands before silence.
    var budget = Math.max(durationMs * 0.92, 220);
    var elapsed = 0;

    words.forEach(function (word, i) {
      var at = elapsed;
      setTimeout(function () {
        span.textContent += (i === 0 ? '' : ' ') + word;
        scroll(false);
      }, at);
      elapsed += (weights[i] / total) * budget;
    });
  }

  /**
   * The flush is always deferred by a beat, which means it can still be in
   * flight when the next turn begins — stopping playback to send a new prompt
   * schedules one, and it would land after the new reply's first sentence had
   * already arrived, flushing and closing a turn that was still being spoken
   * into. Any new content cancels a pending flush.
   */
  function scheduleFlush(delay) {
    cancelFlush();
    state.flushTimer = setTimeout(flushUnspoken, delay);
  }

  function cancelFlush() {
    if (state.flushTimer) {
      clearTimeout(state.flushTimer);
      state.flushTimer = null;
    }
  }

  /**
   * Put anything that will never be spoken on screen — no voice configured, or
   * synthesis failed for that sentence — and settle the turn.
   *
   * This waits on playback, not just generation: `turn_complete` means every
   * clip has been *made*, but the first may not have started playing, and
   * flushing then would print the whole reply for the audio to reveal again.
   */
  function flushUnspoken() {
    state.flushTimer = null;
    if (ConsoleAudio.isPlaying) return;   // settle once the audio has drained
    Object.keys(state.pending)
      .map(Number)
      .sort(function (a, b) { return a - b; })
      .forEach(function (index) {
        revealSentence(state.pending[index], index, 0);
      });
    closeTurn();
  }

  /* --- state ------------------------------------------------------------- */

  function setState(value) {
    document.documentElement.dataset.state = value;
    var copy = STATE_COPY[value] || STATE_COPY.idle;
    el['viz-state'].textContent = copy[0];
    el['viz-sub'].textContent = copy[1];
    if (state.viz) state.viz.setMode(value);
  }

  /* --- directory --------------------------------------------------------- */

  function renderDirectory(contacts) {
    el.book.innerHTML = '';
    contacts.forEach(function (contact) {
      state.contacts[contact.id] = contact;

      var li = document.createElement('li');
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'book__item';
      button.style.setProperty('--contact-accent', contact.accent);
      button.dataset.available = contact.available ? '1' : '0';
      button.setAttribute('aria-current', String(contact.id === state.currentId));

      var dot = document.createElement('span');
      dot.className = 'book__dot';

      var text = document.createElement('span');
      var name = document.createElement('span');
      name.className = 'book__name';
      name.textContent = contact.name;
      var role = document.createElement('span');
      role.className = 'book__role';
      role.textContent = contact.available ? contact.role : 'Unavailable';
      text.appendChild(name);
      text.appendChild(role);

      button.appendChild(dot);
      button.appendChild(text);
      button.addEventListener('click', function () { connect(contact.id); });

      li.appendChild(button);
      el.book.appendChild(li);
    });
  }

  function setCurrent(contactId) {
    var contact = state.contacts[contactId];
    if (!contact) return;
    state.currentId = contactId;

    document.documentElement.style.setProperty('--contact-accent', contact.accent);
    el['bar-title'].textContent = contact.full_name;
    el['now-name'].textContent = contact.full_name;
    el['now-role'].textContent = contact.role;
    el['now-tag'].textContent = contact.tagline || '';
    el['rd-model'].textContent = contact.model;
    el['rd-voice'].textContent = contact.voice ? 'ElevenLabs' : 'Unavailable';
    el['rd-voice'].dataset.ok = contact.voice ? '1' : '0';
    document.title = contact.name + ' · WayneTech Console';

    Array.prototype.forEach.call(el.book.querySelectorAll('.book__item'), function (b, i) {
      var id = Object.keys(state.contacts)[i];
      b.setAttribute('aria-current', String(id === contactId));
    });
  }

  function connect(contactId) {
    if (contactId === state.currentId) return;
    ConsoleAudio.stop();
    closeTurn();
    el.log.innerHTML = '';
    setCurrent(contactId);
    send({ type: 'connect', id: contactId });
  }

  /* --- socket ------------------------------------------------------------ */

  function send(payload) {
    if (state.socket && state.socket.readyState === WebSocket.OPEN) {
      state.socket.send(JSON.stringify(payload));
    }
  }

  function handle(event) {
    switch (event.type) {
      case 'state':
        // Playback owns the speaking state; the engine going idle mid-clip
        // must not cut the visualizer short.
        if (!(ConsoleAudio.isPlaying && event.value === 'idle')) setState(event.value);
        break;

      case 'contact':
        setCurrent(event.id);
        break;

      case 'message':
        if (event.role === 'user') {
          cancelFlush();
          closeTurn();
          addTurn('user', state.operator, event.text);
        }
        break;

      case 'reply_start':
        cancelFlush();
        closeTurn();
        state.generationDone = false;
        break;

      case 'sentence':
        // Held, not rendered: it appears when its audio starts.
        cancelFlush();
        state.generationDone = false;
        state.pending[event.index] = event.text;
        assistantTurn();
        break;

      case 'speak':
        ConsoleAudio.enqueue(event.audio_id, event.text, event.index);
        break;

      case 'sources':
        addSources(event.items);
        break;

      case 'reply_end':
        break;   // audio may still be in flight; wait for turn_complete

      case 'turn_complete':
        // Generation and synthesis are both finished. If nothing is playing —
        // no voice, or every clip failed — settle now; otherwise the audio
        // finishing will do it.
        state.generationDone = true;
        scheduleFlush(60);
        break;

      case 'notice':
        addTurn('system', 'System', event.text, event.level);
        break;
    }
  }

  function connectSocket() {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    state.socket = new WebSocket(proto + '//' + location.host + '/ws');

    state.socket.onopen = function () {
      el['link-state'].textContent = 'Link Active';
      el['link-state'].dataset.up = '1';
      send({ type: 'connect', id: state.currentId });
    };
    state.socket.onmessage = function (message) {
      try { handle(JSON.parse(message.data)); } catch (e) { /* malformed frame */ }
    };
    state.socket.onclose = function () {
      el['link-state'].textContent = 'Reconnecting';
      el['link-state'].dataset.up = '0';
      setState('idle');
      setTimeout(connectSocket, 1500);
    };
    state.socket.onerror = function () { try { state.socket.close(); } catch (e) {} };
  }

  /* --- speech in --------------------------------------------------------- */

  function submitAudio(pcm) {
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

  function setMode(mode) {
    state.mode = mode;
    el['mode-ptt'].classList.toggle('is-on', mode === 'ptt');
    el['mode-ambient'].classList.toggle('is-on', mode === 'ambient');
    el.ptt.dataset.ambient = mode === 'ambient' ? '1' : '0';
    el.ptt.title = mode === 'ambient'
      ? 'Listening — click to send now'
      : 'Hold to speak (or hold Space)';
    ConsoleMic.setMode(mode).catch(function () {
      // Permission refused — fall back rather than leaving a dead toggle.
      if (mode === 'ambient') setMode('ptt');
    });
  }

  /* --- wiring ------------------------------------------------------------ */

  function wireInput() {
    el.compose.addEventListener('submit', function (e) {
      e.preventDefault();
      var text = el.input.value.trim();
      if (!text) return;
      ConsoleAudio.stop();
      el.input.value = '';
      send({ type: 'prompt', text: text });
    });

    el.ptt.addEventListener('pointerdown', function (e) {
      e.preventDefault();
      // In ambient mode the button means "that's it, go" — ending the take now
      // instead of waiting out the silence hangover.
      if (state.mode === 'ambient') {
        ConsoleMic.cut();
        return;
      }
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
    el['mode-ambient'].addEventListener('click', function () { setMode('ambient'); });

    el.lock.addEventListener('click', function () {
      ConsoleAudio.stop();
      ConsoleMic.close();
      setMode('ptt');
      ConsoleBoot.lock();
      startBoot();
    });

    // Hold Space to talk, but never while typing or locked.
    window.addEventListener('keydown', function (e) {
      if (document.documentElement.dataset.phase !== 'live') return;
      if (e.key === 'Escape') { ConsoleAudio.stop(); return; }
      if (e.code !== 'Space' || state.spaceDown) return;
      if (document.activeElement === el.input) return;
      if (state.mode !== 'ptt') return;
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
      // Audio draining is only the end of the turn if the model has also
      // stopped writing — otherwise a sentence still in synthesis would land
      // in a fresh bubble and split the reply in two.
      if (state.generationDone) scheduleFlush(40);
    });
  }

  function wireMic() {
    ConsoleMic.on('onLevel', function (level) {
      if (state.viz) state.viz.setLevel(level);
    });
    ConsoleMic.on('onUtterance', submitAudio);
    ConsoleMic.on('onSpeechStart', function () { setState('listening'); });
    ConsoleMic.on('onSpeechEnd', function () {
      if (document.documentElement.dataset.state === 'listening') setState('idle');
    });
    ConsoleMic.on('onBargeIn', function () {
      // Talking over a reply cuts it off, the way interrupting a person does.
      ConsoleAudio.stop();
      closeTurn();
    });
    ConsoleMic.on('onError', function (err) {
      addTurn('system', 'System', 'Microphone unavailable: ' + err.message, 'error');
    });
  }

  /* --- session ----------------------------------------------------------- */

  function loadSession() {
    return fetch('/api/session').then(function (r) { return r.json(); }).then(function (info) {
      state.operator = info.operator || 'Operator';
      el['rd-operator'].textContent = state.operator;
      el['rd-stt'].textContent = info.stt || '—';

      renderDirectory(info.contacts || []);
      state.currentId = info.current || info.default || (info.contacts[0] || {}).id;
      if (state.currentId) setCurrent(state.currentId);

      (info.transcript || []).forEach(function (turn) {
        if (turn.role === 'system') {
          addTurn('system', 'System', turn.text, turn.level);
        } else if (turn.role === 'sources') {
          addSources(turn.items);
        } else {
          addTurn(turn.role, turn.role === 'user' ? state.operator : contactName(), turn.text);
        }
      });
      scroll(true);
      return info;
    });
  }

  function startBoot() {
    ConsoleBoot.start({
      contactCount: Object.keys(state.contacts).length,
      onAuthenticated: function () {
        el.input.focus();
        if (!state.socket || state.socket.readyState > WebSocket.OPEN) connectSocket();
      }
    });
  }

  /* --- clock ------------------------------------------------------------- */

  setInterval(function () {
    var now = new Date();
    el.clock.textContent = [now.getHours(), now.getMinutes(), now.getSeconds()]
      .map(function (n) { return String(n).padStart(2, '0'); }).join(':');
  }, 1000);

  /* --- go ---------------------------------------------------------------- */

  cacheElements();
  state.viz = new Visualizer($('viz'));
  setState('idle');
  wireInput();
  wireAudio();
  wireMic();

  loadSession().then(startBoot, function () {
    addTurn('system', 'System', 'Could not reach the console API.', 'error');
    startBoot();
  });
})();
