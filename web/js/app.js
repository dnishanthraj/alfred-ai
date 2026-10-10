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
    freshKeys: {},       // sentences that open a reply: each clears the line when it's heard
    replyFirst: false,   // the next sentence to arrive opens a reply
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
    messagesWith: null, // whose text thread is open
    unread: {},       // contacts with a text reply not yet seen
    pinged: {},       // groups where someone tagged him, not yet opened
    typing: {},       // contacts writing a reply
    thread: null,     // the open text thread: {id, messages, more, loading}
    lastSpeaker: null, // who said the line on screen, on a call with company
    incomingId: null, // who is calling him right now
    groups: {},       // group chats by id: {id, name, members, reads, last}
    seats: {},        // a call with company: contact id -> {node, viz, line}
    groupRinging: [], // rung together, not yet picked up (or turned down)
    groupTyping: {}   // group id -> {contact id: true} while they write
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
  var MAX_PARTY = 4;

  var IDLE_WINDOWS = [
    [30000, 30000],   // first lull: he says something of his own
    [75000, 60000],   // a long second silence: now he asks if you're there
    [100000, 80000]   // and then he closes the call
  ];
  var AFTER_QUESTION = [9000, 6000];   // he asked and you went quiet
  var MAX_NUDGES = IDLE_WINDOWS.length - 1;
  // On a call with company a pause is shorter before somebody fills it — or
  // lets it sit; the server decides which, and who.
  // On a call with company the next line comes quickly — a beat, not a silence
  // — while the call has go in it; the server decides whether anyone takes it.
  var GROUP_LULL = [700, 1500];
  var MAX_LULLS = 14;

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
     'dossier', 'dossier-close', 'dossier-name',
     'dossier-role', 'dossier-text', 'dossier-save', 'dossier-saved',
     'dossier-portrait', 'messages', 'messages-avatar', 'messages-name', 'messages-role',
     'messages-close', 'messages-tabs', 'messages-thread', 'messages-compose', 'messages-input', 'messages-emoji', 'messages-replying',
     'messages-resize', 'messages-who', 'rail-toggle', 'rail-resize', 'inbox', 'inbox-count',
     'toasts', 'dossier-call', 'dossier-message', 'incoming', 'incoming-avatar',
     'incoming-name', 'incoming-accept', 'incoming-decline', 'groups', 'group-new',
     'group-modal', 'group-form', 'group-name', 'group-people', 'group-cancel', 'group-create',
     'messages-delete', 'groups-wrap', 'group-info', 'group-info-name', 'group-info-members',
     'group-info-add', 'group-info-add-label', 'group-info-close', 'group-info-save', 'seats',
     'messages-call', 'call-pick', 'call-pick-form', 'call-pick-people', 'call-pick-cancel',
     'call-pick-go'].forEach(function (id) { el[id] = $(id); });
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
    state.freshKeys = {};
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
    state.freshKeys = {};
    var text = el.utterance.textContent.replace(/[\s—-]+$/, '');
    if (text) el.utterance.textContent = text + '—';
    // On a call with company, the one talked over stops mid-line under their own seat.
    var seat = state.seats[state.seatLast];
    if (seat) {
      var said = seat.line.textContent.replace(/[\s—-]+$/, '');
      if (said) seat.line.textContent = said + '—';
      seat.fresh = true;
    }
    state.fresh = true;
  }

  function wasCutOff() {
    return el.utterance.textContent.trimEnd().endsWith('—');
  }

  /** Stop him talking, the way interrupting a person does. */
  function interruptHim() {
    if (!ConsoleAudio.isPlaying) return;
    // Whoever he talked over is who he's answering, unless he names someone —
    // the voice he heard, not whoever the model had got to writing next.
    state.talkedOver = state.party.length > 1 ? (state.seatLast || state.lastSpeaker || null) : null;
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
  // Subtitles, not a transcript: a new reply starts a clean line, and within one
  // the words roll on — never more than this many sentences up at once, so a
  // long answer passes under the speaker instead of piling up and spilling out.
  var SUBTITLE_SENTENCES = 2;
  function rollOn(box) {
    var said = box.querySelectorAll('.said');
    for (var i = 0; i < said.length - SUBTITLE_SENTENCES; i++) said[i].remove();
    var first = box.querySelector('.said');
    if (first && /^\s+$/.test(first.textContent)) first.textContent = '';
  }

  function revealSentence(text, key, durationMs, timings, speaker) {
    if (state.rendered[key]) return;
    state.rendered[key] = true;
    delete state.pending[key];
    var opensReply = !!state.freshKeys[key];
    delete state.freshKeys[key];
    callbarSaid(speaker, text);        // and over the map, if it's open on the call

    // On a call with company, a new voice starts a new line, labelled with who
    // it is, and the instrument takes their colour.
    var group = state.party.length > 1;
    if (speaker && state.contacts[speaker]) {
      document.documentElement.style.setProperty('--contact-accent', state.contacts[speaker].accent);
    }
    if (group && speaker && speaker !== state.lastSpeaker) state.fresh = true;
    state.lastSpeaker = speaker || state.lastSpeaker;

    // With seats, the words go under the speaker's own ring.
    var seat = speaker && state.seats[speaker];
    if (seat) {
      if (opensReply || seat.fresh !== false || speaker !== state.seatLast) seat.line.textContent = '';
      seat.fresh = false;
      state.seatLast = speaker;
      state.fresh = false;
      var said = document.createElement('span');
      said.className = 'said';
      var earlier = seat.line.querySelector('.said');   // spans, not text: words still on timers
      seat.line.appendChild(said);
      if (earlier) said.textContent = ' ';
      typeWords(said, text, durationMs, timings);
      rollOn(seat.line);
      return;
    }

    // The first sentence of a reply replaces whatever was said before it.
    if (state.fresh || opensReply) {
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
    // A space before every sentence but the first. Judged by the spans, not
    // the text: when several sentences are revealed at once their words are
    // still on timers, the text is empty, and they ran together.
    var after = el.utterance.querySelector('.said');
    el.utterance.appendChild(span);
    if (after) span.textContent = ' ';
    typeWords(span, text, durationMs, timings);
    rollOn(el.utterance);
  }

  /* Words revealed in time with the voice: on the real word timings when the
     voice gave them, otherwise spread across the clip by length. */
  function typeWords(span, text, durationMs, timings) {
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
    var lull = state.party.length > 1 && (state.lulls || 0) < MAX_LULLS;
    if (lull) { window_ = GROUP_LULL; closing = false; }
    var wait = window_[0] + Math.random() * window_[1];
    var quietFor = state.quietUntil - Date.now();
    if (quietFor > 0) wait += quietFor;

    state.idleTimer = setTimeout(function () {
      // Someone being rung in: the quiet is waiting for them, not a lull to fill.
      if (!state.connectedId || ConsoleAudio.isPlaying || (state.ringingId && state.ringingId !== state.connectedId)) {
        armIdleCheck(); return;
      }
      if (closing) {
        state.closing = true;
        send({ type: 'signoff' });
        return;
      }
      if (lull) state.lulls = (state.lulls || 0) + 1;
      else state.nudges += 1;
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
    var line = state.connectedId;
    clearTimeout(state.closeTimer);
    state.closeTimer = setTimeout(function () {
      if (state.connectedId && state.connectedId === line) hangUp();
    }, 700);
  }

  function noteActivity(opts) {
    // Anything from the operator cancels a pending hang-up — he was leaving
    // because nobody was there, and now somebody is.
    state.hangUpWhenQuiet = false;
    state.closing = false;        // he's here after all: the sign-off is off
    state.nudges = 0;
    state.lulls = 0;
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
    // On a call with company, speaking belongs to one seat (see seatSpeaking);
    // the rest breathe — or all of them think, while a reply is coming.
    Object.keys(state.seats).forEach(function (id) {
      if (value !== 'speaking') state.seats[id].viz.setMode(value === 'thinking' ? 'thinking' : 'idle');
    });
  }

  /* --- seats ---------------------------------------------------------------
     A call with more than one of them gets a seat each: their own ring in
     their own colour, their name, and their words underneath. Whoever is
     speaking lights up; the rest wait. One voice at a time, always.
     ------------------------------------------------------------------------ */

  function seatIds() {
    var ids = state.party.slice();
    state.groupRinging.forEach(function (id) { if (ids.indexOf(id) === -1) ids.push(id); });
    return ids;
  }

  function renderSeats() {
    var ids = seatIds();
    var on = ids.length > 1;
    el.seats.hidden = !on;
    document.documentElement.dataset.seats = on ? ids.length : '';
    Object.keys(state.seats).forEach(function (id) {
      if (!on || ids.indexOf(id) === -1) { state.seats[id].node.remove(); delete state.seats[id]; }
    });
    if (!on) return;
    ids.forEach(function (id, i) {
      var contact = state.contacts[id];
      if (!contact) return;
      var seat = state.seats[id];
      if (!seat) {
        var node = document.createElement('div');
        node.className = 'seat';
        node.style.setProperty('--seat-accent', contact.accent);
        var ring = document.createElement('div');
        ring.className = 'seat__ring';
        var canvas = document.createElement('canvas');
        ring.appendChild(canvas);
        var face = document.createElement('span');
        face.className = 'seat__face';
        portraitStyle(face, contact, 'center 22%');
        ring.appendChild(face);
        var name = document.createElement('p');
        name.className = 'seat__name';
        name.textContent = contact.name;
        var line = document.createElement('p');
        line.className = 'seat__line';
        // Let one of them go, from their own seat: they say goodbye, the call goes on.
        var drop = document.createElement('button');
        drop.type = 'button';
        drop.className = 'seat__drop';
        drop.setAttribute('aria-label', 'Drop ' + contact.name + ' from the call');
        drop.dataset.tip = 'Drop ' + contact.name + ' from the call';
        drop.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';
        drop.addEventListener('click', (function (cid) {
          return function (e) { e.stopPropagation(); send({ type: 'drop', id: cid }); };
        })(id));
        ring.appendChild(drop);
        node.appendChild(ring);
        node.appendChild(name);
        node.appendChild(line);
        el.seats.appendChild(node);
        seat = state.seats[id] = { node: node, viz: new Visualizer(canvas, { accent: contact.accent }), line: line };
      }
      seat.node.style.order = i;
      seat.node.dataset.state = state.groupRinging.indexOf(id) !== -1 || state.ringingId === id ? 'ringing' : 'live';
    });
  }

  function seatSpeaking(speaker) {
    Object.keys(state.seats).forEach(function (id) {
      var seat = state.seats[id];
      seat.viz.setMode(id === speaker ? 'speaking' : 'idle');
      seat.node.dataset.speaking = id === speaker ? '1' : '0';
    });
  }

  /* Ring several at once — from a group chat's header. */
  function callGroup(ids) {
    ids = ids.filter(function (id) { return state.contacts[id]; }).slice(0, MAX_PARTY);
    if (!ids.length) return;
    if (ids.length === 1) return placeCall(ids[0]);
    if (state.connectedId) hangUp({ switching: true });
    state.groupRinging = ids.slice();
    state.party = [];
    state.currentId = state.connectedId = ids[0];
    state.ringingId = ids[0];
    state.lastSpeaker = null;
    el['bar-title'].textContent = ids.map(function (id) { return state.contacts[id].name; }).join(' · ');
    clearUtterance();
    showHeard('');
    state.fresh = true;
    state.nudges = 0;
    el['ringing-label'].textContent = 'Ringing ' + ids.length + '…';
    setLink('ringing');
    setState('idle');
    renderSeats();
    renderDirectory();
    send({ type: 'call_group', ids: ids });
    ConsoleTones.startRinging();
    if (state.mode === 'ptt') ConsoleMic.warm();
  }

  function setLink(value) {
    document.documentElement.dataset.link = value;
    // One button to end any call — ringing, one-to-one, or with company.
    var end = $('end-call');
    if (end) end.hidden = !(value === 'on' || value === 'ringing');
    renderDirectory();
  }

  /* --- directory ---------------------------------------------------------- */

  /* A contact's portrait, as a background stack: the supplied image if there
     is one, the silhouette if not, cropped by the profile's framing. Shared by
     the directory and the personnel file. */
  function portraitStyle(node, contact, fallbackPosition) {
    // Stamped with when the picture was saved: a new one is never the cached old one.
    var stamp = state.portraitStamp || contact.portrait_v;
    var base = '/static/portraits/' + contact.id, v = stamp ? '?v=' + stamp : '';
    node.style.backgroundImage =
      "url('" + base + ".png" + v + "'), url('" + base + ".jpg" + v + "'), url('" + base + ".webp" + v + "'), " +
      "url('/static/portraits/_silhouette.svg')";
    var frame = contact.portrait || {};
    node.style.backgroundSize = frame.size
      ? [frame.size, frame.size, frame.size, 'cover'].join(', ') : '';
    node.style.backgroundPosition = frame.position
      ? [frame.position, frame.position, frame.position, fallbackPosition].join(', ') : '';
  }

  function onCall(id) { return state.party.indexOf(id) !== -1; }

  /* What their status reads as, the way a phone would put it — with what
     they're doing, since this console knows its people. */
  function presenceLabel(contact) {
    var p = contact.presence || {};
    if (p.status === 'unknown') return p.line || 'Status unknown';
    var doing = p.doing ? ' · ' + p.doing : '';
    if (p.status === 'online') return 'Online';
    if (p.status === 'busy') return 'Busy' + doing;
    if (p.status === 'offline') return 'Offline' + doing;
    return p.last_active ? 'Last seen ' + clock(p.last_active) : 'Away';
  }

  var ICONS = {
    reply: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 14 4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 6 6v3"/></svg>',
    smile: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M8.5 14.2a4.2 4.2 0 0 0 7 0"/><path d="M9 9.6h.01M15 9.6h.01" stroke-width="2.6"/></svg>',
    plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 6v12M6 12h12"/></svg>',
    call: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z"/></svg>',
    add: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M19 8v6M22 11h-6"/></svg>',
    drop: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 11h-6"/></svg>',
    end: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="transform:rotate(135deg)"><path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z"/></svg>',
    text: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>'
  };
  var ACTION_TITLE = { call: 'Call', add: 'Add to call', drop: 'Drop from call', end: 'End call' };

  function actionButton(kind, title, onClick, disabled) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'book__act';
    b.dataset.kind = kind;
    b.dataset.tip = title;
    b.setAttribute('aria-label', title);
    b.innerHTML = ICONS[kind];
    b.disabled = !!disabled;
    b.addEventListener('click', function (e) { e.stopPropagation(); onClick(); });
    return b;
  }

  function renderDirectory() {
    hideHovercard();          // its avatar is about to be replaced, and with it the mouseleave
    el.book.innerHTML = '';
    var inCall = state.party.length > 0;
    var group = state.party.length > 1;
    var lastGroup = null;
    state.order.forEach(function (id) {
      var contact = state.contacts[id];
      // Grouped under small headings, in directory order.
      if (contact.group && contact.group !== lastGroup) {
        var heading = document.createElement('li');
        heading.className = 'book__group';
        heading.textContent = contact.group;
        el.book.appendChild(heading);
        lastGroup = contact.group;
      }
      var live = onCall(id) && state.ringingId !== id;
      var ringing = state.ringingId === id;

      var li = document.createElement('li');
      li.className = 'book__item';
      li.dataset.live = live ? '1' : '0';
      li.dataset.presence = (contact.presence || {}).status || 'idle';
      li.dataset.thread = state.messagesWith === id && !el.messages.hidden ? '1' : '0';
      if (ringing) li.dataset.state = 'ringing';
      li.style.setProperty('--contact-accent', contact.accent);

      var row = document.createElement('div');
      row.className = 'book__row';

      var avatar = document.createElement('span');
      avatar.className = 'book__avatar';
      portraitStyle(avatar, contact, 'center 22%');
      var dot = document.createElement('span');
      dot.className = 'book__dot';
      avatar.appendChild(dot);
      if (state.unread[id]) {
        var badge = document.createElement('span');
        badge.className = 'book__badge';
        badge.textContent = state.unread[id] > 9 ? '9+' : state.unread[id];
        avatar.appendChild(badge);
      }
      avatar.addEventListener('mouseenter', function () { showHovercard(id, avatar); });
      avatar.addEventListener('mouseleave', hideHovercard);
      avatar.addEventListener('click', function () {
        el.dossier.dataset.contact = id;
        openDossier(id);
      });

      var text = document.createElement('button');
      text.type = 'button';
      text.className = 'book__open';

      var name = document.createElement('span');
      name.className = 'book__name';
      name.textContent = contact.name;
      var role = document.createElement('span');
      role.className = 'book__role';
      role.textContent = ringing ? 'Connecting'
                       : live ? (group ? 'On the call' : 'Connected')
                       : (contact.available ? contact.role : 'Unavailable');
      // Their own status line in place of the role when they've set one —
      // the portrait already says who they are. Written by them, so it reads
      // like them: "patrol 🦇 hmu", "In meetings until four."
      var p = contact.presence || {};
      var shown = p.line || (p.doing ? p.doing.charAt(0).toUpperCase() + p.doing.slice(1) : '');
      if (shown && !ringing && !live) {
        role.textContent = '';
        var what = document.createElement('span');
        what.className = 'book__doing';
        what.textContent = shown;
        role.appendChild(what);
      }
      text.appendChild(name);
      text.appendChild(role);
      text.addEventListener('click', function () {
        // With the map open, a name in the directory is a person on it.
        if (window.GothamMap && GothamMap.isOpen()) return GothamMap.focus(id);
        el.dossier.dataset.contact = id;
        openDossier(id);
      });

      // Message, and one call action whose job depends on the call.
      var action;
      if (ringing) action = group ? 'drop' : 'end';
      else if (!inCall) action = 'call';
      else if (onCall(id)) action = group ? 'drop' : 'end';
      else action = 'add';
      var actions = document.createElement('div');
      actions.className = 'book__actions';
      var msg = actionButton('text', 'Message ' + contact.name, function () { openMessages(id); });
      actions.appendChild(msg);
      actions.appendChild(actionButton(action, ACTION_TITLE[action] + ' — ' + contact.name, function () {
        if (action === 'call') placeCall(id);
        else if (action === 'add') addToCall(id);
        else if (action === 'drop') send({ type: 'drop', id: id });
        else hangUp();
      }, (action === 'call' || action === 'add')
          && (!contact.available || (action === 'add' && state.party.length >= MAX_PARTY))));

      row.appendChild(avatar);
      row.appendChild(text);
      row.appendChild(actions);
      li.appendChild(row);
      el.book.appendChild(li);
    });
  }

  /* --- messages ------------------------------------------------------------
     A text thread with one contact. Written, never spoken, kept in the same
     memory as their calls, and paced like a person: delivered, read when they
     get to it, typed, answered. Scroll up for older messages.
     ------------------------------------------------------------------------ */

  function clock(at) {
    var d = new Date(at * 1000);
    return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  }

  function dayLabel(at) {
    var d = new Date(at * 1000), today = new Date();
    var yesterday = new Date(); yesterday.setDate(today.getDate() - 1);
    if (d.toDateString() === today.toDateString()) return 'Today';
    if (d.toDateString() === yesterday.toDateString()) return 'Yesterday';
    return d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' });
  }

  var CALL_LINE = {
    missed_call: 'Missed call', declined_call: 'You declined',
    refused_call: 'Call declined', unanswered_call: 'No answer', cancelled_call: 'Cancelled call'
  };

  function bubbleNode(message, contact, opts) {
    opts = opts || {};
    var li = document.createElement('li');
    if (message.kind === 'system') {
      li.className = 'messages__system';
      li.textContent = message.text;
      return li;
    }
    if (message.kind) {
      li.className = 'messages__call';
      li.innerHTML = ICONS.call;
      li.appendChild(document.createTextNode((CALL_LINE[message.kind] || 'Call') + ' · ' + clock(message.at)));
      return li;
    }
    var from = message.from === 'me' ? 'me' : 'them';
    li.className = 'bubble bubble--' + from + (message.typing ? ' bubble--typing' : '');
    if (message.id) li.dataset.id = message.id;
    if (!message.typing && from === 'them' && pingsMe(message.text, opts.group)) li.classList.add('bubble--ping');
    if (!message.typing && onlyEmoji(message.text)) li.classList.add('bubble--jumbo');
    if (from === 'them') {
      var av = document.createElement('span');
      av.className = 'bubble__avatar';
      portraitStyle(av, contact, 'center 22%');
      li.appendChild(av);
    }
    var t = document.createElement('span');
    t.className = 'bubble__text';
    if (opts.label && contact) {
      // In a group, whose message it is: their name in their colour.
      var who = document.createElement('span');
      who.className = 'bubble__who';
      who.textContent = contact.name;
      who.style.color = contact.accent;
      t.appendChild(who);
    }
    if (message.typing) {
      t.classList.add('bubble__dots');
      t.insertAdjacentHTML('beforeend', '<i></i><i></i><i></i>');
    } else {
      if (message.reply_to) t.appendChild(quoteNode(message.reply_to, opts.group, contact));
      appendMentions(t, message.text, opts.mentions, opts.group);
      if (message.at) {
        var time = document.createElement('span');
        time.className = 'bubble__time';
        time.textContent = clock(message.at);
        t.appendChild(time);
      }
    }
    var reactions = message.reactions || {};
    var who = Object.keys(reactions);
    var body = t;
    if (who.length) {
      body = document.createElement('span');
      body.className = 'bubble__body';
      body.appendChild(t);
    }
    li.appendChild(body);
    if (message.id && !message.typing) {
      // Reply to this one in particular.
      var answer = document.createElement('button');
      answer.type = 'button';
      answer.className = 'bubble__reply';
      answer.setAttribute('aria-label', 'Reply');
      answer.dataset.tip = 'Reply';
      answer.innerHTML = ICONS.reply;
      li.appendChild(answer);
    }
    if (from === 'them' && message.id && !message.typing) {
      // A way to react without knowing to double-click.
      var add = document.createElement('button');
      add.type = 'button';
      add.className = 'bubble__add';
      add.setAttribute('aria-label', 'React');
      add.dataset.tip = 'React';
      add.innerHTML = ICONS.smile;
      li.appendChild(add);
    }
    if (who.length) {
      // Tapbacks: each emoji once, with who — in the console's own tip.
      var row = document.createElement('span');
      row.className = 'bubble__reacts';
      var byEmoji = {};
      who.forEach(function (cid) { (byEmoji[reactions[cid]] = byEmoji[reactions[cid]] || []).push(cid); });
      Object.keys(byEmoji).forEach(function (emoji) {
        var chip = document.createElement('span');
        chip.className = 'bubble__react';
        if (byEmoji[emoji].indexOf('me') !== -1) chip.dataset.mine = '1';
        chip.textContent = emoji + (byEmoji[emoji].length > 1 ? ' ' + byEmoji[emoji].length : '');
        chip.dataset.tip = byEmoji[emoji].map(function (cid) {
          return cid === 'me' ? 'You' : (state.contacts[cid] || {}).name || cid;
        }).join(', ');
        row.appendChild(chip);
      });
      body.appendChild(row);
      li.dataset.reacted = '1';
    }
    return li;
  }

  /* Double-click someone's message — in a group or a DM — to tap a reaction on it. */
  var TAPBACKS = ['❤️', '👍', '👎', '😂', '‼️', '❓'];

  function wireTapbacks() {
    var bar = document.createElement('div');
    bar.className = 'tapbar';
    bar.hidden = true;
    document.body.appendChild(bar);
    var target = null;
    function react(emoji) {
      if (!target || !state.thread) return;
      var m = (state.thread.messages || []).filter(function (x) { return x.id === target; })[0];
      var mine = m && (m.reactions || {}).me;
      var emojiOut = mine === emoji ? '' : emoji;
      if (state.groupOpen) send({ type: 'group_react', id: state.groupOpen, message: target, emoji: emojiOut });
      else send({ type: 'text_react', id: state.thread.id, message: target, emoji: emojiOut });
      ConsoleTones.sent();
      rememberEmoji(emoji);
      bar.hidden = true;
    }
    TAPBACKS.forEach(function (emoji) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'tapbar__btn';
      b.textContent = emoji;
      b.addEventListener('click', function () { react(emoji); });
      bar.appendChild(b);
    });
    // Any other emoji: the same picker as the composer's.
    var more = document.createElement('button');
    more.type = 'button';
    more.className = 'tapbar__btn tapbar__more';
    more.setAttribute('aria-label', 'More reactions');
    more.innerHTML = ICONS.plus;
    more.addEventListener('click', function () {
      var r = bar.getBoundingClientRect();
      bar.hidden = true;
      openEmojiPicker({ left: r.left, top: r.top }, react);
    });
    bar.appendChild(more);
    function open(bubble) {
      if (!bubble || !state.thread) return;
      window.getSelection && window.getSelection().removeAllRanges();
      target = bubble.dataset.id;
      // What he's already put on it shows as chosen.
      var m = (state.thread.messages || []).filter(function (x) { return x.id === target; })[0];
      var mine = m && (m.reactions || {}).me;
      Array.prototype.forEach.call(bar.querySelectorAll('.tapbar__btn'), function (b) {
        b.dataset.on = b.textContent === mine ? '1' : '';
      });
      bar.hidden = false;
      var r = bubble.querySelector('.bubble__text').getBoundingClientRect();
      var w = bar.offsetWidth;
      bar.style.left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8)) + 'px';
      bar.style.top = Math.max(8, r.top - bar.offsetHeight - 6) + 'px';
    }
    el['messages-thread'].addEventListener('dblclick', function (e) {
      var bubble = e.target.closest && e.target.closest('.bubble--them[data-id]');
      if (!bubble) return;
      e.preventDefault();
      open(bubble);
    });
    el['messages-thread'].addEventListener('click', function (e) {
      var button = e.target.closest && e.target.closest('.bubble__add');
      if (button) { e.stopPropagation(); open(button.closest('.bubble')); return; }
      var reply = e.target.closest && e.target.closest('.bubble__reply');
      if (reply && state.thread) {
        var id = reply.closest('.bubble').dataset.id;
        var m = (state.thread.messages || []).filter(function (x) { return x.id === id; })[0];
        if (m) setReplyTo(m);
        return;
      }
      var quote = e.target.closest && e.target.closest('.bubble__quote');
      if (quote) {
        var original = el['messages-thread'].querySelector('.bubble[data-id="' + quote.dataset.goto + '"]');
        if (original) {
          original.scrollIntoView({ behavior: 'smooth', block: 'center' });
          original.classList.remove('is-flash');
          void original.offsetWidth;
          original.classList.add('is-flash');
        }
      }
    });
    document.addEventListener('pointerdown', function (e) {
      if (!bar.hidden && !bar.contains(e.target) && !(e.target.closest && e.target.closest('.bubble__add'))) bar.hidden = true;
    });
    el['messages-thread'].addEventListener('scroll', function () { bar.hidden = true; });
  }

  /* --- emoji ---------------------------------------------------------------
     His own, in the box or on a message: a picker in the console's furniture,
     the ones he uses most up front. */
  var EMOJI = [
    ['Recent', '🕘', []],
    ['Faces', '🙂', '😀 😂 🤣 😅 😊 🙂 😉 😍 🥲 😎 🤔 🤨 😐 😑 🙄 😏 😬 😮‍💨 😴 🤐 😶 🥱 😤 😠 😡 🤬 😳 😱 😨 😰 😢 😭 🫠 🫡 🤫 🤭 🥶 🥵 🤯 😵 🤕 🤒 💀 👻 🤡 😈'.split(' ')],
    ['Hands', '👍', '👍 👎 👌 🤌 ✌️ 🤞 🤝 🙏 👏 🙌 🫶 👊 ✊ 🤛 💪 🫡 👋 🤙 ☝️ 👆 👇 👉 👈 🖕 ✍️ 🫵'.split(' ')],
    ['Hearts', '❤️', '❤️ 🖤 🤍 💙 💜 💚 💛 🧡 💔 ❤️‍🩹 💯 ‼️ ❓ ❗ ⁉️ ✅ ❌ ⚠️ 🔥 ✨ ⭐ 💥 💢 💤 💬 👀 🎯'.split(' ')],
    ['Gotham', '🦇', '🦇 🌃 🌆 🌧️ ⛈️ 🌙 🌕 🏙️ 🗼 🕰️ 🚨 🚓 🏍️ 🚁 🔦 🕶️ 🎭 🃏 ♠️ 🐧 🌿 🧊 🐈‍⬛ 💣 🔪 🩸 🩹 💊 🔒 🗝️ 📡 💻 📱 ☕ 🍵 🍕 🍜 🥃 🍷'.split(' ')],
    ['Life', '🎮', '🎮 🎧 🎵 🎸 📚 🎬 🍿 🏀 🥊 🏋️ 🤸 🩰 🏃 🚗 ✈️ 🎂 🎉 🎁 🐶 🐱 🌹 🌞 🌊 🍔 🍩 🍪 🥞 🍳 🧃 🍺'.split(' ')]
  ];
  var emojiPick = null;

  function recentEmoji() {
    try { return JSON.parse(recall('emoji') || '[]') || []; } catch (e) { return []; }
  }

  function rememberEmoji(emoji) {
    if (!emoji) return;
    var list = recentEmoji().filter(function (e) { return e !== emoji; });
    list.unshift(emoji);
    remember('emoji', JSON.stringify(list.slice(0, 16)));
  }

  /* Emoji and nothing else, three at most: shown large, as phones do. */
  function onlyEmoji(text) {
    var t = (text || '').replace(/\s+/g, '');
    if (!t || t.length > 24) return false;
    try {
      var parts = t.match(/\p{Extended_Pictographic}(\uFE0F|\u200D\p{Extended_Pictographic}\uFE0F?|[\u{1F3FB}-\u{1F3FF}])*/gu) || [];
      return parts.length > 0 && parts.length <= 3 && parts.join('') === t;
    } catch (e) { return false; }
  }

  function openEmojiPicker(at, onPick) {
    if (!emojiPick) {
      emojiPick = document.createElement('div');
      emojiPick.className = 'emoji-pick';
      emojiPick.hidden = true;
      emojiPick.innerHTML = '<div class="emoji-pick__tabs"></div><div class="emoji-pick__grid"></div>';
      document.body.appendChild(emojiPick);
      document.addEventListener('pointerdown', function (e) {
        if (!emojiPick.hidden && !emojiPick.contains(e.target) &&
            !(e.target.closest && e.target.closest('.messages__emoji'))) emojiPick.hidden = true;
      });
      document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && !emojiPick.hidden) { emojiPick.hidden = true; e.stopPropagation(); }
      }, true);
    }
    var tabs = emojiPick.querySelector('.emoji-pick__tabs');
    var grid = emojiPick.querySelector('.emoji-pick__grid');
    var recent = recentEmoji();
    function show(index) {
      var set = index === 0 ? recent : EMOJI[index][2];
      grid.innerHTML = '';
      set.forEach(function (emoji) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'emoji-pick__btn';
        b.textContent = emoji;
        b.addEventListener('mousedown', function (e) { e.preventDefault(); });
        b.addEventListener('click', function () {
          rememberEmoji(emoji);
          if (emojiPick.dataset.keep !== '1') emojiPick.hidden = true;
          onPick(emoji);
        });
        grid.appendChild(b);
      });
      Array.prototype.forEach.call(tabs.children, function (t, i) { t.dataset.on = i === index ? '1' : ''; });
    }
    tabs.innerHTML = '';
    EMOJI.forEach(function (cat, i) {
      if (i === 0 && !recent.length) { tabs.appendChild(document.createElement('span')); return; }
      var t = document.createElement('button');
      t.type = 'button';
      t.className = 'emoji-pick__tab';
      t.textContent = cat[1];
      t.dataset.tip = cat[0];
      t.addEventListener('mousedown', function (e) { e.preventDefault(); });
      t.addEventListener('click', function () { show(i); });
      tabs.appendChild(t);
    });
    emojiPick.dataset.keep = at.keep ? '1' : '';
    emojiPick.hidden = false;
    show(recent.length ? 0 : 1);
    var w = emojiPick.offsetWidth, h = emojiPick.offsetHeight;
    var left = at.right != null ? at.right - w : at.left;
    emojiPick.style.left = Math.max(8, Math.min(left, window.innerWidth - w - 8)) + 'px';
    emojiPick.style.top = Math.max(8, (at.bottom != null ? at.bottom : at.top) - h - 6) + 'px';
  }

  function wireEmoji() {
    var input = el['messages-input'];
    var button = el['messages-emoji'];
    button.addEventListener('mousedown', function (e) { e.preventDefault(); });
    button.addEventListener('click', function () {
      if (emojiPick && !emojiPick.hidden) { emojiPick.hidden = true; return; }
      var r = el['messages-compose'].getBoundingClientRect();
      openEmojiPicker({ right: r.right - 16, bottom: r.top + 4, keep: true }, function (emoji) {
        var a = input.selectionStart == null ? input.value.length : input.selectionStart;
        var b = input.selectionEnd == null ? a : input.selectionEnd;
        input.value = input.value.slice(0, a) + emoji + input.value.slice(b);
        var pos = a + emoji.length;
        input.focus();
        input.setSelectionRange(pos, pos);
      });
    });
  }

  /* "@Tim" as a tag — only for someone who can actually be tagged here: a
     member of this group, or the person on the other end of this thread.
     Anyone else stays plain text. Hover one for their card. */
  var MENTION = /@([A-Za-z][A-Za-z'’-]*)/g;

  function mentionTarget(name, ids, group) {
    name = name.toLowerCase();
    if (name === 'bruce' || name === 'me') return 'me';
    if (group && name === 'everyone') return 'all';
    for (var i = 0; i < (ids || []).length; i++) {
      var c = state.contacts[ids[i]];
      if (c && (c.name.toLowerCase() === name || c.id === name)) return c.id;
    }
    return null;
  }

  function appendMentions(node, text, ids, group) {
    var last = 0, m;
    MENTION.lastIndex = 0;
    while ((m = MENTION.exec(text || '')) !== null) {
      var target = mentionTarget(m[1], ids, group);
      if (!target) continue;
      if (m.index > last) node.appendChild(document.createTextNode(text.slice(last, m.index)));
      var tag = document.createElement('span');
      tag.className = 'mention' + (target === 'me' ? ' mention--me' : target === 'all' ? ' mention--all' : '');
      tag.textContent = '@' + m[1];
      if (target !== 'me' && target !== 'all') {
        tag.dataset.mention = target;
        tag.style.setProperty('--contact-accent', state.contacts[target].accent);
      }
      node.appendChild(tag);
      last = m.index + m[0].length;
    }
    if (last < (text || '').length) node.appendChild(document.createTextNode(text.slice(last)));
  }

  /* A tag on him — "@Bruce", or "@everyone" in a group: a ping, not a mention in passing. */
  function pingsMe(text, group) {
    return new RegExp('(^|[^\\w@])@(bruce' + (group ? '|everyone' : '') + ')\\b', 'i').test(text || '');
  }

  /* Typing "@" in the box: who can be tagged here, to pick from. */
  function mentionables() {
    if (state.groupOpen) return ((state.groups[state.groupOpen] || {}).members || []).concat(['everyone']);
    return state.messagesWith ? [state.messagesWith] : [];
  }

  function wireMentions() {
    var input = el['messages-input'];
    var pick = document.createElement('ul');
    pick.className = 'mention-pick';
    pick.hidden = true;
    el['messages-compose'].appendChild(pick);
    var options = [], at = -1, chosen = 0;

    function close() { pick.hidden = true; options = []; at = -1; }
    function draw() {
      pick.innerHTML = '';
      options.forEach(function (cid, i) {
        var c = state.contacts[cid] || { name: 'everyone', accent: 'var(--primary)' };
        var li = document.createElement('li');
        li.className = 'mention-pick__item';
        if (i === chosen) li.dataset.on = '1';
        li.style.setProperty('--contact-accent', c.accent);
        var face = document.createElement('span');
        face.className = 'bubble__avatar';
        if (cid === 'everyone') {
          face.classList.add('mention-pick__all');
          face.textContent = '@';
        } else {
          portraitStyle(face, c, 'center 22%');
        }
        li.appendChild(face);
        li.appendChild(document.createTextNode(c.name));
        if (cid === 'everyone') {
          var hint = document.createElement('small');
          hint.textContent = 'pings the whole chat';
          li.appendChild(hint);
        }
        li.addEventListener('mousedown', function (e) { e.preventDefault(); choose(i); });
        pick.appendChild(li);
      });
      pick.hidden = !options.length;
    }
    function choose(i) {
      var c = state.contacts[options[i]] || (options[i] === 'everyone' ? { name: 'everyone' } : null);
      if (!c || at < 0) return close();
      var caret = input.selectionStart;
      var before = input.value.slice(0, at), after = input.value.slice(caret);
      input.value = before + '@' + c.name + ' ' + after.replace(/^\s+/, '');
      var pos = before.length + c.name.length + 2;
      input.setSelectionRange(pos, pos);
      close();
    }
    input.addEventListener('input', function () {
      var upto = input.value.slice(0, input.selectionStart);
      var m = /(^|\s)@([A-Za-z'’-]*)$/.exec(upto);
      if (!m) return close();
      at = upto.length - m[2].length - 1;
      var q = m[2].toLowerCase();
      options = mentionables().filter(function (cid) {
        if (cid === 'everyone') return 'everyone'.indexOf(q) === 0;
        var c = state.contacts[cid];
        return c && (c.name.toLowerCase().indexOf(q) === 0 || c.full_name.toLowerCase().indexOf(q) === 0);
      });
      chosen = 0;
      draw();
    });
    input.addEventListener('keydown', function (e) {
      if (pick.hidden) return;
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        chosen = (chosen + (e.key === 'ArrowDown' ? 1 : options.length - 1)) % options.length;
        draw();
      } else if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        choose(chosen);
      } else if (e.key === 'Escape') {
        e.preventDefault();
        close();
      }
    });
    input.addEventListener('blur', close);
    // A tag in a message: their card, as on their portrait.
    el['messages-thread'].addEventListener('pointerover', function (e) {
      var tag = e.target.closest && e.target.closest('[data-mention]');
      if (tag) showHovercard(tag.dataset.mention, tag);
    });
    el['messages-thread'].addEventListener('pointerout', function (e) {
      if (e.target.closest && e.target.closest('[data-mention]')) hideHovercard();
    });
  }

  /* In a group: one typing row however many are at it — their faces, who, the dots. */
  function typingRow(typers) {
    var names = typers.map(function (cid) { return state.contacts[cid].name; });
    var li = document.createElement('li');
    li.className = 'bubble bubble--them bubble--typing';
    var faces = document.createElement('span');
    faces.className = 'bubble__avatar';
    if (typers.length === 1) portraitStyle(faces, state.contacts[typers[0]], 'center 22%');
    else stackPortraits(faces, typers);
    var t = document.createElement('span');
    t.className = 'bubble__text bubble__dots';
    var who = document.createElement('span');
    who.className = 'bubble__who';
    who.textContent = names.length === 1 ? names[0] + ' is typing'
      : names.length === 2 ? names[0] + ' and ' + names[1] + ' are typing'
      : names.length + ' people are typing';
    who.style.color = names.length === 1 ? state.contacts[typers[0]].accent : 'var(--text-dim)';
    t.appendChild(who);
    t.insertAdjacentHTML('beforeend', '<i></i><i></i><i></i>');
    li.appendChild(faces);
    li.appendChild(t);
    return li;
  }

  /* Two messages from the same person, close together, read as one run. */
  function sameRun(a, b) {
    return a && b && !a.kind && !b.kind && a.from === b.from && Math.abs(b.at - a.at) < 180
      && dayLabel(a.at) === dayLabel(b.at);
  }

  /* Draw the whole thread: day separators, bubbles, and — on your last
     message — whether it's been delivered or read. */
  function renderThread() {
    var th = state.thread;
    if (!th) return;
    var group = th.kind === 'group';
    var contact = group ? null : state.contacts[th.id];
    var box = el['messages-thread'];
    // Reading back through the thread, it stays where he is; at the bottom,
    // it follows what comes in (see scrollThreadToEnd).
    var kept = box.scrollTop;
    state.threadAtEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    box.innerHTML = '';
    if (th.more) {
      var more = document.createElement('li');
      more.className = 'messages__more';
      more.textContent = 'Scroll for earlier';
      box.appendChild(more);
    }
    var lastDay = null, lastMine = null;
    th.messages.forEach(function (m, i) {
      var day = dayLabel(m.at);
      if (day !== lastDay) {
        var sep = document.createElement('li');
        sep.className = 'messages__day';
        sep.textContent = day;
        box.appendChild(sep);
        lastDay = day;
      }
      var sender = group ? state.contacts[m.from] : contact;
      if (m.kind === 'system') { box.appendChild(bubbleNode(m)); return; }
      var prev = th.messages[i - 1];
      var node = bubbleNode(m, sender, { label: group && m.from !== 'me' && !sameRun(prev, m),
                                         mentions: group ? th.members : [th.id], group: !!group });
      if (m.id && th.seen && !th.seen[m.id]) { node.dataset.new = '1'; th.seen[m.id] = true; }
      var next = th.messages[i + 1];
      if (sameRun(m, next) || (!next && m.from === 'them' && state.typing[th.id])) node.dataset.run = '1';
      box.appendChild(node);
      if (m.from === 'me' && !m.kind) lastMine = m;
    });
    // Delivered or Read, under your message — while it's the latest thing said.
    if (lastMine && lastMine === th.messages[th.messages.length - 1]) {
      var receipt = document.createElement('li');
      receipt.className = 'messages__receipt';
      if (group) {
        // Who in the group has read it, by name.
        var readers = (th.members || []).filter(function (cid) {
          return (th.reads || {})[cid] >= lastMine.at;
        }).map(function (cid) { return (state.contacts[cid] || {}).name || cid; });
        receipt.textContent = readers.length ? 'Read by ' + readers.join(', ') : 'Delivered';
        if (readers.length) receipt.dataset.read = '1';
      } else {
        receipt.textContent = lastMine.read_at ? 'Read ' + clock(lastMine.read_at) : 'Delivered';
        if (lastMine.read_at) receipt.dataset.read = '1';
      }
      box.appendChild(receipt);
    }
    if (group) {
      var typers = Object.keys(state.groupTyping[th.id] || {}).filter(function (cid) { return state.contacts[cid]; });
      if (typers.length) box.appendChild(typingRow(typers));
    } else if (state.typing[th.id]) {
      box.appendChild(bubbleNode({ from: 'them', text: '', typing: true }, contact));
    }
    if (state.threadAtEnd === false) box.scrollTop = kept;
  }

  /* What the server sent for a thread, plus anything that arrived as an event
     while it was being fetched — by id, in order. Replacing the list dropped
     a message that landed in between. */
  function mergeMessages(fetched, live) {
    var seen = {};
    fetched.forEach(function (m) { if (m.id) seen[m.id] = true; });
    var extra = (live || []).filter(function (m) { return m.id && !seen[m.id]; });
    return fetched.concat(extra).sort(function (a, b) { return (a.at || 0) - (b.at || 0); });
  }

  /* To the latest message — if he was already there, or `force` (opening a
     thread, sending one himself). Someone typing or reading no longer drags
     him down from whatever he'd scrolled back to. */
  function scrollThreadToEnd(force) {
    var box = el['messages-thread'];
    if (force || state.threadAtEnd !== false) box.scrollTop = box.scrollHeight;
  }

  /* --- tabs: the conversations he has open ----------------------------------
     Each DM or group he opens gets a tab along the top of the panel — most
     recent last, a handful at most — with an unread dot, closable. */
  var MAX_TABS = 6;
  try { state.tabs = JSON.parse(recall('tabs') || '[]') || []; } catch (e) { state.tabs = []; }

  function openTab(kind, id) {
    var at = state.tabs.findIndex(function (tb) { return tb.kind === kind && tb.id === id; });
    if (at === -1) {
      state.tabs.push({ kind: kind, id: id });
      if (state.tabs.length > MAX_TABS) state.tabs.shift();
    }
    remember('tabs', JSON.stringify(state.tabs));
    renderTabs();
  }

  function closeTab(kind, id) {
    var at = state.tabs.findIndex(function (tb) { return tb.kind === kind && tb.id === id; });
    if (at === -1) return;
    state.tabs.splice(at, 1);
    remember('tabs', JSON.stringify(state.tabs));
    var open = (kind === 'dm' && state.messagesWith === id) || (kind === 'group' && state.groupOpen === id);
    if (open) {
      var next = state.tabs[Math.min(at, state.tabs.length - 1)];
      if (!next) closeMessages();
      else if (next.kind === 'dm') openMessages(next.id);
      else openGroup(next.id);
    }
    renderTabs();
  }

  function renderTabs() {
    var nav = el['messages-tabs'];
    if (!nav) return;
    state.tabs = state.tabs.filter(function (tb) {
      return tb.kind === 'dm' ? !!state.contacts[tb.id] : !!state.groups[tb.id];
    });
    nav.innerHTML = '';
    nav.hidden = state.tabs.length < 2;
    state.tabs.forEach(function (tb) {
      var isDm = tb.kind === 'dm', contact = isDm && state.contacts[tb.id], group = !isDm && state.groups[tb.id];
      var tab = document.createElement('div');
      tab.className = 'messages__tab';
      var on = isDm ? state.messagesWith === tb.id : state.groupOpen === tb.id;
      tab.classList.toggle('is-on', !!on);
      tab.setAttribute('role', 'tab');
      tab.tabIndex = 0;
      var face = document.createElement('span');
      face.className = 'messages__tab-face';
      if (contact) { portraitStyle(face, contact, 'center 22%'); face.style.setProperty('--accent', contact.accent); }
      else face.textContent = (group.name || '#')[0].toUpperCase();
      var name = document.createElement('span');
      name.className = 'messages__tab-name';
      name.textContent = contact ? contact.name : group.name;
      var unread = isDm ? state.unread[tb.id] : (state.unread[groupKey(tb.id)] || state.pinged[groupKey(tb.id)]);
      if (unread && !on) tab.classList.add('is-unread');
      var x = document.createElement('button');
      x.type = 'button';
      x.className = 'messages__tab-x';
      x.setAttribute('aria-label', 'Close ' + name.textContent);
      x.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';
      x.addEventListener('click', function (e) { e.stopPropagation(); closeTab(tb.kind, tb.id); });
      tab.appendChild(face);
      tab.appendChild(name);
      tab.appendChild(x);
      tab.addEventListener('click', function () { if (isDm) openMessages(tb.id); else openGroup(tb.id); });
      tab.addEventListener('keydown', function (e) { if (e.key === 'Enter') tab.click(); });
      nav.appendChild(tab);
    });
  }

  function openMessages(id) {
    var contact = state.contacts[id];
    if (!contact) return;
    openTab('dm', id);
    clearToasts('t:' + id);
    setReplyTo(null);
    state.messagesWith = id;
    state.groupOpen = null;
    state.lastThread = id;
    delete state.unread[id];
    updateInbox();
    el['messages-delete'].hidden = true;
    el['messages-call'].hidden = true;
    el['messages-avatar'].innerHTML = '';
    el['messages-avatar'].classList.remove('stack');
    el.messages.style.setProperty('--contact-accent', contact.accent);
    portraitStyle(el['messages-avatar'], contact, 'center 22%');
    el['messages-name'].textContent = contact.full_name;
    el['messages-role'].textContent = presenceLabel(contact);
    el['messages-role'].dataset.presence = (contact.presence || {}).status || '';
    state.thread = { id: id, messages: [], more: false, loading: true, seen: {} };
    renderThread();
    el.messages.hidden = false;
    renderDirectory();
    fetch('/api/contacts/' + id + '/messages?limit=40')
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!state.thread || state.thread.id !== id) return;
        state.thread.messages = mergeMessages(d.messages || [], state.thread.messages);
        state.thread.messages.forEach(function (m) { state.thread.seen[m.id] = true; });
        state.thread.more = (d.messages || []).length >= 40;
        state.thread.loading = false;
        if (d.typing) {
          state.typing[id] = true;
          expireTyping('t:' + id, function () { delete state.typing[id]; if (threadOpenFor(id)) renderThread(); });
        } else {
          delete state.typing[id];
        }
        renderThread();
        scrollThreadToEnd(true);
        markSeen();
      })
      .catch(function () { if (state.thread && state.thread.id === id) state.thread.loading = false; });
    el['messages-input'].focus();
  }

  /* Scrolled to the top: fetch the page before the oldest message shown, and
     keep the view where it was. */
  function loadEarlier() {
    var th = state.thread;
    if (!th || th.loading || !th.more || !th.messages.length) return;
    th.loading = true;
    var box = el['messages-thread'];
    var fromBottom = box.scrollHeight - box.scrollTop;
    var base = th.kind === 'group' ? '/api/groups/' : '/api/contacts/';
    fetch(base + th.id + '/messages?limit=40&before=' + th.messages[0].at)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (state.thread !== th) return;
        (d.messages || []).forEach(function (m) { th.seen[m.id] = true; });
        th.messages = (d.messages || []).concat(th.messages);
        th.more = (d.messages || []).length >= 40;
        th.loading = false;
        renderThread();
        box.scrollTop = box.scrollHeight - fromBottom;
      })
      .catch(function () { th.loading = false; });
  }

  function closeMessages() {
    state.messagesWith = null;
    state.groupOpen = null;
    state.thread = null;
    el.messages.hidden = true;
    renderDirectory();
  }

  var STATUS_COLOUR = { online: 'var(--good)', idle: '#c9a23a', busy: 'var(--alert)', offline: 'var(--text-faint)', unknown: 'transparent' };

  /* Hover a portrait: who, and what they're doing — beside it, in the rail or out. */
  function showHovercard(id, anchor) {
    var contact = state.contacts[id];
    if (!contact) return;
    var card = $('hovercard');
    card.style.setProperty('--contact-accent', contact.accent);
    card.style.setProperty('--status', STATUS_COLOUR[(contact.presence || {}).status] || '');
    $('hovercard-name').textContent = contact.full_name;
    var p = contact.presence || {};
    var line = onCall(id) ? 'On the call' : (p.status === 'unknown' ? 'Status unknown' : presenceLabel(contact));
    if (state.unread[id]) line += ' · ' + state.unread[id] + ' unread';
    $('hovercard-status').textContent = line;
    $('hovercard-line').textContent = p.line ? '“' + p.line + '”' : '';
    $('hovercard-line').hidden = !p.line;
    // Where they are, for those who share it — and who's there with them.
    var where = $('hovercard-where');
    var company = (p['with'] || []).map(function (cid) { return (state.contacts[cid] || {}).name; })
      .filter(Boolean);
    // On the way somewhere is a state of its own: where to, how, how long — and with nobody yet.
    var route = p.route && p.route.end * 1000 > Date.now() ? p.route : null;
    where.textContent = !p.where ? '' : route
      ? '→ ' + p.where + (route.by ? ' · ' + route.by : '') + ' · ' + Math.max(1, Math.round((route.end * 1000 - Date.now()) / 60000)) + ' min'
      : p.where + (company.length ? ' · with ' + company.join(', ') : '');
    where.hidden = !p.where;
    var r = anchor.getBoundingClientRect();
    card.hidden = false;
    var w = card.offsetWidth;
    if (r.right + 12 + w > window.innerWidth - 8) {
      // No room beside it (a tag in the messages panel): just under it instead.
      card.style.left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8)) + 'px';
      card.style.top = (r.bottom + 6) + 'px';
    } else {
      card.style.left = (r.right + 12) + 'px';
      card.style.top = (r.top + r.height / 2 - 22) + 'px';
    }
    // Taller with a map: kept on screen near the bottom of the list.
    var over = card.getBoundingClientRect().bottom - (window.innerHeight - 8);
    if (over > 0) card.style.top = (parseFloat(card.style.top) - over) + 'px';
  }

  function hideHovercard() { $('hovercard').hidden = true; }



  function sendMessage(e) {
    e.preventDefault();
    var body = el['messages-input'].value.trim();
    var group = state.groupOpen;
    var id = group || state.messagesWith;
    if (!body || !id) return;
    // Kept in the box if the link is down: a text that silently vanished was
    // worse than one that visibly didn't go.
    var reply = state.replyTo ? state.replyTo.id : null;
    if (!send(group ? { type: 'group_text', id: group, text: body, reply_to: reply }
                    : { type: 'text', id: id, text: body, reply_to: reply })) return;
    el['messages-input'].value = '';
    setReplyTo(null);
    ConsoleTones.sent();
  }

  /* Replying to one message in particular: a bar over the box says which, and
     the message sent carries it — shown in the bubble, and told to them. */
  function setReplyTo(message) {
    state.replyTo = message ? { id: message.id, from: message.from, text: message.text } : null;
    var bar = el['messages-replying'];
    bar.hidden = !message;
    if (!message) return;
    var who = message.from === 'me' ? 'yourself' : ((state.contacts[message.from] || {}).name ||
              ((state.contacts[state.thread && state.thread.id] || {}).name) || 'them');
    bar.querySelector('.messages__replying-who').textContent = 'Replying to ' + who;
    bar.querySelector('.messages__replying-text').textContent = message.text;
    el['messages-input'].focus();
  }

  function quoteNode(quoted, group, contact) {
    var q = document.createElement('span');
    q.className = 'bubble__quote';
    var who = quoted.from === 'me' ? 'You' : group ? ((state.contacts[quoted.from] || {}).name || '') : (contact || {}).name || '';
    var name = document.createElement('b');
    name.textContent = who;
    var text = document.createElement('span');
    text.textContent = quoted.text;
    q.appendChild(name);
    q.appendChild(text);
    q.dataset.goto = quoted.id;
    return q;
  }

  /* He's seen their messages — the thread is open and the window in front.
     Their phone shows it, as anyone's does. */
  function markSeen() {
    if (!state.thread || el.messages.hidden || document.visibilityState !== 'visible') return;
    if (state.thread.kind === 'group') send({ type: 'group_seen', id: state.thread.id });
    else send({ type: 'seen', id: state.thread.id });
  }

  function threadOpenFor(id) {
    return state.thread && state.thread.id === id && !el.messages.hidden;
  }

  function onTextSent(event) {
    if (!threadOpenFor(event.speaker)) return;
    state.thread.messages.push(event.message);
    renderThread();
    scrollThreadToEnd(true);
  }

  function onTextRead(event) {
    if (!threadOpenFor(event.speaker)) return;
    state.thread.messages.forEach(function (m) {
      if (event.ids.indexOf(m.id) !== -1) m.read_at = event.at;
    });
    renderThread();
  }

  /* Dots that outstay any real typing — a restart mid-reply, a lost event —
     go away on their own after a minute without a message. */
  var typingTimers = {};
  function expireTyping(key, clear) {
    clearTimeout(typingTimers[key]);
    typingTimers[key] = setTimeout(clear, 60000);
  }

  function onTextTyping(event) {
    state.typing[event.speaker] = true;
    expireTyping('t:' + event.speaker, function () {
      delete state.typing[event.speaker];
      if (threadOpenFor(event.speaker)) renderThread();
    });
    if (threadOpenFor(event.speaker)) { renderThread(); scrollThreadToEnd(); }
  }

  function onTextIdle(event) {
    delete state.typing[event.speaker];
    if (threadOpenFor(event.speaker)) renderThread();
  }

  function onTextReply(event) {
    delete state.typing[event.speaker];
    if (threadOpenFor(event.speaker)) {
      state.thread.messages.push(event.message);
      renderThread();
      scrollThreadToEnd();
      markSeen();
    } else {
      state.unread[event.speaker] = (state.unread[event.speaker] || 0) + 1;
      updateInbox();
      renderDirectory();
      notify(event.speaker, event.message.text);
    }
  }

  /* Presence changed: the dot, the line under their name, the thread header. */
  function onPresence(event) {
    var contact = state.contacts[event.speaker];
    if (!contact) return;
    contact.presence = event.presence;
    renderDirectory();
    if (window.GothamMap) GothamMap.update();
    if (state.messagesWith === event.speaker) {
      el['messages-role'].textContent = presenceLabel(contact);
      el['messages-role'].dataset.presence = event.presence.status;
    }
  }

  /* --- calls to him ----------------------------------------------------------
     A contact rings: the console announces it, an inbound ring plays, and he
     answers or declines. Unanswered, it's a missed call in their thread — and
     they may well text instead.
     ------------------------------------------------------------------------ */

  function onIncoming(event) {
    var contact = state.contacts[event.speaker];
    if (!contact) return;
    // Locked, or already on a line: it rings out unanswered, as a phone would
    // on a call — not over the lock screen, where answering skipped the passcode.
    if (document.documentElement.dataset.phase !== 'live' || state.connectedId) return;
    state.incomingId = event.speaker;
    el.incoming.style.setProperty('--contact-accent', contact.accent);
    portraitStyle(el['incoming-avatar'], contact, 'center 22%');
    el['incoming-name'].textContent = contact.full_name;
    el.incoming.hidden = false;
    ConsoleSystem.say('incoming', event.speaker).then(function () {
      if (state.incomingId === event.speaker) ConsoleTones.startIncoming();
    });
  }

  function closeIncoming() {
    state.incomingId = null;
    el.incoming.hidden = true;
    ConsoleTones.stopRinging();
    ConsoleSystem.stop();
  }

  function acceptIncoming() {
    var id = state.incomingId;
    var contact = state.contacts[id];
    closeIncoming();
    if (!contact) return;
    state.currentId = id;
    state.ringingId = id;      // until the first word, as with a call he places
    state.connectedId = id;
    state.party = [id];
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
    el['ringing-label'].textContent = contact.name + ' on the line…';
    if (state.mode === 'ptt') ConsoleMic.warm();
    setLink('ringing');
    setState('idle');
    send({ type: 'answer', id: id });
    renderDirectory();
    el.input.focus();
  }

  function declineIncoming() {
    var id = state.incomingId;
    closeIncoming();
    if (id) send({ type: 'decline', id: id });
  }

  function onUnanswered(event) {
    if (state.incomingId === event.speaker) closeIncoming();
    // Answered just as it rang out: the line never opened — don't sit on it.
    if (state.connectedId === event.speaker && document.documentElement.dataset.link !== 'on') {
      hangUp({ refused: 'unavailable', refusedId: event.speaker });
    }
    if (!event.message) return;
    if (threadOpenFor(event.speaker)) {
      state.thread.messages.push(event.message);
      renderThread();
      scrollThreadToEnd();
    }
    if (event.how === 'missed') {
      ConsoleTones.missed();
      notify(event.speaker, 'Missed call');
    }
  }

  /* They didn't take his call: declined after a ring or two, or no answer.
     The Batcomputer says which, and the line closes — or, if they were being
     added to a call in progress, the call simply carries on without them. */
  function onRefused(event) {
    var id = event.speaker;
    var line = event.how === 'declined' ? 'declined' : 'unavailable';
    if (event.group || state.groupRinging.indexOf(id) !== -1) {
      state.groupRinging = state.groupRinging.filter(function (x) { return x !== id; });
      ConsoleSystem.say(line, id);
      if (!state.groupRinging.length && !state.party.length) {
        hangUp({ refused: line, refusedId: id });
        return;
      }
      renderSeats();
      renderDirectory();
      return;
    }
    if (state.connectedId === id && state.party.length <= 1) {
      hangUp({ refused: line, refusedId: id });
      return;
    }
    if (state.ringingId === id) state.ringingId = null;
    ConsoleTones.stopRinging();
    ConsoleSystem.say(line, id);
    renderDirectory();
  }

  function updateInbox() {
    renderTabs();
    var n = Object.keys(state.unread).reduce(function (sum, id) { return sum + (state.unread[id] || 0); }, 0);
    remember('unread', JSON.stringify(state.unread));
    el['inbox-count'].hidden = !n;
    el['inbox-count'].textContent = n;
    el.inbox.setAttribute('aria-label', n ? 'Messages, ' + n + ' unread' : 'Messages');
  }


  /* --- group chats -----------------------------------------------------------
     A chat with several of them at once. Each reads in their own time, decides
     whether to say anything, and may answer each other rather than him; the
     server runs all of that. Here: the list in the directory, the thread in the
     message panel, who's read what, who's typing.
     ------------------------------------------------------------------------ */

  function groupKey(id) { return 'g:' + id; }

  /* Up to four faces in one circle, laid out for how many there are — a pair
     side by side, three in a triangle, four in a square — and "+N" past that. */
  function stackPortraits(node, members) {
    node.innerHTML = '';
    node.classList.add('stack');
    node.style.backgroundImage = 'none';
    var shown = members.filter(function (cid) { return state.contacts[cid]; });
    var faces = shown.length > 4 ? shown.slice(0, 3) : shown.slice(0, 4);
    node.dataset.count = shown.length > 4 ? 4 : faces.length;
    faces.forEach(function (cid) {
      var face = document.createElement('span');
      face.className = 'stack__face';
      portraitStyle(face, state.contacts[cid], 'center 22%');
      node.appendChild(face);
    });
    if (shown.length > 4) {
      var more = document.createElement('span');
      more.className = 'stack__face stack__more';
      more.textContent = '+' + (shown.length - 3);
      node.appendChild(more);
    }
  }

  function groupMembersLine(group) {
    return group.members.map(function (cid) { return (state.contacts[cid] || {}).name || cid; }).join(', ');
  }

  function loadGroups() {
    return fetch('/api/groups').then(function (r) { return r.json(); }).then(function (d) {
      state.groups = {};
      (d.groups || []).forEach(function (g) { state.groups[g.id] = g; });
      Object.keys(state.unread).forEach(function (key) {
        if (key.indexOf('g:') === 0 && !state.groups[key.slice(2)]) delete state.unread[key];
      });
      updateInbox();
      renderGroups();
    }).catch(function () {});
  }

  function renderGroups() {
    var list = el.groups;
    if (!list) return;
    list.innerHTML = '';
    el['groups-wrap'].hidden = !Object.keys(state.groups).length;
    var ids = Object.keys(state.groups).sort(function (a, b) {
      var la = (state.groups[a].last || {}).at || state.groups[a].created_at || 0;
      var lb = (state.groups[b].last || {}).at || state.groups[b].created_at || 0;
      return lb - la;
    });
    ids.forEach(function (id) {
      var g = state.groups[id];
      var li = document.createElement('li');
      li.className = 'book__item groups__item';
      li.dataset.thread = state.groupOpen === id && !el.messages.hidden ? '1' : '0';
      var row = document.createElement('button');
      row.type = 'button';
      row.className = 'book__row groups__row';
      var faces = document.createElement('span');
      faces.className = 'book__avatar';
      stackPortraits(faces, g.members);
      var unread = state.unread[groupKey(id)];
      if (unread) {
        var badge = document.createElement('span');
        badge.className = 'book__badge';
        badge.textContent = state.pinged[groupKey(id)] ? '@' : (unread > 9 ? '9+' : unread);
        if (state.pinged[groupKey(id)]) badge.dataset.ping = '1';
        faces.appendChild(badge);
      }
      var text = document.createElement('span');
      text.className = 'book__open';
      var name = document.createElement('span');
      name.className = 'book__name';
      name.textContent = g.name;
      var line = document.createElement('span');
      line.className = 'book__role groups__last';
      var last = g.last;
      line.textContent = last ? ((last.from === 'me' ? 'You' : (state.contacts[last.from] || {}).name || '')
                                 + ': ' + last.text) : groupMembersLine(g);
      text.appendChild(name);
      text.appendChild(line);
      row.appendChild(faces);
      row.appendChild(text);

      row.addEventListener('click', function () { openGroup(id); });
      li.appendChild(row);
      list.appendChild(li);
    });
  }

  function openGroup(id) {
    var g = state.groups[id];
    if (!g) return;
    openTab('group', id);
    setReplyTo(null);
    state.groupOpen = id;
    state.messagesWith = null;
    delete state.unread[groupKey(id)];
    delete state.pinged[groupKey(id)];
    clearToasts('g:' + id);       // its notifications have done their job
    updateInbox();
    el.messages.style.removeProperty('--contact-accent');
    stackPortraits(el['messages-avatar'], g.members);
    el['messages-name'].textContent = g.name;
    el['messages-role'].textContent = groupMembersLine(g);
    el['messages-role'].dataset.presence = '';
    el['messages-delete'].hidden = false;
    el['messages-call'].hidden = false;
    state.thread = { kind: 'group', id: id, messages: [], more: false, loading: true, seen: {},
                     members: g.members, reads: g.reads || {} };
    renderThread();
    el.messages.hidden = false;
    renderDirectory();
    renderGroups();
    fetch('/api/groups/' + id + '/messages?limit=40')
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!state.thread || state.thread.id !== id) return;
        state.thread.messages = mergeMessages(d.messages || [], state.thread.messages);
        state.thread.messages.forEach(function (m) { state.thread.seen[m.id] = true; });
        state.thread.more = (d.messages || []).length >= 40;
        state.thread.reads = d.reads || {};
        state.thread.loading = false;
        state.groupTyping[id] = {};
        (d.typing || []).forEach(function (cid) {
          state.groupTyping[id][cid] = true;
          expireTyping('g:' + id + ':' + cid, function () {
            if (state.groupTyping[id]) delete state.groupTyping[id][cid];
            if (groupOpenFor(id)) renderThread();
          });
        });
        renderThread();
        scrollThreadToEnd(true);
        markSeen();
      })
      .catch(function () { if (state.thread && state.thread.id === id) state.thread.loading = false; });
    el['messages-input'].focus();
  }

  function groupOpenFor(id) {
    return state.groupOpen === id && state.thread && state.thread.id === id && !el.messages.hidden;
  }

  function onGroupEvent(event) {
    var id = event.group || event.id || (event.group_info || {}).id;
    switch (event.type) {
      case 'group_created':
        state.groups[event.group.id] = event.group;
        renderGroups();
        return;
      case 'group_deleted':
        delete state.groups[event.id];
        delete state.unread[groupKey(event.id)];
        updateInbox();
        if (state.groupOpen === event.id) closeMessages();
        renderGroups();
        return;
      case 'group_updated':
        state.groups[event.group.id] = Object.assign(state.groups[event.group.id] || {}, event.group);
        if (groupOpenFor(event.group.id)) {
          state.thread.members = event.group.members;
          el['messages-name'].textContent = event.group.name;
          el['messages-role'].textContent = groupMembersLine(event.group);
          stackPortraits(el['messages-avatar'], event.group.members);
          renderThread();
        }
        if (!el['group-info'].hidden && state.groupOpen === event.group.id) openGroupInfo();
        renderGroups();
        return;
      case 'group_typing':
        (state.groupTyping[id] = state.groupTyping[id] || {})[event.speaker] = true;
        expireTyping('g:' + id + ':' + event.speaker, function () {
          if (state.groupTyping[id]) delete state.groupTyping[id][event.speaker];
          if (groupOpenFor(id)) renderThread();
        });
        break;
      case 'group_idle':
        if (state.groupTyping[id]) delete state.groupTyping[id][event.speaker];
        break;
      case 'group_read':
        if (state.groups[id]) (state.groups[id].reads = state.groups[id].reads || {})[event.member] = event.at;
        if (groupOpenFor(id)) state.thread.reads[event.member] = event.at;
        break;
      case 'group_reaction':
        if (state.groups[id] && state.groups[id].last && state.groups[id].last.id === event.message.id) {
          state.groups[id].last = event.message;
        }
        if (groupOpenFor(id)) {
          state.thread.messages = state.thread.messages.map(function (x) {
            return x.id === event.message.id ? event.message : x;
          });
          renderThread();
        }
        return;
      case 'group_sent':
      case 'group_message':
        var m = event.message;
        if (!state.groups[id]) return;      // a group this page doesn't know (deleted mid-message)
        if (state.groupTyping[id]) delete state.groupTyping[id][m.from];
        if (state.groups[id]) state.groups[id].last = m;
        var ping = m.from !== 'me' && pingsMe(m.text, true);
        if (groupOpenFor(id)) {
          state.thread.messages.push(m);
          if (m.from === 'me') state.threadAtEnd = true;
          else markSeen();
          if (ping && ConsoleTones.ping) ConsoleTones.ping();
        } else if (m.from !== 'me') {
          state.unread[groupKey(id)] = (state.unread[groupKey(id)] || 0) + 1;
          if (ping) state.pinged[groupKey(id)] = true;
          updateInbox();
          notifyGroup(id, m, ping);
        }
        renderGroups();
        break;
    }
    if (groupOpenFor(id)) { renderThread(); scrollThreadToEnd(); }
  }

  /* A group message from someone, while its thread is closed. */
  function notifyGroup(id, message, ping) {
    var g = state.groups[id];
    var who = state.contacts[message.from];
    if (!g || !who) return;
    var card = document.createElement('div');
    card.className = 'toast';
    card.style.setProperty('--contact-accent', who.accent);
    var av = document.createElement('span');
    av.className = 'bubble__avatar';
    portraitStyle(av, who, 'center 22%');
    var words = document.createElement('div');
    var name = document.createElement('span');
    name.className = 'toast__name';
    name.textContent = who.name + (ping ? (/@everyone\b/i.test(message.text) ? ' pinged everyone' : ' pinged you')
                                        : '') + ' · ' + g.name;
    var line = document.createElement('span');
    line.className = 'toast__text';
    appendMentions(line, message.text, g.members, true);
    if (ping) card.dataset.ping = '1';
    words.appendChild(name);
    words.appendChild(line);
    card.appendChild(av);
    card.appendChild(words);
    card.addEventListener('click', function () { dismissToast(card); openGroup(id); });
    showToast(card, 'g:' + id, ping ? 12000 : 6500);
    if (ping && ConsoleTones.ping) ConsoleTones.ping();
    else if (ConsoleTones.message) ConsoleTones.message();
  }

  /* New group: a name, and two or more of them. */
  function openGroupModal() {
    var list = el['group-people'];
    list.innerHTML = '';
    el['group-name'].value = '';
    state.order.forEach(function (cid) {
      var c = state.contacts[cid];
      var li = document.createElement('li');
      var label = document.createElement('label');
      label.className = 'modal__person';
      var box = document.createElement('input');
      box.type = 'checkbox';
      box.value = cid;
      box.addEventListener('change', function () {
        el['group-create'].disabled = list.querySelectorAll('input:checked').length < 2;
      });
      var face = document.createElement('span');
      face.className = 'bubble__avatar';
      portraitStyle(face, c, 'center 22%');
      label.appendChild(box);
      label.appendChild(face);
      label.appendChild(document.createTextNode(c.name));
      li.appendChild(label);
      list.appendChild(li);
    });
    el['group-create'].disabled = true;
    el['group-modal'].hidden = false;
    el['group-name'].focus();
  }

  /* A group bigger than a call holds: pick who to ring, four at most — the
     ones around right now ticked to start with. */
  function openCallPick(members) {
    var list = el['call-pick-people'];
    list.innerHTML = '';
    var ticked = 0;
    function sync() {
      var n = list.querySelectorAll('input:checked').length;
      list.querySelectorAll('input').forEach(function (box) { box.disabled = !box.checked && n >= MAX_PARTY; });
      el['call-pick-go'].disabled = n < 1;
    }
    members.forEach(function (cid) {
      var c = state.contacts[cid];
      if (!c) return;
      var li = document.createElement('li');
      var label = document.createElement('label');
      label.className = 'modal__person';
      var box = document.createElement('input');
      box.type = 'checkbox';
      box.value = cid;
      if (ticked < MAX_PARTY && (c.presence || {}).status === 'online') { box.checked = true; ticked += 1; }
      box.addEventListener('change', sync);
      var face = document.createElement('span');
      face.className = 'bubble__avatar';
      portraitStyle(face, c, 'center 22%');
      label.appendChild(box);
      label.appendChild(face);
      label.appendChild(document.createTextNode(c.name));
      li.appendChild(label);
      list.appendChild(li);
    });
    sync();
    el['call-pick'].hidden = false;
  }

  function createGroup(e) {
    e.preventDefault();
    var members = Array.prototype.map.call(el['group-people'].querySelectorAll('input:checked'),
                                           function (b) { return b.value; });
    if (members.length < 2) return;
    fetch('/api/groups', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: el['group-name'].value.trim(), members: members })
    }).then(function (r) { return r.json(); }).then(function (d) {
      el['group-modal'].hidden = true;
      if (d.group) {
        state.groups[d.group.id] = d.group;
        renderGroups();
        openGroup(d.group.id);
      }
    });
  }

  /* Who's in the group and who's around right now; rename, add, remove. */
  function openGroupInfo() {
    var g = state.groups[state.groupOpen];
    if (!g) return;
    el['group-info-name'].value = g.name;
    var members = el['group-info-members'];
    members.innerHTML = '';
    g.members.forEach(function (cid) {
      var c = state.contacts[cid];
      if (!c) return;
      var li = document.createElement('li');
      li.className = 'modal__member';
      li.dataset.presence = (c.presence || {}).status || 'idle';
      var face = document.createElement('span');
      face.className = 'bubble__avatar';
      portraitStyle(face, c, 'center 22%');
      var dot = document.createElement('span');
      dot.className = 'book__dot';
      face.appendChild(dot);
      var who = document.createElement('span');
      who.className = 'modal__who';
      var name = document.createElement('span');
      name.textContent = c.name;
      var status = document.createElement('span');
      status.className = 'modal__status';
      status.textContent = presenceLabel(c);
      who.appendChild(name);
      who.appendChild(status);
      var remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'modal__remove';
      remove.textContent = 'Remove';
      remove.disabled = g.members.length <= 2;
      remove.addEventListener('click', function () {
        fetch('/api/groups/' + g.id, { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                                       body: JSON.stringify({ remove: [cid] }) });
      });
      li.appendChild(face);
      li.appendChild(who);
      li.appendChild(remove);
      members.appendChild(li);
    });
    var add = el['group-info-add'];
    add.innerHTML = '';
    var others = state.order.filter(function (cid) { return g.members.indexOf(cid) === -1; });
    el['group-info-add-label'].hidden = !others.length;
    others.forEach(function (cid) {
      var c = state.contacts[cid];
      var li = document.createElement('li');
      var label = document.createElement('label');
      label.className = 'modal__person';
      var box = document.createElement('input');
      box.type = 'checkbox';
      box.value = cid;
      var face = document.createElement('span');
      face.className = 'bubble__avatar';
      portraitStyle(face, c, 'center 22%');
      label.appendChild(box);
      label.appendChild(face);
      label.appendChild(document.createTextNode(c.name));
      li.appendChild(label);
      add.appendChild(li);
    });
    el['group-info'].hidden = false;
  }

  function saveGroupInfo() {
    var g = state.groups[state.groupOpen];
    if (!g) return;
    var adding = Array.prototype.map.call(el['group-info-add'].querySelectorAll('input:checked'),
                                          function (b) { return b.value; });
    var name = el['group-info-name'].value.trim();
    fetch('/api/groups/' + g.id, { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                                   body: JSON.stringify({ name: name !== g.name ? name : null, add: adding }) });
    el['group-info'].hidden = true;
  }

  function deleteGroup() {
    var id = state.groupOpen;
    var g = state.groups[id];
    if (!g) return;
    // The console's own question, in its own style — never the browser's.
    consoleConfirm('Delete “' + g.name + '”?', 'Everything said in it goes too.', 'Delete', function () {
      fetch('/api/groups/' + id, { method: 'DELETE' });
    });
  }

  /* A yes-or-no in the console's own dialog. */
  function consoleConfirm(title, detail, action, onYes) {
    $('confirm-title').textContent = title;
    $('confirm-detail').textContent = detail;
    $('confirm-yes').textContent = action;
    var modal = $('confirm-modal');
    var close = function () { modal.hidden = true; };
    $('confirm-yes').onclick = function () { close(); onYes(); };
    $('confirm-no').onclick = close;
    modal.hidden = false;
    $('confirm-no').focus();
  }

  /* --- notifications -------------------------------------------------------
     A message from someone whose thread isn't open: a card slides in with
     their portrait and the first line, a soft tone, and clicking it opens the
     thread. They leave on their own after a few seconds.
     ------------------------------------------------------------------------ */

  function notify(id, textBody) {
    var contact = state.contacts[id];
    if (!contact) return;
    var card = document.createElement('div');
    card.className = 'toast';
    card.style.setProperty('--contact-accent', contact.accent);
    var av = document.createElement('span');
    av.className = 'bubble__avatar';
    portraitStyle(av, contact, 'center 22%');
    var words = document.createElement('div');
    var who = document.createElement('span');
    who.className = 'toast__name';
    who.textContent = contact.name;
    var line = document.createElement('span');
    line.className = 'toast__text';
    line.textContent = textBody;
    words.appendChild(who);
    words.appendChild(line);
    card.appendChild(av);
    card.appendChild(words);
    card.addEventListener('click', function () { dismissToast(card); openMessages(id); });
    showToast(card, 't:' + id, 6500);
    if (ConsoleTones.message) ConsoleTones.message();
  }

  /* One toast per thread — a newer message replaces the older — and three at
     most on screen, the oldest going first. */
  function showToast(card, key, ms) {
    card.dataset.thread = key;
    Array.prototype.forEach.call(el.toasts.querySelectorAll('.toast'), function (t) {
      if (t.dataset.thread === key) t.remove();
    });
    el.toasts.appendChild(card);
    var live = el.toasts.querySelectorAll('.toast:not([data-leaving])');
    for (var i = 0; i < live.length - 3; i++) dismissToast(live[i]);
    setTimeout(function () { dismissToast(card); }, ms);
  }

  function dismissToast(card) {
    if (!card.isConnected || card.dataset.leaving) return;
    card.dataset.leaving = '1';
    setTimeout(function () { card.remove(); }, 300);
  }

  function clearToasts(key) {
    Array.prototype.forEach.call(el.toasts.querySelectorAll('.toast'), function (t) {
      if (t.dataset.thread === key) dismissToast(t);
    });
  }

  /* --- layout ----------------------------------------------------------------
     The directory can be dragged wider or narrower — narrow enough and it
     becomes portraits only — or hidden. The messages panel drags too. Both
     remember where you left them.
     ------------------------------------------------------------------------ */

  var COMPACT_BELOW = 150;
  // A narrow window: the directory goes to portraits, beside the stage.
  var NARROW = window.matchMedia('(max-width: 820px)');

  function remember(key, value) { try { localStorage.setItem('console.' + key, value); } catch (e) {} }
  function recall(key) { try { return localStorage.getItem('console.' + key); } catch (e) { return null; } }

  function setRailWidth(px) {
    px = Math.max(76, Math.min(px, 420));
    document.documentElement.style.setProperty('--rail-w', px + 'px');
    var compact = px < COMPACT_BELOW || NARROW.matches;
    if (document.documentElement.dataset.rail !== 'hidden') {
      document.documentElement.dataset.rail = compact ? 'compact' : 'open';
    }
    remember('railWidth', px);
  }

  /* The chat's width: dragged or remembered, but never so wide that the stage
     beside it — the call, the map — is left with less than 360px. */
  function setMessagesWidth(px, passing) {
    var rail = document.documentElement.dataset.rail === 'hidden' ? 0
      : parseInt(getComputedStyle(document.documentElement).getPropertyValue('--rail-w'), 10) || 270;
    var most = Math.max(300, Math.min(720, window.innerWidth - rail - 360));
    var width = Math.max(300, Math.min(px, most));
    document.documentElement.style.setProperty('--msg-w', width + 'px');
    if (!passing) remember('msgWidth', width);
  }

  function toggleRail() {
    var hidden = document.documentElement.dataset.rail === 'hidden';
    if (hidden) {
      document.documentElement.dataset.rail = 'open';
      setRailWidth(parseInt(recall('railWidth') || '270', 10));
    } else {
      document.documentElement.dataset.rail = 'hidden';
    }
    remember('railHidden', hidden ? '0' : '1');
  }

  function dragToResize(handle, onMove) {
    handle.addEventListener('pointerdown', function (e) {
      e.preventDefault();
      document.documentElement.dataset.resizing = '1';
      var move = function (ev) { onMove(ev.clientX); };
      var up = function () {
        delete document.documentElement.dataset.resizing;
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
      };
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up);
    });
  }

  /* The console's furniture makes small sounds: a tick as the pointer finds
     something to press, a click when it's pressed. */
  function wireKeyboardButtons() {
    document.addEventListener('keydown', function (e) {
      var t = e.target;
      if ((e.key === 'Enter' || e.key === ' ') && t && t.getAttribute && t.getAttribute('role') === 'button'
          && t.tagName !== 'BUTTON') {
        e.preventDefault();
        t.click();
      }
    });
  }

  function wireSounds() {
    document.addEventListener('pointerover', function (e) {
      var b = e.target.closest && e.target.closest('button:not(:disabled)');
      if (b && !(e.relatedTarget && b.contains(e.relatedTarget))) ConsoleTones.hover();
    });
    document.addEventListener('click', function (e) {
      if (e.target.closest && e.target.closest('button:not(:disabled)')) ConsoleTones.press();
    });
  }

  /* The console's own tooltips — never the browser's. Anything with data-tip
     gets one, in the same style as the status card, after a short hover. */
  function wireTips() {
    var tip = document.createElement('div');
    tip.className = 'tip';
    tip.hidden = true;
    document.body.appendChild(tip);
    var timer = null;
    document.addEventListener('pointerover', function (e) {
      var node = e.target.closest && e.target.closest('[data-tip]');
      clearTimeout(timer);
      if (!node || !node.dataset.tip) { tip.hidden = true; return; }
      timer = setTimeout(function () {
        if (!node.isConnected) return;      // re-rendered away meanwhile: it would measure as the corner
        tip.textContent = node.dataset.tip;
        tip.hidden = false;
        var r = node.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
        var x = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8);
        var y = r.bottom + 8 + h > window.innerHeight ? r.top - h - 8 : r.bottom + 8;
        tip.style.left = x + 'px';
        tip.style.top = y + 'px';
      }, 450);
    });
    ['pointerdown', 'scroll', 'blur'].forEach(function (name) {
      window.addEventListener(name, function () { clearTimeout(timer); tip.hidden = true; }, true);
    });
  }

  function wireLayout() {
    el['incoming-accept'].addEventListener('click', acceptIncoming);
    el['group-new'].addEventListener('click', openGroupModal);
    el['group-cancel'].addEventListener('click', function () { el['group-modal'].hidden = true; });
    el['group-form'].addEventListener('submit', createGroup);
    el['messages-delete'].addEventListener('click', deleteGroup);
    el['messages-call'].addEventListener('click', function () {
      var g = state.groups[state.groupOpen];
      if (!g) return;
      if (g.members.length > MAX_PARTY) openCallPick(g.members);
      else callGroup(g.members);
    });
    el['call-pick-cancel'].addEventListener('click', function () { el['call-pick'].hidden = true; });
    el['call-pick-form'].addEventListener('submit', function (e) {
      e.preventDefault();
      var ids = Array.prototype.map.call(el['call-pick-people'].querySelectorAll('input:checked'),
        function (box) { return box.value; });
      el['call-pick'].hidden = true;
      callGroup(ids);
    });
    el['group-info-close'].addEventListener('click', function () { el['group-info'].hidden = true; });
    el['group-info-save'].addEventListener('click', saveGroupInfo);
    el['incoming-decline'].addEventListener('click', declineIncoming);
    setRailWidth(parseInt(recall('railWidth') || '270', 10));
    if (recall('railHidden') === '1') document.documentElement.dataset.rail = 'hidden';
    setMessagesWidth(parseInt(recall('msgWidth') || '400', 10));
    window.addEventListener('resize', function () { setMessagesWidth(parseInt(recall('msgWidth') || '400', 10), true); });
    el['rail-toggle'].addEventListener('click', toggleRail);
    NARROW.addEventListener('change', function () {
      if (document.documentElement.dataset.rail !== 'hidden') setRailWidth(parseInt(recall('railWidth') || '270', 10));
    });
    dragToResize(el['rail-resize'], function (x) { setRailWidth(x); });
    dragToResize(el['messages-resize'], function (x) { setMessagesWidth(window.innerWidth - x); });
    el.inbox.addEventListener('click', function () {
      var first = Object.keys(state.unread)[0];
      if (first && first.indexOf('g:') === 0) openGroup(first.slice(2));
      else if (first) openMessages(first);
      else if (!el.messages.hidden) closeMessages();
      else openMessages(state.connectedId || state.lastThread || state.order[0]);
    });
    el['messages-thread'].addEventListener('scroll', function () {
      if (el['messages-thread'].scrollTop < 40) loadEarlier();
    });
    var openFile = function () {
      if (state.groupOpen) { openGroupInfo(); return; }
      if (!state.messagesWith) return;
      el.dossier.dataset.contact = state.messagesWith;
      openDossier(state.messagesWith);
    };
    el['messages-avatar'].addEventListener('click', openFile);
    el['messages-who'].addEventListener('click', openFile);
    el['dossier-call'].addEventListener('click', function () {
      var id = el.dossier.dataset.contact;
      el.dossier.hidden = true;
      if (!id) return;
      if (state.party.length && !onCall(id)) addToCall(id);
      else if (!state.party.length) placeCall(id);
    });
    el['dossier-message'].addEventListener('click', function () {
      var id = el.dossier.dataset.contact;
      el.dossier.hidden = true;
      if (id) openMessages(id);
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
    if (state.incomingId) closeIncoming();   // one line at a time: that call is missed
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
    // The mic opens while it rings, so the first press is instant.
    if (state.mode === 'ptt') ConsoleMic.warm();
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
      renderSeats();
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
    renderSeats();
    armIdleCheck();
  }

  /* The line-up changed: someone was added, or let go. */
  function onParty(event) {
    state.party = event.members || [];
    state.groupRinging = state.groupRinging.filter(function (id) { return state.party.indexOf(id) === -1; });
    if (state.party.length && state.connectedId && state.party.indexOf(state.connectedId) === -1
        && !event.added) {
      state.connectedId = state.currentId = state.party[0];
    }
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
    var wasAlone = el.seats.hidden;          // one-to-one until now
    renderSeats();
    syncBeds();
    renderCallbar();
    if (wasAlone && state.party.length > 1) {
      // Turned into a group mid-sentence: what was being said moves under its
      // speaker's seat and finishes there, instead of vanishing with the old line.
      var seat = state.seats[state.lastSpeaker || state.party[0]];
      if (seat) {
        seat.line.textContent = '';
        Array.prototype.slice.call(el.utterance.querySelectorAll('.said')).slice(-SUBTITLE_SENTENCES)
          .forEach(function (span) { seat.line.appendChild(span); });
        seat.fresh = false;
        state.seatLast = state.lastSpeaker || state.party[0];
      }
    }
    renderDirectory();
  }

  function hangUp(opts) {
    opts = opts || {};
    if (state.muted) { state.muted = false; paintMute(); }
    clearTimeout(bedTimer);
    ConsoleAudio.beds().forEach(ConsoleAudio.unbed);
    setTimeout(renderCallbar, 0);
    // What hadn't been spoken yet stays unspoken — not flashed up as text
    // while the line fades.
    state.pending = {};
    state.freshKeys = {};
    cancelFlush();
    clearTimeout(state.closeTimer);
    ConsoleAudio.stop();
    ConsoleTones.stopRinging();
    ConsoleTones.disconnected();
    // A call that was never answered closes with why, not "line closed".
    ConsoleSystem.say(opts.refused || 'end', opts.refusedId);
    ConsoleMic.close();
    setMode('ptt');
    clearTimeout(state.idleTimer);
    clearTimeout(state.resumeTimer);
    state.closing = false;
    state.hangUpWhenQuiet = false;
    // A switch tells the server by connecting to someone else, so the call it
    // ends can be remembered as cut short rather than simply over.
    if (!opts.switching && !opts.refused) send({ type: 'disconnect' });
    state.connectedId = null;
    state.ringingId = null;
    state.party = [];
    state.groupRinging = [];
    renderSeats();
    state.lastSpeaker = null;
    el['bar-title'].textContent = '';
    document.title = 'WayneTech Console';
    setState('idle');

    // Flash the alert colour, then let everything fade before clearing, so the
    // line visibly closes instead of blinking out.
    document.documentElement.style.removeProperty('--contact-accent');
    setLink('ending');
    clearTimeout(state.fadeTimer);
    state.fadeTimer = setTimeout(function () {
      if (state.connectedId) return;     // a new call began within the fade
      clearUtterance();
      showHeard('');
      state.fresh = true;
      setLink('off');
    }, 480);
  }

  /* A short tap that produced no take leaves "Listening" up otherwise. */
  function settleListening() {
    setTimeout(function () {
      if (document.documentElement.dataset.state === 'listening' && !state.spaceDown) setState('idle');
    }, 350);
  }

  /* --- socket ------------------------------------------------------------- */

  function send(payload) {
    if (state.socket && state.socket.readyState === WebSocket.OPEN) {
      state.socket.send(JSON.stringify(payload));
      return true;
    }
    return false;
  }

  function handle(event) {
    // Nothing on the line means nothing to render. Without this, hanging up
    // mid-reply leaves the rest of the turn still arriving: sentences queue,
    // audio plays, and a contact you just cut off keeps talking.
    // Texts arrive whether or not anyone is on a call.
    var conversational = ['notice', 'text_sent', 'text_read', 'text_typing', 'text_idle', 'text_reaction',
                          'text_reply', 'presence', 'call_incoming', 'call_unanswered',
                          'call_refused', 'group_created', 'group_deleted', 'group_sent',
                          'group_message', 'group_read', 'group_typing', 'group_idle',
                          'group_updated', 'group_reaction', 'cases', 'scanner']
                          .indexOf(event.type) === -1;
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
        state.replyFirst = true;          // its first sentence clears the board, when it's heard
        break;

      case 'sentence':
        // Held, not shown: it appears when its audio starts.
        cancelFlush();
        state.generationDone = false;
        state.pending[event.key] = event.text;
        state.pendingSpeaker[event.key] = event.speaker;
        if (state.replyFirst) { state.freshKeys[event.key] = true; state.replyFirst = false; }
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

      case 'text_sent':
        onTextSent(event);
        break;

      case 'text_read':
        onTextRead(event);
        break;

      case 'text_typing':
        onTextTyping(event);
        break;

      case 'text_idle':
        onTextIdle(event);
        break;

      case 'text_reply':
        onTextReply(event);
        break;

      case 'text_reaction':
        if (threadOpenFor(event.speaker)) {
          state.thread.messages = state.thread.messages.map(function (x) {
            return x.id === event.message.id ? event.message : x;
          });
          renderThread();
        }
        break;

      case 'presence':
        onPresence(event);
        break;

      case 'call_incoming':
        onIncoming(event);
        break;

      case 'call_unanswered':
        onUnanswered(event);
        break;

      case 'call_refused':
        onRefused(event);
        break;

      case 'group_created': case 'group_deleted': case 'group_sent': case 'group_message':
      case 'group_read': case 'group_typing': case 'group_idle': case 'group_updated':
      case 'group_reaction':
        onGroupEvent(event);
        break;

      case 'party':
        onParty(event);
        break;

      case 'cases':
      case 'scanner':
        if (window.GothamMap) GothamMap.refreshCases();
        break;

      case 'picked_up':
        // They've picked up, whether or not their first words survived — if
        // it's someone this page is ringing or has on the line. A pick-up from
        // a line he's since left behind is nothing to do with this call.
        if (event.speaker !== state.ringingId && event.speaker !== state.connectedId && !onCall(event.speaker)) break;
        answered(event.speaker);
        if (state.ringingId === event.speaker) { state.ringingId = null; ConsoleTones.stopRinging(); }
        renderSeats();
        renderDirectory();
        syncBeds();
        renderCallbar();
        break;

      case 'call_ending':
        // He said goodbye and was answered: ring off once the voice stops.
        state.hangUpWhenQuiet = true;
        break;

      case 'turn_complete':
        state.generationDone = true;
        answered();
        // A ring for someone being patched in stops when they pick up or refuse
        // (picked_up, call_refused) — not when the call's last remark finishes,
        // which made a decliner look joined for half a minute.
        // No voice arrived (degraded link): the ring mustn't sit on "thinking".
        if (!ConsoleAudio.isPlaying && document.documentElement.dataset.state === 'thinking') {
          setState('idle');
        }
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
    // One socket, ever. Unlocking during a reconnect opened a second; every
    // event then arrived twice — clips played twice, unread counts doubled.
    clearTimeout(state.reconnectTimer);
    if (state.socket && state.socket.readyState <= WebSocket.OPEN) return;
    if (state.socket) state.socket.onclose = state.socket.onmessage = null;
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var reconnecting = !!state.socket;
    state.socket = new WebSocket(proto + '//' + location.host + '/ws');
    state.socket.onopen = function () {
      // Back after a drop: whatever call or ring was up is gone server-side
      // or will be ended — reset rather than sit on a line that isn't there.
      if (!reconnecting) return;
      if (state.incomingId) closeIncoming();
      if (state.connectedId) hangUp();
      state.typing = {};
      if (state.thread) renderThread();
    };

    // Note: no auto-connect here. The socket being up is not the same as
    // someone being on the line.
    state.socket.onmessage = function (message) {
      try { handle(JSON.parse(message.data)); } catch (e) { /* malformed frame */ }
    };
    state.socket.onclose = function () {
      setState('idle');
      clearTimeout(state.reconnectTimer);
      state.reconnectTimer = setTimeout(connectSocket, 1500);
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
        send({ type: 'prompt', text: text, spoken: true, talked_over: state.talkedOver,
               confidence: typeof data.confidence === 'number' ? data.confidence : 1 });
        state.talkedOver = null;
      })
      .catch(function () { setState('idle'); });
  }

  function setMode(mode) {
    state.mode = mode;
    el.ptt.dataset.ambient = mode === 'ambient' ? '1' : '0';
    el.ptt.dataset.tip = mode === 'ambient'
      ? 'Listening — click to send now'
      : 'Hold to speak (or hold Space)';
    ConsoleMic.setMode(mode).catch(function () {
      if (mode === 'ambient') setMode('ptt');
    });
  }

  /* --- wiring ------------------------------------------------------------- */

  function toggleMute(force) {
    var on = typeof force === 'boolean' ? force : !state.muted;
    if (on === !!state.muted) { paintMute(); return; }
    state.muted = on;
    if (state.mode === 'ambient' || state.ambientWasOn) {
      // Hands-free stops listening while muted, and picks up again after.
      if (on) { state.ambientWasOn = state.mode === 'ambient'; if (state.ambientWasOn) setMode('ptt'); }
      else if (state.ambientWasOn) { state.ambientWasOn = false; setMode('ambient'); }
    }
    if (on && state.spaceDown) { state.spaceDown = false; ConsoleMic.pushStop(); settleListening(); }
    paintMute();
    if (state.connectedId) send({ type: 'mute', on: on });
  }

  function paintMute() {
    var tip = state.muted ? 'Unmute your mic' : 'Mute your mic (hold Space to talk)';
    el.ptt.dataset.muted = state.muted ? '1' : '0';
    el.ptt.dataset.tip = tip;
    el.ptt.setAttribute('aria-pressed', state.muted ? 'true' : 'false');
    el.ptt.setAttribute('aria-label', tip);
    var bar = callbarEl();
    if (bar) {
      bar.talk.dataset.muted = state.muted ? '1' : '0';
      bar.talk.dataset.tip = tip;
      bar.talk.setAttribute('aria-pressed', state.muted ? 'true' : 'false');
      bar.talk.setAttribute('aria-label', tip);
    }
  }

  // While he talks, the contact reads ahead the part of the next turn that won't
  // change with his words — so when he stops, they answer about twice as fast.
  var lastReadAhead = 0;
  function readAhead() {
    if (!state.connectedId || Date.now() - lastReadAhead < 1500) return;
    lastReadAhead = Date.now();
    send({ type: 'listening' });
  }

  function startTalking() {
    if (!state.connectedId) return;
    // In ambient mode the button means "that's it, go".
    if (state.mode === 'ambient') { ConsoleMic.cut(); return; }
    interruptHim();
    setState('listening');
    ConsoleMic.pushStart();
  }

  function stopTalking() {
    if (state.mode !== 'ambient') { ConsoleMic.pushStop(); settleListening(); }
  }

  /* --- the call, over the map ------------------------------------------------
     With the map open on a call, the line stays in reach: who's on it, a ring
     each lit by their own voice, the sentence being said, hold-to-talk, back to
     the call, and the handset. */
  var callbar = null;
  function callbarEl() {
    if (callbar) return callbar;
    var root = document.querySelector('.gm-call');
    if (!root) return null;
    callbar = { root: root, who: root.querySelector('.gm-call__who'), line: root.querySelector('.gm-call__line'),
                talk: root.querySelector('.gm-call__talk'), faces: {} };
    root.querySelector('.gm-call__end').innerHTML = ICONS.end;
    root.querySelector('.gm-call__end').addEventListener('click', function () { if (state.connectedId) hangUp(); });
    root.querySelector('.gm-call__back').addEventListener('click', function () { GothamMap.close(); });
    callbar.talk.addEventListener('click', function (e) { e.preventDefault(); toggleMute(); });
    dragCallbar(root);
    return callbar;
  }

  // Dragged by its glass, not its buttons; kept on screen, and where he left it.
  function dragCallbar(root) {
    var stage = root.parentElement, start = null;
    function place(x, y) {
      var w = stage.clientWidth, h = stage.clientHeight, bw = root.offsetWidth, bh = root.offsetHeight;
      x = Math.max(8, Math.min(w - bw - 8, x));
      y = Math.max(8, Math.min(h - bh - 8, y));
      root.style.left = x + 'px'; root.style.top = y + 'px'; root.style.bottom = 'auto';
      return { x: x, y: y };
    }
    try {
      var saved = JSON.parse(localStorage.getItem('gm-call-pos') || 'null');
      if (saved) setTimeout(function () { if (!root.hidden) place(saved.x, saved.y); root.dataset.pos = '1'; }, 0);
      if (saved) root.dataset.saved = JSON.stringify(saved);
    } catch (e) { /* no stored place */ }
    root.addEventListener('pointerdown', function (e) {
      if (e.target.closest('button')) return;
      var box = root.getBoundingClientRect(), frame = stage.getBoundingClientRect();
      start = { dx: e.clientX - box.left, dy: e.clientY - box.top, fx: frame.left, fy: frame.top };
      root.classList.add('is-dragging');
      root.setPointerCapture(e.pointerId);
    });
    root.addEventListener('pointermove', function (e) {
      if (!start) return;
      var at = place(e.clientX - start.fx - start.dx, e.clientY - start.fy - start.dy);
      root.dataset.saved = JSON.stringify(at);
    });
    ['pointerup', 'pointercancel'].forEach(function (name) {
      root.addEventListener(name, function () {
        if (!start) return;
        start = null;
        root.classList.remove('is-dragging');
        try { localStorage.setItem('gm-call-pos', root.dataset.saved || 'null'); } catch (e) { /* private window */ }
      });
    });
    root._place = place;
  }

  function renderCallbar() {
    var bar = callbarEl();
    if (!bar) return;
    var covered = (window.GothamMap && GothamMap.isOpen()) || (window.Codex && Codex.isOpen());
    var live = covered && state.connectedId;
    bar.root.hidden = !live;
    if (!live) { bar.line.textContent = ''; return; }
    try {
      var at = JSON.parse(bar.root.dataset.saved || 'null');
      if (at && bar.root._place) bar.root._place(at.x, at.y);
    } catch (e) { /* stays where the stylesheet puts it */ }
    var ids = state.party.length ? state.party.slice() : [state.connectedId];
    state.groupRinging.forEach(function (id) { if (ids.indexOf(id) === -1) ids.push(id); });
    if (state.ringingId && ids.indexOf(state.ringingId) === -1) ids.push(state.ringingId);
    Object.keys(bar.faces).forEach(function (id) {
      if (ids.indexOf(id) === -1) { bar.faces[id].remove(); delete bar.faces[id]; }
    });
    ids.forEach(function (id, i) {
      var contact = state.contacts[id];
      if (!contact) return;
      var face = bar.faces[id];
      if (!face) {
        face = bar.faces[id] = document.createElement('span');
        face.className = 'gm-call__face';
        face.style.setProperty('--accent', contact.accent);
        face.innerHTML = '<span class="gm-call__ring"></span><span class="gm-call__name"></span>';
        portraitStyle(face.querySelector('.gm-call__ring'), contact, 'center 22%');
        face.querySelector('.gm-call__name').textContent = contact.name;
        face.dataset.tip = contact.name;
        bar.who.appendChild(face);
      }
      face.style.order = i;
      face.dataset.state = (state.ringingId === id || state.groupRinging.indexOf(id) !== -1) ? 'ringing' : 'live';
    });
    bar.who.dataset.many = ids.length > 1 ? '1' : '0';
    if (!callbarLoop) callbarLoop = requestAnimationFrame(callbarPulse);
  }

  function callbarSaid(speaker, text) {
    var bar = callbarEl();
    if (!bar || bar.root.hidden) return;
    var contact = state.contacts[speaker || state.connectedId];
    bar.line.textContent = '';
    if (contact && state.party.length > 1) {
      var who = document.createElement('b');
      who.textContent = contact.name;
      who.style.color = contact.accent;
      bar.line.appendChild(who);
    }
    bar.line.appendChild(document.createTextNode(text));
    Object.keys(bar.faces).forEach(function (id) {
      bar.faces[id].dataset.speaking = id === (speaker || state.connectedId) ? '1' : '0';
    });
  }

  // The speaking ring's glow follows the voice itself, read off the analyser.
  var callbarLoop = null, callbarBins = null;
  function callbarPulse() {
    callbarLoop = null;
    var bar = callbar;
    if (!bar || bar.root.hidden) return;
    var analyser = ConsoleAudio.analyser, level = 0;
    if (analyser && ConsoleAudio.isPlaying) {
      callbarBins = callbarBins || new Uint8Array(analyser.frequencyBinCount);
      analyser.getByteFrequencyData(callbarBins);
      var sum = 0;
      for (var i = 2; i < 64; i++) sum += callbarBins[i];
      level = Math.min(1, sum / (62 * 160));
    }
    Object.keys(bar.faces).forEach(function (id) {
      var face = bar.faces[id];
      face.style.setProperty('--lvl', face.dataset.speaking === '1' ? level.toFixed(2) : '0');
    });
    callbarLoop = requestAnimationFrame(callbarPulse);
  }

  function wireInput() {
    el.compose.addEventListener('submit', function (e) {
      e.preventDefault();
      var text = el.input.value.trim();
      if (!text || !state.connectedId) return;
      interruptHim();
      el.input.value = '';
      noteActivity({ quietFor: requestedTime(text) });
      send({ type: 'prompt', text: text, talked_over: state.talkedOver });
      state.talkedOver = null;
    });

    // Space is push-to-talk; the button mutes. Muted, the line knows it.
    el.ptt.addEventListener('click', function (e) { e.preventDefault(); toggleMute(); });

    // Ambient listening has no control: push-to-talk is the one way to speak
    // until its detector stops hearing the room. setMode('ambient') still works.

    el['dossier-close'].addEventListener('click', function () { el.dossier.hidden = true; });
    el['messages-close'].addEventListener('click', closeMessages);
    // The same handset as the directory's end-call, so ending a call looks the same everywhere.
    $('end-call').innerHTML = ICONS.end;
    $('end-call').addEventListener('click', function () { if (state.connectedId) hangUp(); });
    el['messages-compose'].addEventListener('submit', sendMessage);
    el['messages-replying'].querySelector('.messages__replying-x').addEventListener('click', function () { setReplyTo(null); });
    el['dossier-save'].addEventListener('click', saveDossier);
    el.dossier.addEventListener('click', function (e) {
      if (e.target === el.dossier) el.dossier.hidden = true;
    });

    el.lock.addEventListener('click', function () {
      if (state.incomingId) closeIncoming();
      if (state.connectedId) hangUp();
      ConsoleSystem.say('lock');
      ConsoleBoot.lock();
      startBoot();
    });

    window.addEventListener('keydown', function (e) {
      if (document.documentElement.dataset.phase !== 'live') return;
      if (e.defaultPrevented) return;         // a picker or a field already dealt with it
      var typing = e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.isContentEditable);
      if (!typing && (e.key === 'c' || e.key === 'C') && !e.metaKey && !e.ctrlKey && !e.altKey && window.Codex) {
        e.preventDefault();
        if (Codex.isOpen()) Codex.close(); else Codex.open();
        return;
      }
      if (!typing && (e.key === 'm' || e.key === 'M') && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        // The button, not GothamMap.open: the map is built on first open.
        if (window.GothamMap && GothamMap.isOpen()) GothamMap.close(); else document.getElementById('map-open').click();
        return;
      }
      if (!typing && e.key === '/' && window.GothamMap && GothamMap.isOpen()) {
        e.preventDefault();
        GothamMap.search();
        return;
      }
      if (e.key === 'Escape') {
        // The topmost thing first: a dialog, the personnel file, then the map;
        // only then is it "stop talking".
        var modal = document.querySelector('.modal:not([hidden])');
        if (modal) { modal.hidden = true; return; }
        if (!el.dossier.hidden) { el.dossier.hidden = true; return; }
        if (typing) { e.target.blur(); return; }
        if (window.GothamMap && GothamMap.isOpen()) { if (!GothamMap.escape()) GothamMap.close(); return; }
        interruptHim();
        return;
      }
      if (!PTT_CODES[e.code] || state.spaceDown) return;
      // Never while typing — a text, the personnel file, a search — or Space in
      // a message opened the mic and cut the contact off. But the call's own
      // box, empty, is the mic: it's focused the moment a call connects, and
      // every press went into it instead. And a box that's been put away (the
      // Codex closed over its search) still holds focus without being typed in.
      var t = e.target;
      var inBox = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
      if (inBox && t === el.input && !t.value.trim()) inBox = false;
      if (inBox && !t.offsetParent) inBox = false;
      if (inBox) return;
      if (state.mode !== 'ptt' || !state.connectedId) return;
      e.preventDefault();
      if (state.muted) {
        el.status.textContent = 'You’re muted';
        setTimeout(function () { if (el.status.textContent === 'You’re muted') el.status.textContent = ''; }, 1600);
        return;
      }
      state.spaceDown = true;
      interruptHim();
      setState('listening');
      ConsoleMic.pushStart();
      readAhead();
    });
    window.addEventListener('keyup', function (e) {
      if (!PTT_CODES[e.code] || !state.spaceDown) return;
      state.spaceDown = false;
      ConsoleMic.pushStop();
      settleListening();
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

  /* The rooms behind the voices on the line: for each of them, where they are
     right now — asked again every minute and a half, since they move. */
  var bedTimer = null;
  function syncBeds() {
    clearTimeout(bedTimer);
    var live = state.connectedId && state.ringingId !== state.connectedId;
    var on = live ? (state.party.length ? state.party.slice() : [state.connectedId]) : [];
    ConsoleAudio.beds().forEach(function (id) { if (on.indexOf(id) === -1) ConsoleAudio.unbed(id); });
    var gated = on.length > 1;
    on.forEach(function (id) {
      fetch('/api/ambience/' + encodeURIComponent(id)).then(function (r) { return r.json(); })
        .then(function (body) {
          if (!body.scene) { ConsoleAudio.unbed(id); return; }
          ConsoleAudio.bed(id, '/api/ambience/file/' + encodeURIComponent(body.scene), gated, body.shots);
        }).catch(function () {});
    });
    if (on.length) bedTimer = setTimeout(syncBeds, 90000);
  }

  function wireAudio() {
    ConsoleAudio.on('onSentenceStart', function (text, key, durationMs, words, speaker) {
      setState('speaking');
      if (speaker && state.seats[speaker]) seatSpeaking(speaker);
      revealSentence(text, key, durationMs, words, speaker);
      var who = speaker || state.connectedId;
      ConsoleAudio.beds().forEach(function (id) { ConsoleAudio.bedLevel(id, id === who); });
    });
    ConsoleAudio.on('onIdle', function () {
      if (document.documentElement.dataset.state === 'speaking') setState('idle');
      seatSpeaking(null);
      ConsoleAudio.beds().forEach(function (id) { ConsoleAudio.bedLevel(id, false); });
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
    // Hands-free: he's started speaking — listen, and read ahead while he does.
    ConsoleMic.on('onSpeechStart', function () { setState('listening'); readAhead(); });
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
    $('dossier-status').textContent = onCall(contactId) ? 'On the call' : presenceLabel(contact);
    $('dossier-status').style.setProperty('--status', STATUS_COLOUR[(contact.presence || {}).status] || '');
    el['dossier-saved'].dataset.show = '0';
    // Reach them from their file: call (or add them to the call in progress).
    var already = onCall(contactId);
    el['dossier-call'].textContent = state.party.length ? (already ? 'On the call' : 'Add to call') : 'Call';
    el['dossier-call'].disabled = already || !contact.available
      || (state.party.length >= MAX_PARTY && !already);
    // On the file itself, not the whole console: set on the root it recoloured
    // the live call and the next ring.
    el.dossier.style.setProperty('--contact-accent', contact.accent);

    // A supplied portrait wins; otherwise the generated silhouette stands in.
    // Stacked backgrounds need no load handlers: a layer whose URL 404s paints
    // nothing, and the silhouette is last. Cropped by the profile's framing.
    portraitStyle(el['dossier-portrait'], contact, 'center 22%');

    // Not editable until it's loaded — saving "Loading…" as a bio, or the
    // last file's text into this one, was one click away.
    el['dossier-text'].value = '';
    el['dossier-text'].placeholder = 'Loading…';
    el['dossier-text'].disabled = true;
    el['dossier-save'].disabled = true;
    fetch('/api/contacts/' + contactId + '/bio')
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (el.dossier.dataset.contact !== contactId) return;
        el['dossier-text'].value = d.bio || '';
        el['dossier-text'].disabled = false;
        el['dossier-save'].disabled = false;
        el['dossier-text'].placeholder = '';
      })
      .catch(function () { el['dossier-text'].placeholder = 'Could not load the file.'; });

    el.dossier.hidden = false;
  }

  function saveDossier() {
    var id = el.dossier.dataset.contact;
    if (!id) return;
    fetch('/api/contacts/' + id + '/bio', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bio: el['dossier-text'].value })
    }).then(function (r) {
      el['dossier-saved'].textContent = r.ok ? 'Saved' : 'Not saved';
      el['dossier-saved'].dataset.show = '1';
      setTimeout(function () { el['dossier-saved'].dataset.show = '0'; }, 1800);
    });
  }

  /* --- session ------------------------------------------------------------ */

  /* The map of Gotham (gothammap.js), in the console's hands: its people are
     our contacts, its portraits ours, its Message and Call ours. */
  function wireMap() {
    var button = $('map-open');
    if (!window.GothamMap || button.dataset.wired) return;
    button.dataset.wired = '1';
    var ready = false;
    function start() {
      if (ready || !state.map) return ready;
      ready = true;
      initMap();
      return true;
    }
    button.addEventListener('click', function () {
      if (GothamMap.isOpen()) return GothamMap.close();
      if (start()) return GothamMap.open();
      // No map in the session (an older server): ask for it once more.
      fetch('/api/session').then(function (r) { return r.json(); }).then(function (info) {
        state.map = info.map || null;
        if (start()) GothamMap.open();
        else el.status.textContent = 'The map isn\u2019t available — restart the console.';
      });
    });
  }

  function initCodex() {
    if (!window.Codex) return;
    Codex.init({
      root: $('codex'),
      contacts: function () { return state.contacts; },
      portrait: function (node, c) { portraitStyle(node, c, 'center 22%'); },
      call: function (id) { placeCall(id); },
      message: function (id, draft) {
        openMessages(id);
        if (draft) { el['messages-input'].value = draft; el['messages-input'].focus(); }
      },
      showOnMap: function (x, y) {
        if (!window.GothamMap) return;
        if (!GothamMap.isOpen()) document.getElementById('map-open').click();
        setTimeout(function () { GothamMap.flyTo(x, y); }, 400);
      },
      showMap: function () {
        if (window.GothamMap && !GothamMap.isOpen()) document.getElementById('map-open').click();
      },
      // A new face: every portrait on the page fetched afresh.
      portraitChanged: function () {
        state.portraitStamp = Date.now();
        renderDirectory();
        renderCallbar();
      },
      // The Codex and the map are two tabs of one screen: opening one puts the other away.
      toggled: function (open) {
        $('codex-open').classList.toggle('is-on', open);
        if (open && window.GothamMap && GothamMap.isOpen()) GothamMap.close();
        renderCallbar();
      }
    });
    $('codex-open').addEventListener('click', function () { if (Codex.isOpen()) Codex.close(); else Codex.open(); });
  }

  function initMap() {
    GothamMap.init({
      root: $('map-view'),
      data: state.map,
      contacts: function () { return state.contacts; },
      order: function () { return state.order; },
      portrait: function (node, c) { portraitStyle(node, c, 'center 22%'); },
      label: presenceLabel,
      message: function (id) { GothamMap.close(); openMessages(id); },
      // Calling from the map keeps the map: the call comes up over it.
      call: function (id) { placeCall(id); setTimeout(renderCallbar, 60); },
      toggled: function (open) {
        $('map-open').classList.toggle('is-on', open);
        if (open && window.Codex && Codex.isOpen()) Codex.close();
        renderCallbar();
      }
    });
    var toCodex = $('map-view').querySelector('[data-act="codex"]');
    if (toCodex && window.Codex) toCodex.addEventListener('click', function () { Codex.open(); });
  }

  function loadSession() {
    return fetch('/api/session').then(function (r) { return r.json(); }).then(function (info) {
      state.map = info.map || null;
      (info.contacts || []).forEach(function (contact) {
        state.contacts[contact.id] = contact;
        state.order.push(contact.id);
      });
      // Unread kept for someone no longer in the directory would hold the
      // inbox badge up forever.
      Object.keys(state.unread).forEach(function (id) {
        if (id.indexOf('g:') !== 0 && !state.contacts[id]) delete state.unread[id];
      });
      updateInbox();
      renderDirectory();
      loadGroups();
      wireMap();
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

  /* --- the sky over the console ---------------------------------------------
     The same sun as the map's (Gotham at New York's latitude, on this clock):
     --night eases from 0 by day to 1 at night, and the stylesheet lights the
     console up as it rises — gold hour and dusk on the way. */
  function skyNight(date) {
    var year = date.getFullYear(), start = new Date(year, 0, 1);
    var day = Math.floor((date - start) / 86400000) + 1;
    var standard = Math.max(start.getTimezoneOffset(), new Date(year, 6, 1).getTimezoneOffset());
    var solar = date.getHours() + date.getMinutes() / 60 - (date.getTimezoneOffset() < standard ? 1 : 0);
    var tilt = 23.44 * Math.sin(2 * Math.PI * (284 + day) / 365) * Math.PI / 180;
    var phi = 40.7 * Math.PI / 180, ha = (solar - 12) * 15 * Math.PI / 180;
    var sun = Math.asin(Math.sin(phi) * Math.sin(tilt) + Math.cos(phi) * Math.cos(tilt) * Math.cos(ha)) * 180 / Math.PI;
    var x = Math.max(0, Math.min(1, (sun + 10) / 14));
    return 1 - x * x * (3 - 2 * x);
  }
  function paintSky() {
    var n = skyNight(new Date());
    document.documentElement.style.setProperty('--night', n.toFixed(3));
    document.documentElement.dataset.sky = n > 0.6 ? 'night' : n > 0.1 ? 'dusk' : 'day';
  }
  paintSky();
  setInterval(paintSky, 60000);

  /* --- go ----------------------------------------------------------------- */

  cacheElements();
  state.viz = new Visualizer($('viz'));
  setState('idle');
  setLink('off');
  wireInput();
  wireAudio();
  wireMic();
  wireSystem();
  wireLayout();
  wireSounds();
  wireKeyboardButtons();
  wireTips();
  wireMentions();
  wireTapbacks();
  wireEmoji();
  initCodex();
  // Back at the window with a thread open: what's on screen has been seen.
  document.addEventListener('visibilitychange', markSeen);
  try { state.unread = JSON.parse(recall('unread') || '{}') || {}; } catch (e) { state.unread = {}; }
  updateInbox();
  loadSession().then(startBoot, startBoot);
})();
