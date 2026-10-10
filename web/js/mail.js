/* ==========================================================================
   Mail: his inbox, the cave's secure line, the spam, what he's filed away.

   Folders down the side; the folder's mail in a list, newest first, unread in
   full; the one he picks, in full on the right — read the moment it's opened,
   and from there archived, binned, marked as spam (or not), or left unread
   again. The night report is set as the Batcomputer prints it; everything
   else as a letter.
   ========================================================================== */
(function () {
  'use strict';

  var root = null, opts = null, folder = 'inbox', items = [], openId = null, counts = {};
  var NAMES = { inbox: 'Inbox', secure: 'Secure', spam: 'Spam', archive: 'Archive' };

  function $(sel) { return root.querySelector(sel); }

  function when(at) {
    var d = new Date(at * 1000), now = new Date();
    var hm = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
    if (d.toDateString() === now.toDateString()) return hm;
    var y = new Date(now); y.setDate(now.getDate() - 1);
    if (d.toDateString() === y.toDateString()) return 'Yesterday';
    return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
  }

  function load() {
    return fetch('/api/mail?folder=' + folder).then(function (r) { return r.json(); }).then(function (d) {
      items = d.mail || [];
      counts = d.counts || {};
      if (opts.unread) opts.unread(d.unread || 0);
      render();
    }).catch(function () {});
  }

  function render() {
    root.querySelectorAll('.mx-folder').forEach(function (b) {
      b.classList.toggle('is-on', b.dataset.folder === folder);
      var n = counts[b.dataset.folder] || 0;
      b.querySelector('b').textContent = n && b.dataset.folder !== 'archive' ? n : '';
    });
    var list = $('.mx-list');
    list.innerHTML = '';
    if (!items.length) {
      var empty = document.createElement('li');
      empty.className = 'mx-list__empty';
      empty.textContent = folder === 'spam' ? 'Nothing caught.' : folder === 'archive' ? 'Nothing filed.' : 'Nothing new.';
      list.appendChild(empty);
    }
    items.forEach(function (m) {
      var li = document.createElement('li');
      li.className = 'mx-item' + (m.read ? '' : ' is-unread') + (m.id === openId ? ' is-on' : '');
      li.dataset.kind = m.kind || '';
      li.tabIndex = 0;
      li.innerHTML = '<span class="mx-item__top"><b></b><time></time></span><span class="mx-item__subject"></span>' +
                     '<span class="mx-item__snip"></span>';
      li.querySelector('b').textContent = m.from;
      li.querySelector('time').textContent = when(m.at);
      li.querySelector('.mx-item__subject').textContent = m.subject;
      li.querySelector('.mx-item__snip').textContent = (m.body || '').replace(/\s+/g, ' ').slice(0, 110);
      li.addEventListener('click', function () { show(m.id); });
      li.addEventListener('keydown', function (e) { if (e.key === 'Enter') show(m.id); });
      list.appendChild(li);
    });
    var current = items.filter(function (m) { return m.id === openId; })[0];
    paint(current || null);
  }

  function paint(m) {
    var pane = $('.mx-read');
    pane.dataset.kind = m ? (m.kind || '') : '';
    if (!m) {
      pane.innerHTML = '<p class="mx-read__none">' + (items.length ? 'Pick something to read.' : '') + '</p>';
      return;
    }
    pane.innerHTML = '<header class="mx-read__head"><h2></h2><p class="mx-read__from"><b></b> <span></span>' +
      '<time></time></p><div class="mx-read__acts"></div></header><div class="mx-read__body"></div>';
    pane.querySelector('h2').textContent = m.subject;
    pane.querySelector('.mx-read__from b').textContent = m.from;
    pane.querySelector('.mx-read__from span').textContent = m.address ? '<' + m.address + '>' : '';
    pane.querySelector('time').textContent = new Date(m.at * 1000).toLocaleString(undefined,
      { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
    pane.querySelector('.mx-read__body').textContent = m.body;
    var acts = pane.querySelector('.mx-read__acts');
    var buttons = [];
    if (m.folder !== 'archive') buttons.push(['Archive', { folder: 'archive' }]);
    else buttons.push(['Back to ' + (m.kind === 'report' || m.kind === 'gordon' ? 'Secure' : 'Inbox'),
                       { folder: m.kind === 'report' || m.kind === 'gordon' ? 'secure' : 'inbox' }]);
    if (m.folder === 'spam') buttons.push(['Not spam', { folder: 'inbox' }]);
    else if (m.folder !== 'secure') buttons.push(['Spam', { folder: 'spam' }]);
    buttons.push(['Mark unread', { read: false }]);
    buttons.push(['Delete', { delete: true }]);
    buttons.forEach(function (b) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'cx-btn' + (b[1].delete ? ' mx-del' : '');
      btn.textContent = b[0];
      btn.addEventListener('click', function () { act(m, b[1]); });
      acts.appendChild(btn);
    });
  }

  function act(m, change) {
    fetch('/api/mail/' + m.id, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                 body: JSON.stringify(change) })
      .then(function (r) { return r.json(); }).then(function (d) {
        if (opts.unread && d.unread !== undefined) opts.unread(d.unread);
        if (change.folder || change.delete) openId = null;
        return load();
      }).catch(function () {});
  }

  function show(id) {
    openId = id;
    var m = items.filter(function (x) { return x.id === id; })[0];
    if (!m) return;
    if (!m.read) {
      m.read = true;
      fetch('/api/mail/' + id, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                 body: JSON.stringify({ read: true }) })
        .then(function (r) { return r.json(); }).then(function (d) {
          if (opts.unread && d.unread !== undefined) opts.unread(d.unread);
          if (counts[folder]) counts[folder] = Math.max(0, counts[folder] - 1);
          render();
        }).catch(function () {});
    }
    render();
  }

  function open(which) {
    if (!root) return;
    if (which && NAMES[which]) folder = which;
    root.hidden = false;
    if (opts.toggled) opts.toggled(true);
    load();
  }

  function close() {
    if (!root || root.hidden) return;
    root.hidden = true;
    if (opts.toggled) opts.toggled(false);
  }

  function init(options) {
    opts = options;
    root = options.root;
    root.querySelectorAll('.mx-folder').forEach(function (b) {
      b.addEventListener('click', function () { folder = b.dataset.folder; openId = null; load(); });
    });
    $('.mx-close').addEventListener('click', close);
    root.addEventListener('keydown', function (e) { if (e.key === 'Escape') { e.stopPropagation(); close(); } });
  }

  window.Mail = {
    init: init, open: open, close: close, refresh: function () { if (root && !root.hidden) load(); },
    isOpen: function () { return !!root && !root.hidden; }
  };
})();
