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
    el.gate.dataset.auth = 'granted';
    message('Access granted', 'good');
    return delay(760).then(function () {
      el.gate.setAttribute('hidden', '');
      document.documentElement.dataset.phase = 'live';
      delete el.gate.dataset.auth;
      if (onAuthenticated) onAuthenticated();
    });
  }

  function deny() {
    el.gate.dataset.auth = '';
    el.auth.classList.add('is-denied');
    message('Access denied', 'bad');
    el.input.value = '';
    setTimeout(function () { el.auth.classList.remove('is-denied'); }, 450);
    el.input.focus();
  }

  function submit(event) {
    event.preventDefault();
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
