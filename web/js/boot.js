/* ==========================================================================
   Power-on self test and authorisation.

   This screen earns its keep twice over. It is theatre — but it is also the
   user gesture browsers require before an AudioContext will start, so the
   console genuinely cannot come up without it. Better to make that a moment
   than an apologetic "click to enable sound" banner.

   The passcode is stagecraft, not security: it is checked so the page has
   something to check, the server gates nothing on it, and it sits in plain
   text in .env. Nothing behind it is actually protected.
   ========================================================================== */

(function (global) {
  'use strict';

  var LINES = [
    ['Core subsystems', 'nominal'],
    ['Cryptographic link', 'secure'],
    ['Local inference', 'online'],
    ['Audio subsystem', 'standby'],
    ['Directory', null]        // filled in once the session is known
  ];

  var el = {};
  var onAuthenticated = null;
  var ran = false;

  // Wrong attempts before the gate stops accepting any, and how long it stops
  // for. Escalating, because a second run of three wrong guesses is a different
  // thing from the first — but capped at a minute: this is a console you are
  // meant to get into, and punishing a typo with a five-minute wait would be
  // the kind of security theatre that only ever inconveniences the owner.
  //
  // Client-side, like the passcode itself. It raises the cost of guessing at a
  // keyboard, which is the threat this screen is actually for; it stops nobody
  // who opens the network tab. See the note at the top of this file.
  var MAX_ATTEMPTS = 3;
  var LOCKOUTS_MS = [15000, 30000, 60000];

  var attempts = 0;
  var lockouts = 0;
  var lockedUntil = 0;
  var lockTimer = null;

  function isLocked() { return Date.now() < lockedUntil; }

  function delay(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function addLine(label, value, ok) {
    var li = document.createElement('li');
    var name = document.createElement('span');
    name.textContent = label;
    var state = document.createElement('b');
    state.textContent = value;
    li.appendChild(name);
    li.appendChild(state);
    if (ok) li.dataset.ok = '1';
    el.post.appendChild(li);
  }

  function runPost(contactCount) {
    return LINES.reduce(function (chain, line, index) {
      return chain.then(function () {
        return delay(index === 0 ? 260 : 180 + Math.random() * 160);
      }).then(function () {
        var value = line[1];
        if (value === null) {
          value = contactCount + (contactCount === 1 ? ' contact' : ' contacts');
        }
        addLine(line[0], value, true);
      });
    }, Promise.resolve());
  }

  function message(text, tone) {
    el.msg.textContent = text;
    if (tone) { el.msg.dataset.tone = tone; } else { delete el.msg.dataset.tone; }
  }

  function grant() {
    attempts = 0;
    lockouts = 0;
    el.gate.dataset.auth = 'granted';
    message('Access granted', 'good');
    tone('granted');
    return delay(760).then(function () {
      el.gate.setAttribute('hidden', '');
      document.documentElement.dataset.phase = 'live';
      if (onAuthenticated) onAuthenticated();
      // Only once the gate has finished fading. Clearing it here — as this used
      // to, in the same tick as hiding — dropped the granted styles instantly,
      // so the rings snapped back to full brightness and speed while the panel
      // was still visibly on screen.
      setTimeout(function () { delete el.gate.dataset.auth; }, 800);
    });
  }

  /**
   * Play one of the gate's tones, if the audio context will have us.
   *
   * Everything here runs inside a form submit, which is the gesture browsers
   * want before an AudioContext will start — but the context may still be
   * suspended on the first attempt, so it is resumed rather than assumed. A
   * silent failure is correct: no sound is a worse console, not a broken one.
   */
  function tone(name) {
    try {
      global.ConsoleAudio.resume().then(function () {
        global.ConsoleTones[name]();
      }, function () {});
    } catch (err) { /* no audio; the screen still works */ }
  }

  function deny() {
    el.gate.dataset.auth = '';
    el.auth.classList.add('is-denied');
    el.input.value = '';
    setTimeout(function () { el.auth.classList.remove('is-denied'); }, 450);

    attempts += 1;
    if (attempts >= MAX_ATTEMPTS) {
      attempts = 0;
      beginLockout();
      return;
    }
    var left = MAX_ATTEMPTS - attempts;
    message('Access denied — ' + left + ' attempt' + (left === 1 ? '' : 's') +
            ' remaining', 'bad');
    tone('denied');
    el.input.focus();
  }

  function beginLockout() {
    var span = LOCKOUTS_MS[Math.min(lockouts, LOCKOUTS_MS.length - 1)];
    lockouts += 1;
    lockedUntil = Date.now() + span;
    el.gate.dataset.auth = 'locked';
    el.input.disabled = true;
    tone('lockedOut');

    clearInterval(lockTimer);
    lockTimer = setInterval(function () {
      var left = Math.ceil((lockedUntil - Date.now()) / 1000);
      if (left > 0) {
        message('Terminal locked — ' + left + 's', 'bad');
        global.ConsoleTones.tick();
        return;
      }
      clearInterval(lockTimer);
      lockTimer = null;
      el.gate.dataset.auth = '';
      el.input.disabled = false;
      message('Passcode required');
      el.input.focus();
    }, 1000);
    message('Terminal locked — ' + Math.round(span / 1000) + 's', 'bad');
  }

  function submit(event) {
    event.preventDefault();
    if (isLocked()) return;
    var code = el.input.value;
    if (!code) return;

    el.gate.dataset.auth = 'checking';
    message('Verifying…');

    // A beat of deliberate latency: an instant answer reads as a text
    // comparison, which is exactly what it is.
    Promise.all([
      fetch('/api/unlock', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ passcode: code })
      }).then(function (r) { return r.json(); }).catch(function () { return { ok: false }; }),
      delay(620)
    ]).then(function (results) {
      if (results[0].ok) {
        global.ConsoleAudio.resume().then(grant, grant);
      } else {
        deny();
      }
    });
  }

  function start(options) {
    el.gate = document.getElementById('gate');
    el.post = document.getElementById('post');
    el.auth = document.getElementById('auth');
    el.input = document.getElementById('passcode');
    el.msg = document.getElementById('auth-msg');
    onAuthenticated = options.onAuthenticated;

    el.auth.addEventListener('submit', submit);

    if (ran) return;
    ran = true;

    runPost(options.contactCount || 0).then(function () {
      return delay(300);
    }).then(function () {
      el.auth.hidden = false;
      el.input.focus();
      message('Passcode required');
    });
  }

  /** Return to the lock screen without dropping the socket. */
  function lock() {
    el.post.innerHTML = '';
    el.auth.hidden = true;
    el.input.value = '';
    message('');
    el.gate.removeAttribute('hidden');
    document.documentElement.dataset.phase = 'boot';
    ran = false;
  }

  global.ConsoleBoot = { start: start, lock: lock };
})(window);
