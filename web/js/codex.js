/* ==========================================================================
   The Codex: who's who and what's where.

   People — the family, the company, the city, the friends — and every rogue,
   each as a file: the vital details, then one biography his to rewrite (a
   contact's is the directory's own, the same words in both places). A rogue's
   file adds how they work, who with, their weakness, his contingency, and
   where they are right now: loose, in custody, Arkham, Blackgate. Every place
   on the map, its description his to rewrite. Only his contacts are in his
   phone: anyone else is reached through someone who knows them.

   Faces: click one to give it a picture — a contact's lands where the
   directory looks (web/portraits/<id>), anyone else's in web/portraits/codex/.
   ========================================================================== */
(function () {
  'use strict';

  var root = null, opts = null, data = null, tab = 'people', picked = {}, query = '';

  function $(sel) { return root.querySelector(sel); }
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }
  function initials(name) {
    return (name || '').replace(/^(Dr\.|The)\s+/i, '').split(/\s+/).slice(0, 2).map(function (w) { return w[0]; }).join('');
  }

  // Where a rogue is, as a heading and a colour.
  function whereabouts(r) {
    if (r.status === 'at large') return 'At large';
    if (r.status === 'in custody') return 'In GCPD custody';
    if (r.status === 'locked up') return r.held || 'Locked up';
    return 'Whereabouts unknown';
  }
  var STATUS_TONE = { 'at large': 'var(--alert)', 'in custody': '#e0a43a', 'locked up': '#58c08a', 'unknown': 'var(--text-faint)' };
  var THREAT = { 1: '#7fa9c8', 2: '#c9a23a', 3: '#e08a3f', 4: '#ff3b3b' };
  var GROUPS = ['Family', 'Wayne Enterprises', 'Gotham', 'GCPD', 'Friends'];
  var WHERE_ORDER = ['At large', 'In GCPD custody', 'Arkham Asylum', 'Blackgate Penitentiary', 'Whereabouts unknown'];
  var KINDS = { cafe: 'Café', fire: 'Firehouse', news: 'Newsroom', civic: 'Civic building', industry: 'Industry',
                dock: 'Docks', circus: 'Fairground', nightlife: 'Nightlife', clock: 'Clocktower', naval: 'Naval yard' };
  function kindOf(entry) {
    if (entry.kind === 'district') return 'District';
    if (entry.kind === 'quarter') return 'Quarter';
    var word = entry.venue || entry.icon || entry.kind || '';
    return KINDS[word] || (word ? word[0].toUpperCase() + word.slice(1) : '');
  }

  function load() {
    return fetch('/api/codex').then(function (r) { return r.json(); }).then(function (body) {
      data = body;
      render();
    });
  }

  /* --- faces ---------------------------------------------------------------- */

  // A face as the directory shows it: a contact's through the directory's own
  // portrait and framing, anyone else's picture framed as he set it.
  function face(node, entry, ring) {
    node.style.setProperty('--ring', ring || 'var(--hairline-2)');
    node.textContent = '';
    node.classList.remove('has-portrait');
    node.style.backgroundImage = node.style.backgroundSize = node.style.backgroundPosition = '';
    var contact = entry.contact && opts.contacts()[entry.contact];
    if (contact) { opts.portrait(node, contact); node.classList.add('has-portrait'); return; }
    if (entry.portrait) {
      node.style.backgroundImage = "url('" + entry.portrait + "')";
      var frame = entry.frame || {};
      if (frame.size) node.style.backgroundSize = frame.size;
      if (frame.position) node.style.backgroundPosition = frame.position;
      node.classList.add('has-portrait');
      return;
    }
    node.textContent = tab === 'places' ? (entry.venue || entry.kind || '?')[0].toUpperCase() : initials(entry.name);
  }

  /* Framing a face: drag the picture to choose the part that shows, the slider
     (or the wheel) for how close — the circle shows it as everywhere will. */
  function framer(entry, onDone) {
    var contact = entry.contact && opts.contacts()[entry.contact];
    var url = entry.portrait;
    var start = (contact ? contact.portrait : entry.frame) || {};
    var shade = el('div', 'cx-modal');
    shade.setAttribute('role', 'dialog');
    shade.setAttribute('aria-label', 'Adjust photo');
    var box = el('div', 'cx-frame');
    var view = el('div', 'cx-frame__view');
    var zoom = el('input', 'cx-frame__zoom');
    zoom.type = 'range';
    var row = el('div', 'cx-actions cx-actions--tight');
    var save = el('button', 'cx-btn cx-btn--go', 'Save'), reset = el('button', 'cx-btn', 'Reset'), cancel = el('button', 'cx-btn', 'Cancel');
    [save, reset, cancel].forEach(function (b) { b.type = 'button'; row.appendChild(b); });
    box.appendChild(el('p', 'cx-frame__title', entry.name));
    box.appendChild(el('p', 'cx-label', 'Drag to place · slide or scroll to zoom'));
    box.appendChild(view);
    box.appendChild(zoom);
    box.appendChild(row);
    shade.appendChild(box);
    root.appendChild(shade);
    function close_() { shade.remove(); document.removeEventListener('keydown', onKey, true); }
    function onKey(e) { if (e.key === 'Escape') { e.stopPropagation(); close_(); } }
    document.addEventListener('keydown', onKey, true);
    shade.addEventListener('pointerdown', function (e) { if (e.target === shade) close_(); });

    // The picture's own shape decides what "fills the circle" is.
    var src = entry.portrait || url || (function () {
      var probe = el('span');
      opts.portrait(probe, contact);
      var m = /url\(['"]?([^'")]+)['"]?\)/.exec(probe.style.backgroundImage || '');
      return m ? m[1] : '';
    })();
    var img = new Image(), aspect = 1, size = 100, px = 50, py = 22;
    function paint() {
      view.style.backgroundImage = "url('" + src + "')";
      view.style.backgroundSize = size.toFixed(1) + '%';
      view.style.backgroundPosition = px.toFixed(1) + '% ' + py.toFixed(1) + '%';
    }
    function fill() { return 100 * Math.max(1, aspect); }          // width % at which it just covers the circle
    function fromFrame(f) {
      size = parseFloat(f.size) || fill();
      var pos = (f.position || '50% 22%').split(/\s+/);
      px = parseFloat(pos[0]); py = parseFloat(pos[1]);
      if (isNaN(px)) px = 50;
      if (isNaN(py)) py = 22;
      zoom.min = String(Math.round(fill()));
      zoom.max = String(Math.round(fill() * 3.5));
      zoom.value = String(size);
      paint();
    }
    img.onload = function () { aspect = img.naturalWidth / img.naturalHeight; fromFrame(start); };
    img.onerror = function () { fromFrame(start); };
    img.src = src;

    zoom.addEventListener('input', function () { size = parseFloat(zoom.value); paint(); });
    view.addEventListener('wheel', function (e) {
      e.preventDefault();
      size = Math.max(parseFloat(zoom.min), Math.min(parseFloat(zoom.max), size * (e.deltaY < 0 ? 1.06 : 1 / 1.06)));
      zoom.value = String(size);
      paint();
    }, { passive: false });
    view.addEventListener('pointerdown', function (e) {
      view.setPointerCapture(e.pointerId);
      var x0 = e.clientX, y0 = e.clientY, px0 = px, py0 = py, w = view.clientWidth;
      // How far the picture overhangs the circle, in px — a drag that far is the whole range.
      var over = function () {
        var rw = w * size / 100, rh = rw / aspect;
        return [Math.max(1, rw - w), Math.max(1, rh - w)];
      };
      function move(ev) {
        var o = over();
        px = Math.max(0, Math.min(100, px0 - (ev.clientX - x0) / o[0] * 100));
        py = Math.max(0, Math.min(100, py0 - (ev.clientY - y0) / o[1] * 100));
        paint();
      }
      function up() { view.removeEventListener('pointermove', move); view.removeEventListener('pointerup', up); }
      view.addEventListener('pointermove', move);
      view.addEventListener('pointerup', up);
    });
    function send(frame) {
      var q = contact ? '?contact=' + encodeURIComponent(entry.contact) : '';
      return fetch('/api/codex/frame/' + encodeURIComponent(entry.id) + q, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(frame)
      }).then(function (r) { return r.json(); }).then(function (body) {
        var f = body.frame || {};
        if (contact) { contact.portrait = f; if (opts.portraitChanged) opts.portraitChanged(entry.contact); }
        else entry.frame = f;
        close_();
        onDone();
      });
    }
    save.addEventListener('click', function () { send({ size: size.toFixed(1) + '%', position: px.toFixed(1) + '% ' + py.toFixed(1) + '%' }); });
    reset.addEventListener('click', function () { send({ size: '', position: '' }); });
    cancel.addEventListener('click', close_);
  }

  /* Any picture, made a portrait: drawn at no more than 768 px on its longer
     side and saved as PNG, so whatever was dropped — a phone photo, a webp, a
     screenshot — lands as <name>.png where the page looks for it. */
  function asPortrait(file) {
    return new Promise(function (resolve, reject) {
      var img = new Image(), src = URL.createObjectURL(file);
      img.onload = function () {
        var scale = Math.min(1, 768 / Math.max(img.naturalWidth, img.naturalHeight));
        var canvas = document.createElement('canvas');
        canvas.width = Math.round(img.naturalWidth * scale);
        canvas.height = Math.round(img.naturalHeight * scale);
        var ctx = canvas.getContext('2d');
        ctx.imageSmoothingQuality = 'high';
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        URL.revokeObjectURL(src);
        canvas.toBlob(function (blob) { if (blob) resolve(blob); else reject(new Error('no image')); }, 'image/png');
      };
      img.onerror = function () { URL.revokeObjectURL(src); reject(new Error('not an image')); };
      img.src = src;
    });
  }

  // Click a face (or drop a picture on it) to give it one.
  function uploadable(node, entry) {
    if (tab === 'places') return;
    var pick = el('input');
    pick.type = 'file';
    pick.accept = 'image/*';
    pick.hidden = true;
    node.appendChild(pick);
    node.classList.add('is-uploadable');
    node.dataset.tip = 'Add a picture';
    function send(file) {
      if (!file || !/^image\//.test(file.type)) return;
      var url = '/api/codex/portrait/' + encodeURIComponent(entry.id) + (entry.contact ? '?contact=' + encodeURIComponent(entry.contact) : '');
      asPortrait(file)
        .then(function (png) { return fetch(url, { method: 'POST', headers: { 'Content-Type': 'image/png' }, body: png }); })
        .then(function (r) { return r.json(); })
        .then(function (body) {
          if (!body.portrait) return;
          entry.portrait = body.portrait;
          entry.frame = {};
          var contact = entry.contact && opts.contacts()[entry.contact];
          if (contact) contact.portrait = {};
          if (entry.contact && opts.portraitChanged) opts.portraitChanged(entry.contact);
          render();
          framer(entry, function () { detail(entry); });
        }).catch(function () {});
    }
    node.addEventListener('click', function () { pick.click(); });
    pick.addEventListener('change', function () { send(pick.files[0]); pick.value = ''; });
    node.addEventListener('dragover', function (e) { e.preventDefault(); node.classList.add('is-dropping'); });
    node.addEventListener('dragleave', function () { node.classList.remove('is-dropping'); });
    node.addEventListener('drop', function (e) {
      e.preventDefault(); node.classList.remove('is-dropping');
      send(e.dataTransfer.files && e.dataTransfer.files[0]);
    });
  }

  function matches(entry) {
    if (!query) return true;
    var hay = [entry.name, entry.alias, entry.real, entry.area, entry.occupation, entry.kind, entry.held,
               (entry.haunts || []).join(' ')].join(' ').toLowerCase();
    return hay.indexOf(query) !== -1;
  }

  /* --- the list ------------------------------------------------------------- */

  function render() {
    if (!data) return;
    root.querySelectorAll('.cx-tab').forEach(function (b) {
      b.classList.toggle('is-on', b.dataset.tab === tab);
      b.setAttribute('aria-selected', b.dataset.tab === tab ? 'true' : 'false');
    });
    $('.cx-tab[data-tab="people"] b').textContent = data.people.length;
    var loose = data.rogues.filter(function (r) { return r.status === 'at large'; }).length;
    $('.cx-tab[data-tab="rogues"] b').textContent = loose + ' loose';
    $('.cx-tab[data-tab="places"] b').textContent = data.places.length;
    var list = $('.cx-list');
    list.innerHTML = '';
    var entries = tab === 'people' ? data.people : tab === 'rogues' ? data.rogues : data.places;
    var shown = entries.filter(matches);
    var groupOf = tab === 'people' ? function (e) { return e.group; }
      : tab === 'rogues' ? function (e) { return whereabouts(e); }
      : function (e) { return e.area || 'Gotham'; };
    var order = tab === 'people' ? GROUPS : tab === 'rogues' ? WHERE_ORDER : null;
    var groups = {};
    shown.forEach(function (e) { (groups[groupOf(e)] = groups[groupOf(e)] || []).push(e); });
    var names = order ? order.filter(function (g) { return groups[g]; })
      .concat(Object.keys(groups).filter(function (g) { return order.indexOf(g) === -1; })) : Object.keys(groups).sort();
    names.forEach(function (g) {
      list.appendChild(el('li', 'cx-group', g + ' · ' + groups[g].length));
      groups[g].forEach(function (e) { list.appendChild(row(e)); });
    });
    if (!shown.length) list.appendChild(el('li', 'cx-empty', 'Nothing matches.'));
    var current = picked[tab];
    var still = current && shown.filter(function (e) { return e.name === current; })[0];
    detail(still || shown[0] || null);
  }

  function ringFor(entry) {
    if (tab === 'rogues') return THREAT[entry.threat];
    var contact = entry.contact && opts.contacts()[entry.contact];
    return contact ? contact.accent : null;
  }

  function row(entry) {
    var li = el('li', 'cx-row');
    li.tabIndex = 0;
    li.setAttribute('role', 'button');
    li.dataset.name = entry.name;
    var pic = el('span', 'cx-face cx-face--small' + (tab === 'places' ? ' cx-face--place' : ''));
    face(pic, entry, ringFor(entry));
    li.appendChild(pic);
    var text = el('span', 'cx-row__text');
    text.appendChild(el('b', '', entry.name));
    var sub = tab === 'people' ? (entry.alias || entry.occupation || '') : tab === 'rogues' ? (entry.real || '') : kindOf(entry);
    text.appendChild(el('small', '', sub));
    li.appendChild(text);
    if (tab === 'people' && entry.callable) li.appendChild(el('i', 'cx-row__phone', '☎'));
    if (tab === 'people' && entry.you) li.appendChild(el('i', 'cx-row__you', 'You'));
    if (tab === 'rogues') {
      var dot = el('i', 'cx-row__status');
      dot.style.setProperty('--tone', STATUS_TONE[entry.status] || 'var(--text-faint)');
      dot.dataset.tip = whereabouts(entry);
      li.appendChild(dot);
    }
    if (tab !== 'people' && entry.edited) li.appendChild(el('i', 'cx-row__you', 'Edited'));
    li.addEventListener('click', function () { detail(entry); });
    li.addEventListener('keydown', function (e) { if (e.key === 'Enter') detail(entry); });
    return li;
  }

  /* --- the card ------------------------------------------------------------- */

  function stat(grid, label, value) {
    if (value === undefined || value === null || value === '') return;
    var cell = el('div', 'cx-stat');
    cell.appendChild(el('span', '', label));
    cell.appendChild(el('b', '', String(value)));
    grid.appendChild(cell);
  }

  // The file's longer entries, a row each: label on the left, the entry beside it.
  function fileRows(card, rows) {
    rows = rows.filter(function (r) { return r[1]; });
    if (!rows.length) return;
    var list = el('dl', 'cx-file');
    rows.forEach(function (r) {
      list.appendChild(el('dt', '', r[0]));
      list.appendChild(el('dd', '', r[1]));
    });
    card.appendChild(list);
  }

  // The one text on a card, his to rewrite: saved on blur or with the button, quietly.
  function editable(card, label, value, placeholder, save) {
    card.appendChild(el('p', 'cx-label', label));
    var box = el('textarea', 'cx-edit');
    box.value = value || '';
    box.placeholder = placeholder;
    box.rows = Math.min(12, Math.max(4, Math.ceil((value || '').length / 80) + 1));
    card.appendChild(box);
    var row = el('div', 'cx-actions cx-actions--tight');
    var said = el('span', 'cx-saved', '');
    var btn = el('button', 'cx-btn', 'Save');
    btn.type = 'button';
    var last = box.value;
    function commit() {
      if (box.value === last) return;
      last = box.value;
      save(box.value).then(function () { said.textContent = 'Saved.'; setTimeout(function () { said.textContent = ''; }, 1600); })
        .catch(function () { said.textContent = 'Couldn’t save.'; });
    }
    btn.addEventListener('click', commit);
    box.addEventListener('blur', commit);
    row.appendChild(btn);
    row.appendChild(said);
    card.appendChild(row);
    // Filled in later (a text still loading): taken as what's saved, not an edit.
    box.fill = function (text) {
      box.value = last = text || '';
      box.rows = Math.min(12, Math.max(4, Math.ceil(box.value.length / 80) + 1));
    };
    return box;
  }

  /* The biography: one text per person or rogue. A contact's is the directory's
     own file on them — read from it, saved to it, the same words in both places.
     Anyone else's is the Codex's, his to rewrite; emptied, it goes back to the
     file as it was. A note kept separately before is folded into it. */
  function biography(card, entry, kind) {
    if (entry.contact) {
      var url = '/api/contacts/' + encodeURIComponent(entry.contact) + '/bio';
      var file = editable(card, 'Biography', entry.dossier, 'Who they are, in your words.', function (text) {
        entry.dossier = text;
        return post(url, { bio: text });
      });
      file.disabled = true;
      fetch(url).then(function (r) { return r.json(); }).then(function (d) {
        file.fill(d.bio || '');
        entry.dossier = d.bio || '';
      }).catch(function () {}).then(function () { file.disabled = false; });
      return;
    }
    var text = [entry.bio, entry.note].filter(Boolean).join('\n\n');
    editable(card, 'Biography', text, 'Who they are, in your words.', function (next) {
      var cleared = entry.note ? post('/api/codex/note', { kind: kind, name: entry.name, note: '' }) : Promise.resolve();
      return cleared.then(function () {
        entry.note = '';
        return post('/api/codex/bio', { kind: kind, name: entry.name, bio: next });
      }).then(function (r) {
        if (!next.trim()) load(); else { entry.bio = next; entry.edited = true; }
        return r;
      });
    });
  }

  function post(url, body) {
    return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); });
  }

  // A haunt by name: on the map if it's there, else its card under Places.
  function showPlace(name) {
    var key = name.toLowerCase();
    var first = key.split(/\s+/)[0];
    var found = data.places.filter(function (p) { return p.name.toLowerCase() === key; })[0]
      || data.places.filter(function (p) { return p.name.toLowerCase().indexOf(key) !== -1 || key.indexOf(p.name.toLowerCase()) !== -1; })[0]
      || data.places.filter(function (p) { return p.name.toLowerCase().split(/\s+/)[0] === first; })[0];
    if (found && found.x !== undefined && found.x !== null) { close(); opts.showOnMap(found.x, found.y); return; }
    tab = 'places';
    query = key;
    $('.cx-search').value = name;
    render();
  }

  function mapButton(row, x, y, label) {
    if (x === undefined || x === null) return;
    var go = el('button', 'cx-btn', label || 'Show on the map');
    go.type = 'button';
    go.addEventListener('click', function () { close(); opts.showOnMap(x, y); });
    row.appendChild(go);
  }

  function detail(entry) {
    var card = $('.cx-card');
    card.innerHTML = '';
    root.querySelectorAll('.cx-row').forEach(function (r) { r.classList.toggle('is-on', !!entry && r.dataset.name === entry.name); });
    if (!entry) { card.appendChild(el('p', 'cx-empty', 'Nothing here.')); return; }
    picked[tab] = entry.name;
    var top = el('div', 'cx-card__top');
    var pic = el('span', 'cx-face cx-face--big' + (tab === 'places' ? ' cx-face--place' : ''));
    face(pic, entry, ringFor(entry));
    uploadable(pic, entry);
    top.appendChild(pic);
    var head = el('div', 'cx-card__head');
    var kicker = el('p', 'cx-card__kicker', tab === 'people' ? (entry.group || '') : tab === 'rogues' ? whereabouts(entry) : (entry.area || ''));
    if (tab === 'rogues') kicker.style.color = STATUS_TONE[entry.status];
    head.appendChild(kicker);
    head.appendChild(el('h2', 'cx-card__name', entry.name));
    var sub = entry.alias || entry.real || (tab === 'places' ? kindOf(entry) : '');
    if (sub && sub !== entry.name) head.appendChild(el('p', 'cx-card__alias', sub));
    if (tab !== 'places' && (entry.portrait || entry.contact)) {
      var adjust = el('button', 'cx-link', 'Adjust photo');
      adjust.type = 'button';
      adjust.addEventListener('click', function () {
        if (root.querySelector('.cx-modal')) return;
        framer(entry, function () { detail(entry); });
      });
      head.appendChild(adjust);
    }
    top.appendChild(head);
    card.appendChild(top);
    if (tab === 'people') return person(card, entry);
    if (tab === 'rogues') return rogue(card, entry);
    return place(card, entry);
  }

  function person(card, entry) {
    var grid = el('div', 'cx-stats');
    stat(grid, 'Age', entry.age); stat(grid, 'Born', entry.born); stat(grid, 'Height', entry.height);
    stat(grid, 'Weight', entry.weight); stat(grid, 'Eyes', entry.eyes); stat(grid, 'Hair', entry.hair);
    stat(grid, 'Home', entry.home); stat(grid, 'Work', entry.occupation);
    card.appendChild(grid);
    card.appendChild(reach(entry));
    biography(card, entry, 'people');
    if (entry.first || entry.canon) {
      card.appendChild(el('p', 'cx-foot', [entry.first ? 'First seen: ' + entry.first : '', entry.canon || ''].filter(Boolean).join(' · ')));
    }
  }

  function rogue(card, entry) {
    var grid = el('div', 'cx-stats');
    stat(grid, 'Now', whereabouts(entry));
    stat(grid, 'Threat', '●●●●'.slice(0, entry.threat || 0) + '○○○○'.slice(entry.threat || 0));
    stat(grid, 'Age', entry.age); stat(grid, 'Height', entry.height); stat(grid, 'Weight', entry.weight);
    stat(grid, 'Eyes', entry.eyes); stat(grid, 'Hair', entry.hair);
    card.appendChild(grid);
    var row = el('div', 'cx-actions cx-actions--tight');
    mapButton(row, entry.x, entry.y, entry.status === 'at large' ? 'Their ground on the map' : 'Where they’re held');
    if (entry.status_how) row.appendChild(el('span', 'cx-saved', entry.status_how[0].toUpperCase() + entry.status_how.slice(1) + '.'));
    card.appendChild(row);
    // The file proper: how they work, who with, what stops them, and what he keeps ready.
    fileRows(card, [['How they work', entry.mo], ['Associates', entry.associates], ['Weakness', entry.weakness],
                    ['Contingency', entry.contingency], ['Lately', entry.encountered_how ? 'Suspected in the ' + entry.encountered_how : '']]);
    if ((entry.haunts || []).length) {
      var chips = el('div', 'cx-chips cx-chips--file');
      chips.appendChild(el('span', 'cx-chips__label', 'Haunts'));
      entry.haunts.forEach(function (h) {
        var chip = el('button', 'cx-chip', h);
        chip.type = 'button';
        chip.addEventListener('click', function () { showPlace(h); });
        chips.appendChild(chip);
      });
      card.appendChild(chips);
    }
    biography(card, entry, 'rogues');
    if (entry.first) card.appendChild(el('p', 'cx-foot', 'First seen: ' + entry.first));
  }

  function place(card, entry) {
    // His own line on it, as he'd put it — under the name, not a second box to fill in.
    if (entry.note) card.appendChild(el('blockquote', 'cx-quote', entry.note));
    var row = el('div', 'cx-actions cx-actions--tight');
    mapButton(row, entry.x, entry.y);
    card.appendChild(row);
    // What everyone knows of it: rewrite it and they all will.
    editable(card, 'Description', entry.bio, 'What it is, and what goes on there.', function (text) {
      return post('/api/codex/place', { name: entry.name, bio: text }).then(function (r) {
        if (!text.trim()) load(); else { entry.bio = text; entry.edited = true; }
        return r;
      });
    });
    if (entry.canon) card.appendChild(el('p', 'cx-foot', 'From: ' + entry.canon));
  }

  function reach(entry) {
    var box = el('div', 'cx-actions');
    if (entry.you) { box.appendChild(el('span', 'cx-saved', 'That’s you.')); return box; }
    if (entry.callable) {
      var call = el('button', 'cx-btn cx-btn--go', 'Call');
      call.type = 'button';
      call.addEventListener('click', function () { close(); opts.call(entry.contact); });
      var text = el('button', 'cx-btn', 'Message');
      text.type = 'button';
      text.addEventListener('click', function () { close(); opts.message(entry.contact, ''); });
      box.appendChild(call);
      box.appendChild(text);
      return box;
    }
    // Not in his phone: the people who could pass a message on.
    box.appendChild(el('span', 'cx-label cx-label--inline', 'Not in your phone — reach through'));
    (entry.reach || []).forEach(function (who) {
      var b = el('button', 'cx-btn', who.name);
      b.type = 'button';
      b.dataset.tip = 'Text ' + who.name + ' to get a message to ' + entry.name.split(' ')[0];
      b.addEventListener('click', function () {
        close();
        opts.message(who.id, 'Can you get a message to ' + entry.name.replace(/^Dr\.\s+/, '') + ' for me? ');
      });
      box.appendChild(b);
    });
    if (!(entry.reach || []).length) box.appendChild(el('span', 'cx-saved', 'Nobody passes messages to them. They find you.'));
    return box;
  }

  /* --- open, close ---------------------------------------------------------- */

  function open(focus) {
    if (!root) return;
    root.hidden = false;
    document.documentElement.dataset.codex = 'open';
    if (opts.toggled) opts.toggled(true);
    load().then(function () {
      if (focus) { tab = focus.tab || tab; picked[tab] = focus.name; render(); }
    });
    setTimeout(function () { $('.cx-search').focus(); }, 60);
  }

  function close() {
    if (!root || root.hidden) return;
    root.hidden = true;
    delete document.documentElement.dataset.codex;
    if (opts.toggled) opts.toggled(false);
  }

  function init(options) {
    opts = options;
    root = options.root;
    root.querySelectorAll('.cx-tab').forEach(function (b) {
      b.addEventListener('click', function () { tab = b.dataset.tab; render(); });
    });
    $('.cx-search').addEventListener('input', function (e) { query = e.target.value.trim().toLowerCase(); render(); });
    $('.cx-close').addEventListener('click', close);
    var toMap = $('.cx-to-map');
    if (toMap) toMap.addEventListener('click', function () { close(); opts.showMap(); });
    root.addEventListener('keydown', function (e) { if (e.key === 'Escape') { e.stopPropagation(); close(); } });
  }

  window.Codex = { init: init, open: open, close: close, isOpen: function () { return !!root && !root.hidden; } };
})();
