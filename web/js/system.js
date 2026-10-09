/* ==========================================================================
   The console's own voice — the Batcomputer.

   Unlocking, locking, placing a call, ending one: the moments that belong to
   the machine rather than to whoever is on the line. The server picks the
   line and keeps the audio cached, so this only has to play it. A plain
   <audio> element, deliberately not the Web Audio queue: these lines are not
   a contact speaking, so they must not drive the visualiser or the subtitles.

   Resolves with the line's text once it has finished (or at once, with "",
   when there is no system voice configured), so callers can sequence after it.
   ========================================================================== */

(function (global) {
  'use strict';

  var current = null;
  var handlers = { onLine: null };

  function say(event, contactId) {
    var url = '/api/system/' + event + (contactId ? '?contact=' + encodeURIComponent(contactId) : '');
    return fetch(url)
      .then(function (r) {
        if (r.status !== 200) return null;
        var line = r.headers.get('X-Line') || '';
        return r.blob().then(function (blob) { return { blob: blob, line: line }; });
      })
      .then(function (clip) {
        if (!clip) return '';
        return new Promise(function (resolve) {
          stop();
          var audio = new Audio(URL.createObjectURL(clip.blob));
          current = audio;
          var done = function () { if (current === audio) current = null; resolve(clip.line); };
          audio.onended = done;
          audio.onerror = done;
          audio.onpause = done;
          if (handlers.onLine) handlers.onLine(clip.line);
          audio.play().catch(done);
        });
      })
      .catch(function () { return ''; });
  }

  /** Cut the line short — someone on the call is about to speak. */
  function stop() {
    if (current) { var a = current; current = null; a.pause(); }
  }

  global.ConsoleSystem = {
    say: say,
    stop: stop,
    on: function (name, fn) { handlers[name] = fn; }
  };
})(window);
