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
    messagesWith: null, // whose text thread is open
    unread: {},       // contacts with a text reply not yet seen
    typing: {},       // contacts writing a reply
    thread: null,     // the open text thread: {id, messages, more, loading}
    lastSpeaker: null, // who said the line on screen, on a call with company
    incomingId: null  // who is calling him right now
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
     'dossier-portrait', 'messages', 'messages-avatar', 'messages-name', 'messages-role',
     'messages-close', 'messages-thread', 'messages-compose', 'messages-input',
     'messages-resize', 'messages-who', 'rail-toggle', 'rail-resize', 'inbox', 'inbox-count',
     'toasts', 'dossier-call', 'dossier-message', 'incoming', 'incoming-avatar',
     'incoming-name', 'incoming-accept', 'incoming-decline'].forEach(function (id) { el[id] = $(id); });
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
    // A space before every sentence but the first. Judged by the spans, not
    // the text: when several sentences are revealed at once their words are
    // still on timers, the text is empty, and they ran together.
    var after = el.utterance.querySelector('.said');
    el.utterance.appendChild(span);
    if (after) span.textContent = ' ';

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
    b.title = title;
    b.setAttribute('aria-label', title);
    b.innerHTML = ICONS[kind];
    b.disabled = !!disabled;
    b.addEventListener('click', function (e) { e.stopPropagation(); onClick(); });
    return b;
  }

  function renderDirectory() {
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
      text.title = contact.full_name + ' — ' + contact.role + ' (open personnel file)';
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
    refused_call: 'Call declined', unanswered_call: 'No answer'
  };

  function bubbleNode(message, contact) {
    var li = document.createElement('li');
    if (message.kind) {
      li.className = 'messages__call';
      li.innerHTML = ICONS.call;
      li.appendChild(document.createTextNode((CALL_LINE[message.kind] || 'Call') + ' · ' + clock(message.at)));
      return li;
    }
    var from = message.from === 'me' ? 'me' : 'them';
    li.className = 'bubble bubble--' + from + (message.typing ? ' bubble--typing' : '');
    if (message.id) li.dataset.id = message.id;
    if (from === 'them') {
      var av = document.createElement('span');
      av.className = 'bubble__avatar';
      portraitStyle(av, contact, 'center 22%');
      li.appendChild(av);
    }
    var t = document.createElement('span');
    t.className = 'bubble__text';
    if (message.typing) {
      t.classList.add('bubble__dots');
      t.innerHTML = '<i></i><i></i><i></i>';
    } else {
      t.textContent = message.text;
      if (message.at) {
        var time = document.createElement('span');
        time.className = 'bubble__time';
        time.textContent = clock(message.at);
        t.appendChild(time);
      }
    }
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
    var contact = state.contacts[th.id];
    var box = el['messages-thread'];
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
      var node = bubbleNode(m, contact);
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
      receipt.textContent = lastMine.read_at ? 'Read ' + clock(lastMine.read_at) : 'Delivered';
      if (lastMine.read_at) receipt.dataset.read = '1';
      box.appendChild(receipt);
    }
    if (state.typing[th.id]) box.appendChild(bubbleNode({ from: 'them', text: '', typing: true }, contact));
  }

  function scrollThreadToEnd() {
    el['messages-thread'].scrollTop = el['messages-thread'].scrollHeight;
  }

  function openMessages(id) {
    var contact = state.contacts[id];
    if (!contact) return;
    state.messagesWith = id;
    state.lastThread = id;
    delete state.unread[id];
    updateInbox();
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
        state.thread.messages = d.messages || [];
        state.thread.messages.forEach(function (m) { state.thread.seen[m.id] = true; });
        state.thread.more = (d.messages || []).length >= 40;
        state.thread.loading = false;
        if (d.typing) state.typing[id] = true; else delete state.typing[id];
        renderThread();
        scrollThreadToEnd();
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
    fetch('/api/contacts/' + th.id + '/messages?limit=40&before=' + th.messages[0].at)
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
    var r = anchor.getBoundingClientRect();
    card.style.left = (r.right + 12) + 'px';
    card.style.top = (r.top + r.height / 2 - 22) + 'px';
    card.hidden = false;
  }

  function hideHovercard() { $('hovercard').hidden = true; }

  function sendMessage(e) {
    e.preventDefault();
    var body = el['messages-input'].value.trim();
    var id = state.messagesWith;
    if (!body || !id) return;
    // Kept in the box if the link is down: a text that silently vanished was
    // worse than one that visibly didn't go.
    if (!send({ type: 'text', id: id, text: body })) return;
    el['messages-input'].value = '';
    ConsoleTones.sent();
  }

  function threadOpenFor(id) {
    return state.thread && state.thread.id === id && !el.messages.hidden;
  }

  function onTextSent(event) {
    if (!threadOpenFor(event.speaker)) return;
    state.thread.messages.push(event.message);
    renderThread();
    scrollThreadToEnd();
  }

  function onTextRead(event) {
    if (!threadOpenFor(event.speaker)) return;
    state.thread.messages.forEach(function (m) {
      if (event.ids.indexOf(m.id) !== -1) m.read_at = event.at;
    });
    renderThread();
  }

  function onTextTyping(event) {
    state.typing[event.speaker] = true;
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
    } else {
      state.unread[event.speaker] = (state.unread[event.speaker] || 0) + 1;
      updateInbox();
      renderDirectory();
      notify(event.speaker, event.message.text);
    }
    if (document.hidden) systemNotify(event.speaker, event.message.text);
  }

  /* Presence changed: the dot, the line under their name, the thread header. */
  function onPresence(event) {
    var contact = state.contacts[event.speaker];
    if (!contact) return;
    contact.presence = event.presence;
    renderDirectory();
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
    if (document.hidden) systemNotify(event.speaker, 'Incoming call');
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
    if (state.connectedId === id && state.party.length <= 1) {
      hangUp({ refused: line, refusedId: id });
      return;
    }
    if (state.ringingId === id) state.ringingId = null;
    ConsoleTones.stopRinging();
    ConsoleSystem.say(line, id);
    renderDirectory();
  }

  /* The system's own notification, for when the console isn't in front. */
  function systemNotify(id, body) {
    var contact = state.contacts[id];
    if (!contact || !('Notification' in window) || Notification.permission !== 'granted') return;
    try {
      var n = new Notification(contact.full_name, {
        body: body, tag: 'wayne-' + id, icon: '/static/portraits/' + id + '.png', silent: true
      });
      n.onclick = function () { window.focus(); openMessages(id); n.close(); };
    } catch (e) {}
  }

  function updateInbox() {
    var n = Object.keys(state.unread).reduce(function (sum, id) { return sum + (state.unread[id] || 0); }, 0);
    remember('unread', JSON.stringify(state.unread));
    el['inbox-count'].hidden = !n;
    el['inbox-count'].textContent = n;
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
    var dismiss = function () {
      card.dataset.leaving = '1';
      setTimeout(function () { card.remove(); }, 300);
    };
    card.addEventListener('click', function () { dismiss(); openMessages(id); });
    el.toasts.appendChild(card);
    if (ConsoleTones.message) ConsoleTones.message();
    setTimeout(dismiss, 6500);
  }

  /* --- layout ----------------------------------------------------------------
     The directory can be dragged wider or narrower — narrow enough and it
     becomes portraits only — or hidden. The messages panel drags too. Both
     remember where you left them.
     ------------------------------------------------------------------------ */

  var COMPACT_BELOW = 150;

  function remember(key, value) { try { localStorage.setItem('console.' + key, value); } catch (e) {} }
  function recall(key) { try { return localStorage.getItem('console.' + key); } catch (e) { return null; } }

  function setRailWidth(px) {
    px = Math.max(76, Math.min(px, 420));
    document.documentElement.style.setProperty('--rail-w', px + 'px');
    var compact = px < COMPACT_BELOW;
    if (document.documentElement.dataset.rail !== 'hidden') {
      document.documentElement.dataset.rail = compact ? 'compact' : 'open';
    }
    remember('railWidth', px);
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
  function wireSounds() {
    document.addEventListener('pointerover', function (e) {
      var b = e.target.closest && e.target.closest('button:not(:disabled)');
      if (b && !(e.relatedTarget && b.contains(e.relatedTarget))) ConsoleTones.hover();
    });
    document.addEventListener('click', function (e) {
      if (e.target.closest && e.target.closest('button:not(:disabled)')) ConsoleTones.press();
    });
    // Ask once, on a gesture, to notify when the console is behind other windows.
    document.addEventListener('click', function ask() {
      document.removeEventListener('click', ask);
      if ('Notification' in window && Notification.permission === 'default') {
        try { Notification.requestPermission(); } catch (e) {}
      }
    });
  }

  function wireLayout() {
    el['incoming-accept'].addEventListener('click', acceptIncoming);
    el['incoming-decline'].addEventListener('click', declineIncoming);
    setRailWidth(parseInt(recall('railWidth') || '270', 10));
    if (recall('railHidden') === '1') document.documentElement.dataset.rail = 'hidden';
    var w = parseInt(recall('msgWidth') || '400', 10);
    document.documentElement.style.setProperty('--msg-w', w + 'px');
    el['rail-toggle'].addEventListener('click', toggleRail);
    dragToResize(el['rail-resize'], function (x) { setRailWidth(x); });
    dragToResize(el['messages-resize'], function (x) {
      var width = Math.max(300, Math.min(window.innerWidth - x, 720));
      document.documentElement.style.setProperty('--msg-w', width + 'px');
      remember('msgWidth', width);
    });
    el.inbox.addEventListener('click', function () {
      var first = Object.keys(state.unread)[0];
      if (first) openMessages(first);
      else if (!el.messages.hidden) closeMessages();
      else openMessages(state.connectedId || state.lastThread || state.order[0]);
    });
    el['messages-thread'].addEventListener('scroll', function () {
      if (el['messages-thread'].scrollTop < 40) loadEarlier();
    });
    var openFile = function () {
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
    opts = opts || {};
    // What hadn't been spoken yet stays unspoken — not flashed up as text
    // while the line fades.
    state.pending = {};
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
    var conversational = ['notice', 'text_sent', 'text_read', 'text_typing', 'text_idle',
                          'text_reply', 'presence', 'call_incoming', 'call_unanswered',
                          'call_refused']
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
        // Someone being patched in whose greeting never came (talked over,
        // or nothing to say): they're on the call — stop ringing for them.
        if (state.ringingId && state.ringingId !== state.connectedId && onCall(state.ringingId)) {
          state.ringingId = null;
          ConsoleTones.stopRinging();
          renderDirectory();
        }
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
        if (state.mode !== 'ambient') { ConsoleMic.pushStop(); settleListening(); }
      });
    });

    el['mode-ptt'].addEventListener('click', function () { setMode('ptt'); });
    // Ambient is withheld from the UI until the detector is reliable; the
    // handler stays so re-enabling it is a one-line change in the markup.
    el['mode-ambient'].addEventListener('click', function () {
      if (!el['mode-ambient'].disabled && state.connectedId) setMode('ambient');
    });

    el['dossier-close'].addEventListener('click', function () { el.dossier.hidden = true; });
    el['messages-close'].addEventListener('click', closeMessages);
    el['messages-compose'].addEventListener('submit', sendMessage);
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
      if (e.key === 'Escape') { interruptHim(); return; }
      if (!PTT_CODES[e.code] || state.spaceDown) return;
      // Never while typing — the composer, a text, the personnel file — or
      // Space in a message opened the mic and cut the contact off.
      var t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
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

  function loadSession() {
    return fetch('/api/session').then(function (r) { return r.json(); }).then(function (info) {
      (info.contacts || []).forEach(function (contact) {
        state.contacts[contact.id] = contact;
        state.order.push(contact.id);
      });
      // Unread kept for someone no longer in the directory would hold the
      // inbox badge up forever.
      Object.keys(state.unread).forEach(function (id) {
        if (!state.contacts[id]) delete state.unread[id];
      });
      updateInbox();
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
  wireLayout();
  wireSounds();
  try { state.unread = JSON.parse(recall('unread') || '{}') || {}; } catch (e) { state.unread = {}; }
  updateInbox();
  loadSession().then(startBoot, startBoot);
})();
