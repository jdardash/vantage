// Object dossier for the WorldView globe: one click on a satellite answers whose
// it is, what it is and how big, and owner / type / launch year / RCS filters
// hide every satellite that does not match.
//
// Data: GET /api/satcat/{id}, /api/satcat/facets and /api/satcat/ids
// (src/sda/routes/satcat.py), a daily cache of the COMSPOC Spacebook SATCAT and
// SATCAT details. Every card names its source and age and carries the attribution
// "COMSPOC Spacebook; Space-Track; GCAT (J. McDowell)".
//
// Field set of the card after PremaanshVyas/satlas apps/web/src/lib/satcat.ts
// (MIT, commit 410f923): owner, type, status from the decay date, launch date and
// site, international designator. The plain-words summary line is an idea from
// keeptrack.space sat-info-box-object/object-summary.ts (AGPL-3.0, commit
// db3b64e); no code is taken from it. The rest is ours.
//
// Hiding dots reuses the mechanism of categories.js: the module writes
// record.hidden and asks the page to apply it through ctx.refreshSatVisibility().
// The two filters share the flag through record.hideMask: categories.js owns bit
// 1, this module bit 2, and a record is hidden while any bit is set, so clearing
// one filter never shows what the other still hides. The selected satellite
// always stays visible.
//
// noradKey(), cardRows(), buildQuery() and the formatters are pure (no DOM), so
// they run under Node in tests/test_globe_dossier.py. Text goes in through
// textContent only.

