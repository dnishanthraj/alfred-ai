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
    ringingId: null,     // who is being reached, not yet answered
    pending: {},         // sentences awaiting audio, by key
    pendingSpeaker: {},  // who said each of them, on a call with company
    rendered: {},        // sentence keys already shown
    fresh: true,         // next sentence starts a new utterance
    socket: null,
    viz: null,
    mode: 'ptt',
    spaceDown: false,
    generationDone: true,
    flushTimer: null,
    wordTimers: [],   // pending word reveals, cancellable mid-sentence
    idleTimer: null,
    resumeTimer: null,
    nudges: 0,        // consecutive unanswered check-ins
    closing: false,   // he is signing off; hang up once he has finished
    hangUpWhenQuiet: false,  // sign-off written; waiting on the voice to stop
    quietUntil: 0,    // he was asked for time; don't chase until then
    askedLast: false, // his last reply ended on a question
    party: [],        // who is on the call, in order of joining
    lastSpeaker: null // who said the line on screen, on a call with company
  };

  // Held keys that count as push-to-talk. Space is the obvious one; Right
  // Command sits under the thumb and is the only modifier that isn't already
  // spoken for by the browser.
  var PTT_CODES = { Space: true, MetaRight: true };

  // How long a silence runs before he breaks it: [minimum, random spread] ms.
  //
  // Modelled on how silence works on a real call. A question left unanswered is
  // the one silence people do not sit through — replies normally land within a
  // fraction of a second, so after ten seconds or so with nothing, anyone
  // would prod. Otherwise this is an open line, and on an open line nobody is
  // obliged to talk: a lull of half a minute or a minute is ordinary, and what
  // ends it is usually one of them mentioning something, not "are you still
  // there?". That question comes only after a long second silence, and some
  // minutes after that he rings off himself, because nobody sits on a dead
  // line forever. What he says each time is decided on the server.
  //
  // The old windows (26–48s, then 17–29s, then hang up) closed the call after
  // about a minute and a half of quiet, and every nudge was the same question.
  // Contacts on one call at once: whoever was rung, and two more.
  var MAX_PARTY = 3;

  var IDLE_WINDOWS = [
    [30000, 30000],   // first lull: he says something of his own
    [75000, 60000],   // a long second silence: now he asks if you're there
    [100000, 80000]   // and then he closes the call
  ];
  var AFTER_QUESTION = [9000, 6000];   // he asked and you went quiet
  var MAX_NUDGES = IDLE_WINDOWS.length - 1;

  // When he says he needs a moment, he takes one — and comes back on his own.
  var HE_ASKED_FOR_TIME = /\b(give me|just|hold on|hang on|one|bear with)\s*(a\s*)?(moment|minute|second|sec|mo)\b|\blet me (think|check|see)\b/i;
  var RESUME_MIN_MS = 6000;
  var RESUME_SPREAD_MS = 7000;

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
     'heard', 'utterance', 'status', 'empty', 'viz-wrap', 'ringing-label',
     'mode-ptt', 'mode-ambient', 'dossier', 'dossier-close', 'dossier-name',
     'dossier-role', 'dossier-text', 'dossier-save', 'dossier-saved',
     'dossier-portrait'].forEach(function (id) { el[id] = $(id); });
  }

  /* --- the spoken line ---------------------------------------------------- */

  function clearWordTimers() {
    state.wordTimers.forEach(clearTimeout);
    state.wordTimers = [];
  }

  function clearUtterance() {
    clearWordTimers();
    el.utterance.textContent = '';
    state.pending = {};
    state.pendingSpeaker = {};
    state.rendered = {};
  }

  /**
   * Cut him off mid-sentence.
   *
   * The words are revealed on timers spread across the clip, so stopping the
   * audio alone left them firing — he fell silent while the rest of what he
   * would have said carried on appearing. Now the line stops where his voice
   * did, with a dash, and the remainder is never shown: it was never heard.
   */
  function cutOff() {
    clearWordTimers();
    state.pending = {};
    var text = el.utterance.textContent.replace(/[\s—-]+$/, '');
    if (text) el.utterance.textContent = text + '—';
    state.fresh = true;
  }

  function wasCutOff() {
    return el.utterance.textContent.trimEnd().endsWith('—');
  }

  /** Stop him talking, the way interrupting a person does. */
  function interruptHim() {
    if (!ConsoleAudio.isPlaying) return;
    ConsoleAudio.stop();
    cutOff();
  }

  function showHeard(text) {
    el.heard.textContent = text ? '“' + text + '”' : '';
    el.heard.dataset.show = text ? '1' : '0';
  }

  /**
   * Release a sentence's words in time with its audio.
   *
   * `words` is when each word actually starts, from the synthesiser's own
   * alignment, and is used whenever it matches the text. Without it the words
   * are spread across the clip weighted by length — a fair guess for a plain
   * read, and a poor one once the voice can sigh first: the breath comes before
   * the first word, and the guess put the words out ahead of it.
   *
   * Keyed by a per-call sentence key from the server, never by position in a
   * reply. Positions restart at 0 for every reply, so the first sentence of an
   * answer that followed "One moment." looked already shown and never was.
   */
  function revealSentence(text, key, durationMs, timings, speaker) {
    if (state.rendered[key]) return;
    state.rendered[key] = true;
    delete state.pending[key];

    // On a call with company, a new voice starts a new line, labelled with who
    // it is, and the instrument takes their colour.
    var group = state.party.length > 1;
    if (speaker && state.contacts[speaker]) {
      document.documentElement.style.setProperty('--contact-accent', state.contacts[speaker].accent);
    }
    if (group && speaker && speaker !== state.lastSpeaker) state.fresh = true;
    state.lastSpeaker = speaker || state.lastSpeaker;

    // The first sentence of a reply replaces whatever was said before it.
    if (state.fresh) {
      el.utterance.textContent = '';
      state.fresh = false;
      if (group && speaker && state.contacts[speaker]) {
        var label = document.createElement('span');
        label.className = 'speaker';
        label.textContent = state.contacts[speaker].name;
        label.style.color = state.contacts[speaker].accent;
        el.utterance.appendChild(label);
      }
    }

    var span = document.createElement('span');
    span.className = 'said';
    if (el.utterance.textContent) span.textContent = ' ';
    el.utterance.appendChild(span);

    var words = text.split(/\s+/).filter(Boolean);
    if (!words.length) return;

    if (timings && timings.length === words.length) {
      words.forEach(function (word, i) {
        state.wordTimers.push(setTimeout(function () {
          span.textContent += (i === 0 ? '' : ' ') + word;
        }, Math.max(0, timings[i][1])));
      });
      return;
    }

    var weights = words.map(function (w) { return w.length + 1; });
    var total = weights.reduce(function (a, b) { return a + b; }, 0);
    // Aim slightly short of the clip so the last word lands before silence.
    var budget = Math.max(durationMs * 0.92, 200);
    var elapsed = 0;

    words.forEach(function (word, i) {
      state.wordTimers.push(setTimeout(function () {
        span.textContent += (i === 0 ? '' : ' ') + word;
      }, elapsed));
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
      .forEach(function (key) {
        revealSentence(state.pending[key], key, 0, null, state.pendingSpeaker[key]);
      });
  }

  /* --- presence -------------------------------------------------------------
     Silence is part of a conversation, but an unbounded silence is just a dead
     line. After a while he asks — once, then once more, then leaves you alone.
     Any activity resets it, and asking him for a minute buys you one.
     ------------------------------------------------------------------------ */

  function armIdleCheck() {
    clearTimeout(state.idleTimer);
    if (!state.connectedId) return;

    // Past the last window he stops asking and hangs up instead.
    var window_ = (state.nudges === 0 && state.askedLast)
      ? AFTER_QUESTION : IDLE_WINDOWS[Math.min(state.nudges, IDLE_WINDOWS.length - 1)];
    var closing = state.nudges >= MAX_NUDGES;
    var wait = window_[0] + Math.random() * window_[1];
    var quietFor = state.quietUntil - Date.now();
    if (quietFor > 0) wait += quietFor;

    state.idleTimer = setTimeout(function () {
      if (!state.connectedId || ConsoleAudio.isPlaying) { armIdleCheck(); return; }
      if (closing) {
        state.closing = true;
        send({ type: 'signoff' });
        return;
      }
      state.nudges += 1;
      send({ type: 'nudge' });
    }, wait);
  }

  /* He decided to end the call. Let him land the sentence first: hanging up
     over his own voice is the one thing that would make it obvious the timing
     is a timer rather than a person. Interrupting still cancels it — cutting
     him off is your prerogative, not the clock's. */
  function closeIfFinished() {
    if (!state.hangUpWhenQuiet) return;
    if (ConsoleAudio.isPlaying) return;            // still talking; wait for onIdle
    state.hangUpWhenQuiet = false;
    // A beat after the last word, the way anyone pauses before ringing off.
    setTimeout(function () { if (state.connectedId) hangUp(); }, 700);
  }

  function noteActivity(opts) {
    // Anything from the operator cancels a pending hang-up — he was leaving
    // because nobody was there, and now somebody is.
    state.hangUpWhenQuiet = false;
    state.nudges = 0;
    if (opts && opts.quietFor) state.quietUntil = Date.now() + opts.quietFor;
    armIdleCheck();
  }

  // "Give me a minute" should buy a minute, not be answered and then chased.
  var ASK_FOR_TIME = /\b(give me|hold on|hang on|one|just a|wait a|need a)\s*(a\s*)?(second|sec|minute|min|moment|mo)\b|\bhold on\b|\bone moment\b/i;

  function requestedTime(text) {
    if (!ASK_FOR_TIME.test(text || '')) return 0;
    return /\bminute|\bmin\b/i.test(text) ? 90000 : 45000;
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

  /* A contact's portrait, as a background stack: the supplied image if there
     is one, the silhouette if not, cropped by the profile's framing. Shared by
     the directory and the personnel file. */
  function portraitStyle(node, contact, fallbackPosition) {
    var base = '/static/portraits/' + contact.id;
    node.style.backgroundImage =
      "url('" + base + ".png'), url('" + base + ".jpg'), url('" + base + ".webp'), " +
      "url('/static/portraits/_silhouette.svg')";
    var frame = contact.portrait || {};
    node.style.backgroundSize = frame.size
      ? [frame.size, frame.size, frame.size, 'cover'].join(', ') : '';
    node.style.backgroundPosition = frame.position
      ? [frame.position, frame.position, frame.position, fallbackPosition].join(', ') : '';
  }

  function onCall(id) { return state.party.indexOf(id) !== -1; }

  function renderDirectory() {
    el.book.innerHTML = '';
    var inCall = state.party.length > 0;
    var group = state.party.length > 1;
    state.order.forEach(function (id) {
      var contact = state.contacts[id];
      var live = onCall(id) && state.ringingId !== id;
      var ringing = state.ringingId === id;

      var li = document.createElement('li');
      li.className = 'book__item';
      li.dataset.live = live ? '1' : '0';
      if (ringing) li.dataset.state = 'ringing';
      li.style.setProperty('--contact-accent', contact.accent);

      var row = document.createElement('div');
      row.className = 'book__row';

      // Portrait, with the status dot sitting on its edge.
      var avatar = document.createElement('span');
      avatar.className = 'book__avatar';
      portraitStyle(avatar, contact, 'center 22%');
      var dot = document.createElement('span');
      dot.className = 'book__dot';
      avatar.appendChild(dot);

      var text = document.createElement('button');
      text.type = 'button';
      text.className = 'book__open';
      text.title = 'Open personnel file';
      var name = document.createElement('span');
      name.className = 'book__name';
      name.textContent = contact.name;
      var role = document.createElement('span');
      role.className = 'book__role';
      role.textContent = ringing ? 'Connecting'
                       : live ? (group ? 'On the call' : 'Connected')
                       : (contact.available ? contact.role : 'Unavailable');
      text.appendChild(name);
      text.appendChild(role);
      text.addEventListener('click', function () {
        el.dossier.dataset.contact = id;
        openDossier(id);
      });

      row.appendChild(avatar);
      row.appendChild(text);

      // One button, whose job depends on the call: ring them, add them to the
      // call in progress, let them off it, or end it.
      var call = document.createElement('button');
      call.type = 'button';
      call.className = 'book__call';
      var action;
      if (!inCall) action = 'call';
      else if (onCall(id)) action = group ? 'drop' : 'end';
      else action = 'add';
      call.dataset.live = (action === 'end' || action === 'drop') ? '1' : '0';
      call.textContent = ringing ? 'Cancel'
                       : { call: 'Call', add: 'Add to call', drop: 'Drop', end: 'End' }[action];
      call.disabled = (action === 'call' || action === 'add')
        && (!contact.available || (action === 'add' && state.party.length >= MAX_PARTY));
      call.addEventListener('click', function () {
        if (action === 'call') placeCall(id);
        else if (action === 'add') addToCall(id);
        else if (action === 'drop') send({ type: 'drop', id: id });
        else hangUp();
      });

      li.appendChild(row);
      li.appendChild(call);
      el.book.appendChild(li);
    });
  }

  /* Patch someone into the call in progress. The console announces it and
     the line rings while they're reached; the server does the rest. */
  function addToCall(id) {
    if (!state.connectedId || onCall(id)) return;
    noteActivity();
    send({ type: 'add', id: id });
  }

  function placeCall(contactId) {
    var contact = state.contacts[contactId];
    if (!contact) return;
    // Already on a call with someone else: hang that up properly first — the
    // end tone, the console's line — then ring the new one.
    if (state.connectedId && state.connectedId !== contactId) {
      hangUp({ switching: true });
      setTimeout(function () { placeCall(contactId); }, 900);
      return;
    }
    state.currentId = contactId;
    state.ringingId = contactId;
    state.connectedId = contactId;   // input is accepted while it rings
    state.party = [contactId];
    state.lastSpeaker = null;
    el['bar-title'].textContent = contact.full_name;
    document.title = contact.name + ' · WayneTech Console';

    clearUtterance();
    showHeard('');
    state.fresh = true;
    state.nudges = 0;
    state.closing = false;
    state.hangUpWhenQuiet = false;
    state.quietUntil = 0;

    // Ring while he is being reached. This is not only dressing: it covers the
    // seconds the model spends loading, so the wait reads as a call connecting
    // rather than as software thinking about it.
    el['ringing-label'].textContent = 'Connecting to ' + contact.name + '…';
    setLink('ringing');
    setState('idle');
    // Connect first, so the model loads while the console announces the call;
    // the ring follows the announcement rather than talking over it.
    send({ type: 'connect', id: contactId });
    ConsoleSystem.say('call', contactId).then(function () {
      if (state.ringingId === contactId) ConsoleTones.startRinging();
    });
    el.input.focus();
  }

  /** He has picked up: stop the ring, mark the link live, go blue. */
  function answered(speaker) {
    // Someone added to a call in progress has picked up.
    if (speaker && state.ringingId === speaker && document.documentElement.dataset.link === 'on') {
      ConsoleSystem.stop();
      ConsoleTones.connected();
      state.ringingId = null;
      renderDirectory();
      return;
    }
    if (document.documentElement.dataset.link === 'on') return;
    ConsoleSystem.stop();   // he's picked up; the machine stops talking
    ConsoleTones.connected();
    state.ringingId = null;
    var contact = state.contacts[state.connectedId];
    if (contact) {
      document.documentElement.style.setProperty('--contact-accent', contact.accent);
    }
    setLink('on');
    armIdleCheck();
  }

  /* The line-up changed: someone was added, or let go. */
  function onParty(event) {
    state.party = event.members || [];
    var names = state.party.map(function (id) {
      return (state.contacts[id] || {}).name || id;
    });
    if (state.party.length) el['bar-title'].textContent = state.party.length > 1
      ? names.join(' · ')
      : (state.contacts[state.party[0]] || {}).full_name || '';
    if (event.added) {
      state.ringingId = event.added;
      ConsoleSystem.say('add', event.added).then(function () {
        if (state.ringingId === event.added) ConsoleTones.startRinging();
      });
    }
    if (event.removed) {
      if (state.ringingId === event.removed) { state.ringingId = null; ConsoleTones.stopRinging(); }
      if (state.connectedId === event.removed) state.connectedId = state.party[0] || null;
      ConsoleSystem.say('drop', event.removed);
    }
    renderDirectory();
  }

  function hangUp(opts) {
    ConsoleAudio.stop();
    ConsoleTones.stopRinging();
    ConsoleTones.disconnected();
    ConsoleSystem.say('end');
    ConsoleMic.close();
    setMode('ptt');
    clearTimeout(state.idleTimer);
    clearTimeout(state.resumeTimer);
    state.closing = false;
    state.hangUpWhenQuiet = false;
    // A switch tells the server by connecting to someone else, so the call it
    // ends can be remembered as cut short rather than simply over.
    if (!(opts && opts.switching)) send({ type: 'disconnect' });
    state.connectedId = null;
    state.ringingId = null;
    state.party = [];
    state.lastSpeaker = null;
    el['bar-title'].textContent = '';
    document.title = 'WayneTech Console';
    setState('idle');

    // Flash the alert colour, then let everything fade before clearing, so the
    // line visibly closes instead of blinking out.
    setLink('ending');
    setTimeout(function () {
      clearUtterance();
      showHeard('');
      state.fresh = true;
      document.documentElement.style.removeProperty('--contact-accent');
      setLink('off');
    }, 480);
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
          noteActivity({ quietFor: requestedTime(event.text) });
          showHeard(event.text);
          // Clear his last line, so your new question isn't left sitting
          // against his answer to the previous one — except when you cut him
          // off, where the half-finished line is the whole point and should
          // stay until he says something new.
          if (!wasCutOff()) clearUtterance();
          state.fresh = true;
        }
        break;

      case 'reply_start':
        cancelFlush();
        state.generationDone = false;
        state.fresh = true;
        break;

      case 'sentence':
        // Held, not shown: it appears when its audio starts.
        cancelFlush();
        state.generationDone = false;
        state.pending[event.key] = event.text;
        state.pendingSpeaker[event.key] = event.speaker;
        answered(event.speaker);
        break;

      case 'speak':
        ConsoleAudio.enqueue(event.audio_id, event.text, event.index, event.words, event.speaker);
        break;

      case 'reply_end':
        // If he asked for a moment, he means it: nothing is expected of you
        // until he comes back of his own accord.
        clearTimeout(state.resumeTimer);
        if (!event.interim) state.askedLast = /\?\s*$/.test(event.text || '');
        if (!event.interim && HE_ASKED_FOR_TIME.test(event.text || '')) {
          clearTimeout(state.idleTimer);
          state.resumeTimer = setTimeout(function () {
            if (state.connectedId) send({ type: 'resume' });
          }, RESUME_MIN_MS + Math.random() * RESUME_SPREAD_MS);
        }
        break;

      case 'party':
        onParty(event);
        break;

      case 'call_ending':
        // He said goodbye and was answered: ring off once the voice stops.
        state.hangUpWhenQuiet = true;
        break;

      case 'turn_complete':
        state.generationDone = true;
        answered();
        scheduleFlush(60);
        if (state.closing) {
          // Hang up once he has actually finished speaking — not on a timer.
          // `turn_complete` means the model stopped *writing*; the audio queue
          // is still draining well behind it, so a fixed 2.6s wait cut him off
          // mid-sentence on anything longer than a single short line. The
          // handoff is in ConsoleAudio's idle callback instead.
          state.closing = false;
          state.hangUpWhenQuiet = true;
          closeIfFinished();
        } else if (state.hangUpWhenQuiet) {
          closeIfFinished();   // after a goodbye; waits for the voice if it's still going
        } else {
          armIdleCheck();
        }
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
        send({ type: 'prompt', text: text, spoken: true,
               confidence: typeof data.confidence === 'number' ? data.confidence : 1 });
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
      interruptHim();
      el.input.value = '';
      noteActivity({ quietFor: requestedTime(text) });
      send({ type: 'prompt', text: text });
    });

    el.ptt.addEventListener('pointerdown', function (e) {
      e.preventDefault();
      if (!state.connectedId) return;
      // In ambient mode the button means "that's it, go".
      if (state.mode === 'ambient') { ConsoleMic.cut(); return; }
      interruptHim();
      setState('listening');
      ConsoleMic.pushStart();
    });
    ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (name) {
      el.ptt.addEventListener(name, function () {
        if (state.mode !== 'ambient') ConsoleMic.pushStop();
      });
    });

    el['mode-ptt'].addEventListener('click', function () { setMode('ptt'); });
    // Ambient is withheld from the UI until the detector is reliable; the
    // handler stays so re-enabling it is a one-line change in the markup.
    el['mode-ambient'].addEventListener('click', function () {
      if (!el['mode-ambient'].disabled && state.connectedId) setMode('ambient');
    });

    el['dossier-close'].addEventListener('click', function () { el.dossier.hidden = true; });
    el['dossier-save'].addEventListener('click', saveDossier);
    el.dossier.addEventListener('click', function (e) {
      if (e.target === el.dossier) el.dossier.hidden = true;
    });

    el.lock.addEventListener('click', function () {
      if (state.connectedId) hangUp();
      ConsoleSystem.say('lock');
      ConsoleBoot.lock();
      startBoot();
    });

    window.addEventListener('keydown', function (e) {
      if (document.documentElement.dataset.phase !== 'live') return;
      if (e.key === 'Escape') { interruptHim(); return; }
      if (!PTT_CODES[e.code] || state.spaceDown) return;
      // Space types; Right Command does not, so only Space is blocked while
      // the composer has focus.
      if (e.code === 'Space' && document.activeElement === el.input) return;
      if (state.mode !== 'ptt' || !state.connectedId) return;
      e.preventDefault();
      state.spaceDown = true;
      interruptHim();
      setState('listening');
      ConsoleMic.pushStart();
    });
    window.addEventListener('keyup', function (e) {
      if (!PTT_CODES[e.code] || !state.spaceDown) return;
      state.spaceDown = false;
      ConsoleMic.pushStop();
    });

    // A held modifier can swallow its own keyup — switch apps mid-hold and the
    // release never arrives, leaving the microphone open indefinitely.
    ['blur', 'visibilitychange'].forEach(function (name) {
      window.addEventListener(name, function () {
        if (!state.spaceDown) return;
        state.spaceDown = false;
        ConsoleMic.pushStop();
      });
    });
  }

  function wireAudio() {
    ConsoleAudio.on('onSentenceStart', function (text, key, durationMs, words, speaker) {
      setState('speaking');
      revealSentence(text, key, durationMs, words, speaker);
    });
    ConsoleAudio.on('onIdle', function () {
      if (document.documentElement.dataset.state === 'speaking') setState('idle');
      if (state.generationDone) scheduleFlush(40);
      closeIfFinished();
    });
  }

  function wireSystem() {
    // What the console just said, in the status line for a moment.
    ConsoleSystem.on('onLine', function (text) {
      el.status.textContent = text;
      setTimeout(function () {
        if (el.status.textContent === text) {
          el.status.textContent = STATE_COPY[document.documentElement.dataset.state] || '';
        }
      }, 3200);
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
      interruptHim();
    });
    ConsoleMic.on('onError', function () {
      el.status.textContent = 'Microphone unavailable';
    });
  }

  /* --- dossier --------------------------------------------------------------
     Who this person is to you, in your own words. Stored per contact on the
     server so it survives a reload, and editable here because a relationship
     someone else wrote for you is not one you would recognise.
     ------------------------------------------------------------------------ */

  function openDossier(contactId) {
    var contact = state.contacts[contactId];
    if (!contact) return;
    el['dossier-name'].textContent = contact.full_name;
    el['dossier-role'].textContent = contact.role;
    el['dossier-saved'].dataset.show = '0';
    document.documentElement.style.setProperty('--contact-accent', contact.accent);

    // A supplied portrait wins; otherwise the generated silhouette stands in.
    // Stacked backgrounds need no load handlers: a layer whose URL 404s paints
    // nothing, and the silhouette is last. Cropped by the profile's framing.
    portraitStyle(el['dossier-portrait'], contact, 'center 22%');

    el['dossier-text'].value = 'Loading…';
    fetch('/api/contacts/' + contactId + '/bio')
      .then(function (r) { return r.json(); })
      .then(function (d) { el['dossier-text'].value = d.bio || ''; })
      .catch(function () { el['dossier-text'].value = ''; });

    el.dossier.hidden = false;
  }

  function saveDossier() {
    var id = el.dossier.dataset.contact;
    if (!id) return;
    fetch('/api/contacts/' + id + '/bio', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bio: el['dossier-text'].value })
    }).then(function () {
      el['dossier-saved'].textContent = 'Saved';
      el['dossier-saved'].dataset.show = '1';
      setTimeout(function () { el['dossier-saved'].dataset.show = '0'; }, 1800);
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
        ConsoleSystem.say('unlock');
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
  wireSystem();
  loadSession().then(startBoot, startBoot);
})();