(function (global) {
  'use strict';

  var HIDE_BIT = 2;
  var DETAILS_RETRY_MS = 5000;
  var DETAILS_RETRIES = 6;
  var FACETS_RETRY_MS = 15000;
  var FACETS_RETRIES = 8;
  var ATTRIBUTION = 'COMSPOC Spacebook; Space-Track; GCAT (J. McDowell)';
  var STATIC_NOTE = 'The object dossier needs the live server: SATCAT data is not ' +
    'part of the static demo.';
  var IDLE_NOTE = 'Select a satellite to see whose it is, what it is and how big.';

  var TYPES = [
    { code: 'PAYLOAD', label: 'Payload' },
    { code: 'ROCKET BODY', label: 'Rocket body' },
    { code: 'DEBRIS', label: 'Debris' },
    { code: 'UNKNOWN', label: 'Unknown type' }
  ];
  var RCS = [
    { code: 'SMALL', label: 'Small (under 0.1 m2)' },
    { code: 'MEDIUM', label: 'Medium (0.1 to 1 m2)' },
    { code: 'LARGE', label: 'Large (over 1 m2)' },
    { code: 'UNKNOWN', label: 'Not published' }
  ];

  // ---------------------------------------------------------------------
  // Pure helpers
  // ---------------------------------------------------------------------

  var ALPHA5_DIGITS = '0123456789ABCDEFGHJKLMNPQRSTUVWXYZ';
  var ALPHA5_ID = /^([A-HJ-NP-Z])(\d{4})$/;

  // One key per object whatever the encoding: 25544, '25544', '025544' and the
  // alpha-5 'A0001' (100001) all become a plain decimal string. '' for junk.
  function noradKey(v) {
    if (typeof v === 'number') {
      return (isFinite(v) && v > 0 && Math.floor(v) === v) ? String(v) : '';
    }
    if (typeof v !== 'string') return '';
    var s = v.trim().toUpperCase();
    if (/^\d{1,6}$/.test(s)) {
      var n = parseInt(s, 10);
      return n > 0 ? String(n) : '';
    }
    var m = ALPHA5_ID.exec(s);
    if (!m) return '';
    return String(ALPHA5_DIGITS.indexOf(m[1]) * 10000 + parseInt(m[2], 10));
  }

  // Thousands separators without toLocaleString, whose output follows the locale.
  function fmtInt(n) {
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function num(v) {
    return (typeof v === 'number' && isFinite(v)) ? v : null;
  }

  function str(v) {
    if (typeof v === 'string') return v;
    if (typeof v === 'number' && isFinite(v)) return String(v);
    return '';
  }

  function fmtMass(kg) {
    var v = num(kg);
    if (v === null || v <= 0) return '';
    if (v >= 1000) return fmtInt(Math.round(v)) + ' kg';
    if (v >= 10) return String(Math.round(v * 10) / 10) + ' kg';
    return String(Math.round(v * 100) / 100) + ' kg';
  }

  function fmtSpan(m) {
    var v = num(m);
    if (v === null || v <= 0) return '';
    if (v >= 10) return String(Math.round(v * 10) / 10) + ' m';
    return String(Math.round(v * 100) / 100) + ' m';
  }

  function fmtAge(hours) {
    var h = num(hours);
    if (h === null) return '';
    if (h < 1) return Math.max(1, Math.round(h * 60)) + ' min old';
    if (h < 48) return (Math.round(h * 10) / 10) + ' h old';
    return Math.round(h / 24) + ' days old';
  }

  function sourceName(code) {
    if (code === 'spacebook') return 'COMSPOC Spacebook';
    if (code === 'space-track') return 'Space-Track';
    return str(code);
  }

  // Who measured mass and span: COMSPOC's adopted values, else GCAT's.
  function sizeSource(code) {
    var c = str(code);
    if (c === 'COMSPOC') return 'COMSPOC adopted';
    if (c === 'GCAT') return 'GCAT (J. McDowell)';
    if (c === 'COMSPOC and GCAT') return 'COMSPOC adopted and GCAT';
    return c;
  }

  // [label, value] pairs for the card, in display order, skipping what is empty.
  function cardRows(card) {
    var rows = [];
    if (!card || typeof card !== 'object') return rows;
    function add(label, value) { if (value) rows.push([label, value]); }
    add('NORAD ID', str(card.norad_id));
    add('INTL DESIGNATOR', str(card.intl_designator));
    var owner = str(card.owner);
    var ownerCode = str(card.owner_code);
    add('OWNER', ownerCode && owner && owner !== ownerCode ? owner + ' (' + ownerCode + ')'
      : owner || ownerCode);
    add('TYPE', str(card.object_type_label) || str(card.object_type));
    add('STATUS', str(card.status));
    add('LAUNCHED', str(card.launch_date));
    var site = str(card.launch_site);
    var siteCode = str(card.launch_site_code);
    add('LAUNCH SITE', siteCode && site && site !== siteCode ? site + ' (' + siteCode + ')'
      : site || siteCode);
    add('DECAYED', str(card.decay_date));
    add('RCS SIZE', str(card.rcs_label) || str(card.rcs_size));
    var d = card.details;
    if (d && typeof d === 'object') {
      var mass = fmtMass(d.mass_kg);
      var span = fmtSpan(d.span_m);
      add('MASS', mass);
      add('SPAN', span);
      if (mass || span) add('SIZE FROM', sizeSource(d.size_source));
      add('MANUFACTURER', str(d.manufacturer));
      add('BUS', str(d.bus));
      add('OPERATOR', str(d.operator));
      var parent = str(d.parent);
      var pname = str(d.parent_name);
      add('PARENT', parent && pname ? pname + ' (' + parent + ')' : parent);
    }
    return rows;
  }

  // Query string for /api/satcat/ids, or '' when no filter is set.
  function buildQuery(f) {
    if (!f || typeof f !== 'object') return '';
    var parts = [];
    function put(key, value) {
      if (value === '' || value == null) return;
      parts.push(key + '=' + encodeURIComponent(String(value)));
    }
    put('owner', str(f.owner).trim());
    put('type', str(f.type).trim());
    put('rcs', str(f.rcs).trim());
    var from = num(f.yearFrom);
    var to = num(f.yearTo);
    if (from !== null) put('year_from', Math.round(from));
    if (to !== null) put('year_to', Math.round(to));
    return parts.join('&');
  }

  function describeFilters(f, owners) {
    var bits = [];
    if (f.owner) {
      var name = owners && owners[f.owner] ? owners[f.owner] : f.owner;
      bits.push('owner ' + name);
    }
    for (var i = 0; i < TYPES.length; i++) {
      if (TYPES[i].code === f.type) bits.push(TYPES[i].label.toLowerCase());
    }
    for (var j = 0; j < RCS.length; j++) {
      if (RCS[j].code === f.rcs) bits.push('RCS ' + RCS[j].label.toLowerCase());
    }
    if (f.yearFrom !== null && f.yearTo !== null) {
      bits.push('launched ' + f.yearFrom + ' to ' + f.yearTo);
    } else if (f.yearFrom !== null) {
      bits.push('launched from ' + f.yearFrom);
    } else if (f.yearTo !== null) {
      bits.push('launched up to ' + f.yearTo);
    }
    return bits.join(', ');
  }

  // ---------------------------------------------------------------------
  // DOM helpers
  // ---------------------------------------------------------------------

  var STYLE_ID = 'dossier-style';
  var STYLE_CSS = [
    '.dos-title { font-size: 11px; font-weight: 700; letter-spacing: 1px;',
    '  color: var(--hud-color, #ffb000); overflow-wrap: anywhere; }',
    '.dos-summary { margin: 3px 0 6px; font-size: 10px; opacity: 0.85; }',
    '.dos-note, .dos-status, .dos-source, .dos-fnote { margin-top: 5px; font-size: 9px;',
    '  letter-spacing: 0.5px; color: var(--hud-dim, #7a5500); overflow-wrap: anywhere; }',
    '.dos-note:empty, .dos-status:empty, .dos-source:empty, .dos-fnote:empty',
    '  { display: none; }',
    '.dos-status.dos-error { color: #ff6060; }',
    '.dos-card .info-value { text-align: right; max-width: 62%; overflow-wrap: anywhere; }',
    '.dos-filters { margin-top: 8px; border-top: 1px solid rgba(255,176,0,0.08);',
    '  padding-top: 6px; }',
    '.dos-field { display: flex; align-items: center; justify-content: space-between;',
    '  gap: 6px; margin: 3px 0; font-size: 9px; letter-spacing: 1px;',
    '  color: var(--hud-dim, #7a5500); }',
    '.dos-field select, .dos-field input { flex: 1; min-width: 0; max-width: 62%;',
    '  background: rgba(0,0,0,0.6); color: var(--hud-color, #ffb000);',
    '  border: 1px solid rgba(255,176,0,0.2); font: inherit; font-size: 10px;',
    '  padding: 2px 3px; }',
    '.dos-years input { max-width: 30%; }',
    '.dos-buttons { display: grid; grid-template-columns: 1fr 1fr; gap: 4px;',
    '  margin-top: 5px; }'
  ].join('\n');

  function injectStyle() {
    var doc = global.document;
    if (!doc || !doc.head || doc.getElementById(STYLE_ID)) return;
    var style = doc.createElement('style');
    style.id = STYLE_ID;
    style.textContent = STYLE_CSS;
    doc.head.appendChild(style);
  }

  function el(tag, className, text) {
    var node = global.document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function option(value, label) {
    var o = el('option', null, label);
    o.value = value;
    return o;
  }

  function infoRow(label, value) {
    var row = el('div', 'info-row');
    row.appendChild(el('span', 'info-label', label));
    row.appendChild(el('span', 'info-value', value));
    return row;
  }

  // ---------------------------------------------------------------------
  // Panel
  // ---------------------------------------------------------------------

  function createPanel(ctx) {
    var ui = {
      ctx: ctx,
      api: String(ctx.api || ''),
      isStatic: !!ctx.isStatic,
      currentKey: '',        // the object the card shows or is loading
      failed: false,         // the last card request for currentKey failed
      seq: 0,                // drops replies to an older card request
      retries: 0,            // details refetches for the current card
      facets: null,
      facetTries: 0,
      ownerNames: {},
      filter: null,          // { query, f, keys: {key: true}, matched }
      forced: null,          // record shown only because it is selected
      timer: null
    };

    ui.root = el('div', 'panel');
    ui.root.appendChild(el('h3', null, 'Object Dossier'));
    var body = el('div', 'panel-body');
    ui.root.appendChild(body);

    ui.card = el('div', 'dos-card');
    ui.title = el('div', 'dos-title');
    ui.summary = el('div', 'dos-summary');
    ui.rows = el('div', 'dos-rows');
    ui.notes = el('div', 'dos-note');
    ui.status = el('div', 'dos-status', ui.isStatic ? STATIC_NOTE : IDLE_NOTE);
    ui.source = el('div', 'dos-source');
    [ui.title, ui.summary, ui.rows, ui.notes, ui.status, ui.source].forEach(function (n) {
      ui.card.appendChild(n);
    });
    body.appendChild(ui.card);

    // Filters
    var box = el('div', 'dos-filters');
    function field(label, control, extraClass) {
      var row = el('label', 'dos-field' + (extraClass ? ' ' + extraClass : ''));
      row.appendChild(el('span', null, label));
      if (control) row.appendChild(control);
      box.appendChild(row);
      return row;
    }
    ui.owner = el('select', 'dos-owner');
    ui.owner.appendChild(option('', 'All owners'));
    ui.type = el('select', 'dos-type');
    ui.type.appendChild(option('', 'All types'));
    TYPES.forEach(function (t) { ui.type.appendChild(option(t.code, t.label)); });
    ui.rcs = el('select', 'dos-rcs');
    ui.rcs.appendChild(option('', 'All sizes'));
    RCS.forEach(function (r) { ui.rcs.appendChild(option(r.code, r.label)); });
    ui.yearFrom = el('input', 'dos-year-from');
    ui.yearTo = el('input', 'dos-year-to');
    [ui.yearFrom, ui.yearTo].forEach(function (inp) {
      inp.setAttribute('type', 'number');
      inp.setAttribute('min', '1957');
      inp.setAttribute('max', '2100');
      inp.setAttribute('step', '1');
    });
    ui.yearFrom.setAttribute('placeholder', 'from');
    ui.yearTo.setAttribute('placeholder', 'to');
    field('OWNER', ui.owner);
    field('TYPE', ui.type);
    field('RCS', ui.rcs);
    var years = field('LAUNCHED', ui.yearFrom, 'dos-years');
    years.appendChild(ui.yearTo);
    var buttons = el('div', 'dos-buttons');
    ui.applyBtn = el('button', 'mode-btn dos-apply', 'APPLY FILTER');
    ui.clearBtn = el('button', 'mode-btn dos-clear', 'SHOW ALL');
    ui.applyBtn.setAttribute('type', 'button');
    ui.clearBtn.setAttribute('type', 'button');
    buttons.appendChild(ui.applyBtn);
    buttons.appendChild(ui.clearBtn);
    box.appendChild(buttons);
    ui.fnote = el('div', 'dos-fnote');
    box.appendChild(ui.fnote);
    body.appendChild(box);

    if (ui.isStatic) {
      [ui.owner, ui.type, ui.rcs, ui.yearFrom, ui.yearTo, ui.applyBtn, ui.clearBtn]
        .forEach(function (c) { c.disabled = true; });
      ui.fnote.textContent = 'Filters need the live server.';
    }

    // --- page access, each call guarded: the globe must keep working ---------

    ui.log = function (text) {
      try { ui.ctx.addIntelItem(text); } catch (err) { /* keep going */ }
    };
    ui.refresh = function () {
      try { ui.ctx.refreshSatVisibility(); } catch (err) { /* flags are written */ }
    };
    ui.catalog = function () {
      try {
        var sats = ui.ctx.satEngine.sats;
        return (sats && typeof sats.length === 'number') ? sats : [];
      } catch (err) {
        return [];
      }
    };
    ui.lookup = function (id) {
      try {
        var rec = ui.ctx.satEngine.byNoradId[id];
        return (rec && typeof rec === 'object') ? rec : null;
      } catch (err) {
        return null;
      }
    };
    ui.selectedId = function () {
      try {
        var id = ui.ctx.satEngine.selectedId;
        return id == null ? null : id;
      } catch (err) {
        return null;
      }
    };
    ui.later = function (fn, ms) {
      try {
        if (typeof global.setTimeout === 'function') return global.setTimeout(fn, ms);
      } catch (err) { /* no timers, no retries */ }
      return null;
    };
    ui.get = function (path) {
      if (ui.isStatic || typeof global.fetch !== 'function') {
        return Promise.reject(new Error('no server'));
      }
      return global.fetch(ui.api + path).then(function (r) {
        if (!r || !r.ok) {
          var e = new Error('HTTP ' + (r ? r.status : '?'));
          e.status = r ? r.status : 0;
          throw e;
        }
        return r.json();
      });
    };

    // --- card ------------------------------------------------------------

    ui.say = function (text, isError) {
      ui.status.className = isError ? 'dos-status dos-error' : 'dos-status';
      ui.status.textContent = text || '';
    };

    ui.blank = function () {
      ui.title.textContent = '';
      ui.summary.textContent = '';
      ui.rows.textContent = '';
      ui.notes.textContent = '';
      ui.source.textContent = '';
    };

    ui.render = function (card) {
      ui.blank();
      ui.title.textContent = str(card.name) || ('NORAD ' + str(card.norad_id));
      ui.summary.textContent = str(card.summary);
      cardRows(card).forEach(function (pair) {
        ui.rows.appendChild(infoRow(pair[0], pair[1]));
      });
      var notes = Array.isArray(card.notes) ? card.notes.map(str).filter(Boolean) : [];
      ui.notes.textContent = notes.join(' ');
      var ds = str(card.details_status);
      if (!card.details || typeof card.details !== 'object') {
        if (ds === 'loading') ui.say('Loading mass, span, manufacturer and bus...');
        else if (ds === 'unavailable') ui.say('Physical details are not available.');
        else ui.say('');
      } else {
        ui.say(ds === 'stale' ? 'Physical details are from an older copy.' : '');
      }
      var src = [sourceName(card.source), fmtAge(card.age_hours)].filter(Boolean).join(', ');
      if (card.stale === true) src += ' (refresh failed, older copy)';
      ui.source.textContent = 'Source: ' + (src || 'unknown') + '. ' +
        (str(card.attribution) || ATTRIBUTION) + '.';
    };

    ui.fetchCard = function (key, seq) {
      ui.get('/api/satcat/' + encodeURIComponent(key)).then(function (card) {
        if (seq !== ui.seq) return;
        if (!card || typeof card !== 'object') throw new Error('bad reply');
        ui.failed = false;
        ui.render(card);
        if (str(card.details_status) === 'loading' && ui.retries < DETAILS_RETRIES) {
          ui.retries++;
          ui.timer = ui.later(function () {
            if (seq === ui.seq) ui.fetchCard(key, seq);
          }, DETAILS_RETRY_MS);
        }
      }).catch(function (err) {
        if (seq !== ui.seq) return;
        ui.failed = true;  // picking the same object again tries again
        ui.blank();
        ui.title.textContent = 'NORAD ' + key;
        var status = err && err.status;
        if (status === 404) {
          ui.say('Not in the SATCAT. Spacebook lists the objects Space-Track has catalogued.');
        } else if (status === 503) {
          ui.say('The server has not loaded the SATCAT yet. Try again in a minute.', true);
        } else {
          ui.say('Dossier unavailable (' + ((err && err.message) || 'error') + ').', true);
        }
        ui.source.textContent = ATTRIBUTION + '.';
      });
    };

    // Fill the card for one object. Showing the object already shown is a no-op,
    // so the page may call this and fire sda:satellite-selected for the same pick.
    ui.show = function (noradId, force) {
      var key = noradKey(noradId);
      if (!key) return false;
      if (ui.isStatic) {
        ui.currentKey = key;
        ui.blank();
        ui.say(STATIC_NOTE);
        return true;
      }
      if (key === ui.currentKey && !force && !ui.failed) return true;
      ui.currentKey = key;
      ui.failed = false;
      ui.seq++;
      ui.retries = 0;
      ui.blank();
      ui.title.textContent = 'NORAD ' + key;
      ui.say('Loading dossier...');
      ui.fetchCard(key, ui.seq);
      if (!ui.facets) ui.loadFacets();
      return true;
    };

    ui.reset = function () {
      ui.currentKey = '';
      ui.seq++;
      ui.blank();
      ui.say(ui.isStatic ? STATIC_NOTE : IDLE_NOTE);
    };

    // --- filters ---------------------------------------------------------

    ui.fillFacets = function (f) {
      ui.facets = f;
      var owners = Array.isArray(f.owners) ? f.owners : [];
      var keep = ui.owner.value;
      ui.owner.textContent = '';
      ui.owner.appendChild(option('', 'All owners'));
      ui.ownerNames = {};
      owners.forEach(function (o) {
        if (!o || typeof o !== 'object') return;
        var code = str(o.code);
        if (!code || !/^[A-Z0-9\/-]{1,16}$/.test(code)) return;
        var name = str(o.name) || code;
        ui.ownerNames[code] = name;
        var count = num(o.count);
        var label = (name === code ? code : name + ' (' + code + ')') +
          (count !== null ? ' ' + fmtInt(count) : '');
        ui.owner.appendChild(option(code, label));
      });
      ui.owner.value = keep;
      var lo = num(f.year_min);
      var hi = num(f.year_max);
      if (lo !== null) { ui.yearFrom.setAttribute('min', String(lo)); }
      if (hi !== null) { ui.yearTo.setAttribute('max', String(hi)); }
      if (!ui.filter) {
        var total = num(f.total);
        ui.fnote.textContent = total !== null
          ? fmtInt(total) + ' objects on orbit in the SATCAT.' : '';
      }
    };

    ui.loadFacets = function () {
      if (ui.isStatic || ui.facetsLoading) return;
      ui.facetsLoading = true;
      ui.get('/api/satcat/facets').then(function (f) {
        ui.facetsLoading = false;
        if (f && typeof f === 'object') ui.fillFacets(f);
      }).catch(function () {
        ui.facetsLoading = false;
        if (ui.facetTries < FACETS_RETRIES) {
          ui.facetTries++;
          ui.later(function () { if (!ui.facets) ui.loadFacets(); }, FACETS_RETRY_MS);
        }
        if (!ui.filter) ui.fnote.textContent = 'SATCAT not loaded on the server yet.';
      });
    };

    ui.readFilters = function () {
      function year(inp) {
        var t = str(inp.value).trim();
        if (!/^\d{4}$/.test(t)) return null;
        return parseInt(t, 10);
      }
      var f = {
        owner: str(ui.owner.value), type: str(ui.type.value), rcs: str(ui.rcs.value),
        yearFrom: year(ui.yearFrom), yearTo: year(ui.yearTo)
      };
      if (f.yearFrom !== null && f.yearTo !== null && f.yearFrom > f.yearTo) {
        var t = f.yearFrom; f.yearFrom = f.yearTo; f.yearTo = t;
      }
      return f;
    };

    // Set or clear this module's bit on one record and recompute its flag.
    function mark(rec, out) {
      var m = rec.hideMask | 0;
      m = out ? (m | HIDE_BIT) : (m & ~HIDE_BIT);
      rec.hideMask = m;
      rec.hidden = m !== 0;
    }

    // Write the bit on every record from the matching id set (null = no filter).
    ui.applyFlags = function () {
      var sats = ui.catalog();
      var keys = ui.filter ? ui.filter.keys : null;
      var shown = 0;
      var total = 0;
      for (var i = 0; i < sats.length; i++) {
        var rec = sats[i];
        if (!rec || typeof rec !== 'object') continue;
        total++;
        var out = keys ? !keys[noradKey(rec.noradId)] : false;
        mark(rec, out);
        if (!out) shown++;
      }
      ui.forced = null;
      var sel = ui.selectedId();
      ui.force(sel === null ? null : ui.lookup(sel));
      return { shown: shown, total: total };
    };

    // Keep the selected record visible and remember it, so it goes back under
    // the filters when the selection moves. Taken on when this module's bit is
    // set even if the record is visible right now: categories.js may have shown
    // it first, and it only puts back what its own bit hid. Forced even with no
    // dossier filter, because categories.js may still hide it. True on a change.
    ui.force = function (rec) {
      if (!rec || typeof rec !== 'object') return false;
      if (rec.hidden !== true && !((rec.hideMask | 0) & HIDE_BIT)) return false;
      ui.forced = rec;
      if (rec.hidden !== true) return false;
      rec.hidden = false;
      return true;
    };

    // Put the forced record back under every filter's bits. True on a change.
    ui.release = function () {
      var rec = ui.forced;
      ui.forced = null;
      if (!rec) return false;
      var before = rec.hidden;
      rec.hidden = (rec.hideMask | 0) !== 0;
      return rec.hidden !== before;
    };

    ui.paintNote = function (counts) {
      if (!ui.filter) {
        ui.fnote.textContent = 'No dossier filter: showing all ' + fmtInt(counts.total) +
          ' satellites.';
        return;
      }
      var text = 'Showing ' + fmtInt(counts.shown) + ' of ' + fmtInt(counts.total) +
        ' satellites (' + describeFilters(ui.filter.f, ui.ownerNames) + '; ' +
        fmtInt(ui.filter.matched) + ' match in the SATCAT).';
      if (ui.forced) text += ' Selected stays visible.';
      ui.fnote.textContent = text;
    };

    ui.apply = function () {
      if (ui.isStatic) return Promise.resolve(false);
      var f = ui.readFilters();
      var query = buildQuery(f);
      if (!query) { ui.clear(); return Promise.resolve(true); }
      ui.fnote.textContent = 'Filtering...';
      var token = ++ui.applySeq;
      return ui.get('/api/satcat/ids?' + query).then(function (reply) {
        if (token !== ui.applySeq) return false;
        var ids = reply && Array.isArray(reply.ids) ? reply.ids : null;
        if (!ids) throw new Error('bad reply');
        var keys = {};
        for (var i = 0; i < ids.length; i++) {
          var k = noradKey(ids[i]);
          if (k) keys[k] = true;
        }
        ui.filter = { query: query, f: f, keys: keys, matched: ids.length };
        var counts = ui.applyFlags();
        ui.refresh();
        ui.paintNote(counts);
        ui.log('Dossier filter: ' + describeFilters(f, ui.ownerNames) + ' (' +
          fmtInt(counts.shown) + ' of ' + fmtInt(counts.total) + ' shown)');
        return true;
      }).catch(function (err) {
        if (token !== ui.applySeq) return false;
        var status = err && err.status;
        ui.fnote.textContent = status === 503
          ? 'The server has not loaded the SATCAT yet; filter not applied.'
          : 'Filter failed (' + ((err && err.message) || 'error') + '); nothing hidden.';
        return false;
      });
    };
    ui.applySeq = 0;

    ui.clear = function () {
      if (ui.isStatic) return;
      ui.applySeq++;
      var had = !!ui.filter;
      ui.filter = null;
      var counts = ui.applyFlags();
      ui.refresh();
      ui.paintNote(counts);
      if (had) ui.log('Dossier filter cleared, showing all ' + fmtInt(counts.total) +
        ' satellites');
    };

    // The page replaced the record array: put the filter on the new records.
    ui.onLoaded = function () {
      ui.forced = null;
      if (!ui.filter) return;
      var counts = ui.applyFlags();
      ui.refresh();
      ui.paintNote(counts);
    };

    // The selected satellite is always shown; the one shown only because it was
    // selected goes back under the filter when the selection moves or clears.
    ui.onSelected = function (e) {
      var d = e && e.detail;
      if (!d || d.noradId == null) return;
      ui.show(d.noradId);
      var rec = ui.lookup(d.noradId);
      var changed = false;
      if (ui.forced && ui.forced !== rec) changed = ui.release();
      if (ui.forced !== rec && ui.force(rec)) changed = true;
      if (changed) ui.refresh();
    };

    // A deselect naming another object than the card shows is about an older
    // selection (the events crossed) and is ignored.
    ui.onDeselected = function (e) {
      var d = e && e.detail;
      if (d && d.noradId != null && ui.currentKey && noradKey(d.noradId) !== ui.currentKey) {
        return;
      }
      ui.reset();
      if (ui.release()) ui.refresh();
    };

    ui.applyBtn.addEventListener('click', function () {
      try { ui.apply(); } catch (err) { /* the globe must keep working */ }
    });
    ui.clearBtn.addEventListener('click', function () {
      try {
        ui.owner.value = ''; ui.type.value = ''; ui.rcs.value = '';
        ui.yearFrom.value = ''; ui.yearTo.value = '';
        ui.clear();
      } catch (err) { /* keep going */ }
    });

    return ui;
  }

  var current = null;

  function detach(panel) {
    var doc = global.document;
    if (!panel || !doc || typeof doc.removeEventListener !== 'function') return;
    doc.removeEventListener('sda:catalog-loaded', panel.loadedHandler);
    doc.removeEventListener('sda:satellite-selected', panel.selectedHandler);
    doc.removeEventListener('sda:satellite-deselected', panel.deselectedHandler);
  }

  // Mount the panel inside ctx.mount. True when it is in place, false when the
  // page gave us nothing to mount into or anything threw.
  function init(ctx) {
    try {
      if (!ctx || !ctx.mount || typeof ctx.mount.appendChild !== 'function') return false;
      injectStyle();
      var panel = createPanel(ctx);
      ctx.mount.appendChild(panel.root);
      detach(current);
      panel.loadedHandler = function (e) {
        try { panel.onLoaded(e); } catch (err) { /* keep going */ }
      };
      panel.selectedHandler = function (e) {
        try { panel.onSelected(e); } catch (err) { /* keep going */ }
      };
      panel.deselectedHandler = function (e) {
        try { panel.onDeselected(e); } catch (err) { /* keep going */ }
      };
      var doc = global.document;
      if (doc && typeof doc.addEventListener === 'function') {
        doc.addEventListener('sda:catalog-loaded', panel.loadedHandler);
        doc.addEventListener('sda:satellite-selected', panel.selectedHandler);
        doc.addEventListener('sda:satellite-deselected', panel.deselectedHandler);
      }
      current = panel;
      if (!panel.isStatic) panel.loadFacets();
      // A satellite selected before the module started (a shared link) fills now.
      var sel = panel.selectedId();
      if (sel !== null) panel.show(sel);
      return true;
    } catch (err) {
      return false;
    }
  }

  function show(noradId) {
    try {
      return current ? current.show(noradId) : false;
    } catch (err) {
      return false;
    }
  }

  global.SDADossier = {
    init: init,
    show: show,
    apply: function () { return current ? current.apply() : Promise.resolve(false); },
    clear: function () { if (current) current.clear(); },
    noradKey: noradKey,
    cardRows: cardRows,
    buildQuery: buildQuery,
    fmtMass: fmtMass,
    fmtSpan: fmtSpan,
    fmtAge: fmtAge,
    HIDE_BIT: HIDE_BIT
  };
})(typeof window !== 'undefined' ? window : this);
