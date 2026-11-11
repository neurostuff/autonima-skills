'use strict';
// The review page. Everything from the review (titles, abstracts, article text, judges' reasons)
// is untrusted text: it is only ever placed with textContent, never as HTML.

const S = { token: null, ov: null, data: {}, detail: {}, ui: {}, view: 'overview', keys: {} };
const VIEWS = [['overview', 'Overview'], ['abstract', 'Abstracts'], ['fulltext', 'Full texts'],
               ['selection', 'Analyses'], ['targets', 'Targets'], ['findings', 'Findings']];
const STAGE_NAME = { abstract: 'abstract screening', fulltext: 'full-text screening', selection: 'analysis selection' };
const $ = (sel) => document.querySelector(sel);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  return add(el, ...kids);
}
// Append children the way h() takes them: nested arrays are flattened, and null, false and ''
// are skipped. Strings become text nodes, never HTML.
function set(el, ...kids) {
  el.replaceChildren();
  return add(el, ...kids);
}
function add(el, ...kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false || kid === '') continue;
    el.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const fmt = (n) => Number(n || 0).toLocaleString('en-US');
const plural = (n, word, many) => `${fmt(n)} ${n === 1 ? word : (many || word + 's')}`;

function takeToken() {
  let stored = null;
  try { stored = sessionStorage.getItem('review-token'); } catch (e) { /* storage blocked */ }
  const m = location.hash.match(/^#t=([\w-]+)/);
  if (m) {
    try { sessionStorage.setItem('review-token', m[1]); } catch (e) { /* keep it in memory */ }
    history.replaceState(null, '', location.pathname + '#/overview');
    return m[1];
  }
  return stored;
}

async function api(path, body) {
  const res = await fetch('/api/' + path, {
    method: body ? 'POST' : 'GET',
    headers: Object.assign({ 'X-Review-Token': S.token || '' }, body ? { 'Content-Type': 'application/json' } : {}),
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* not JSON */ }
  if (!res.ok) throw new Error(data.error || `request failed (${res.status})`);
  return data;
}

function banner(text) {
  const b = $('#banner');
  b.hidden = !text;
  b.textContent = text || '';
}

function go(view) {
  if (location.hash === '#/' + view) render(); else location.hash = '#/' + view;
}

// ------------------------------------------------------------------ shared pieces
function chip(decision) {
  const cls = { include: 'include', exclude: 'exclude', uncertain: 'uncertain', incomplete: 'incomplete' }[decision] || '';
  return h('span', { class: 'chip ' + cls }, decision);
}
// A criterion's state, coloured by what it does to the study: an exclusion criterion that is met
// counts against it, like an inclusion criterion that is not.
function effect(id, state) {
  if (state === 'unclear') return 'state-unclear';
  const exclusion = id.replace(/^G/, '')[0] === 'E';
  return (state === 'met') !== exclusion ? 'state-met' : 'state-not_met';
}
// A criterion's chip, tinted by its kind: inclusion criteria (I, GI) and exclusion criteria (E, GE).
function kind(id) {
  const letter = String(id).split(':').pop().replace(/^G/, '')[0];
  return 'crit' + (letter === 'I' ? ' inc' : letter === 'E' ? ' exc' : '');
}
function critChip(id, title) {
  return h('span', { class: kind(id), title: title || null }, id);
}
function critTag(id, state, text) {
  return h('span', { class: kind(id), title: text || '' }, id, state ? h('span', { class: effect(id, state) }, ' ' + state.replace('_', ' ')) : null);
}
function studyLinks(it) {
  const out = [[it.journal, it.year].filter(Boolean).join(', ')];
  if (/^\d+$/.test(it.pmid)) {
    out.push(' · ', h('a', { href: `https://pubmed.ncbi.nlm.nih.gov/${it.pmid}/`, target: '_blank', rel: 'noopener noreferrer' }, 'PMID ' + it.pmid));
  }
  if (it.doi && /^10\.\S+$/.test(it.doi)) {
    out.push(' · ', h('a', { href: 'https://doi.org/' + encodeURI(it.doi), target: '_blank', rel: 'noopener noreferrer' }, 'DOI'));
  }
  return out;
}
function criteriaTable(criteria, texts) {
  return h('table', { class: 'plain' }, Object.entries(criteria).map(([id, state]) =>
    h('tr', {}, h('td', {}, critChip(id)), h('td', {}, texts(id) || ''),
      h('td', { class: effect(id, state) }, state.replace('_', ' ')))));
}

// The verdict bar: agree, or disagree with the criterion the judge misapplied and a note.
function verdictBar({ existing, criteria, texts, deciding, onSave, agreeLabel }) {
  const ids = Object.keys(criteria);
  const state = { form: false, picked: new Set(), note: '', busy: false, all: false };
  const box = h('div', { class: 'inner' });
  const save = async (verdict) => {
    if (state.busy) return;
    if (verdict === 'disagree' && !state.picked.size && !state.note.trim()) {
      banner('Pick the criterion the judge got wrong, or write a note.');
      return;
    }
    state.busy = true;
    banner('');
    try { await onSave(verdict, verdict === 'disagree' ? [...state.picked] : [], verdict === 'disagree' ? state.note : ''); }
    catch (e) { banner(e.message); state.busy = false; }
  };
  const open = () => {
    state.form = true;
    state.picked = new Set(existing && existing.verdict === 'disagree' ? existing.criteria : (deciding.length === 1 ? deciding : []));
    state.note = (existing && existing.verdict === 'disagree' && existing.note) || '';
    state.all = !deciding.length || [...state.picked].some((id) => !deciding.includes(id));
    draw();
  };
  function draw() {
    const keys = { a: () => save('agree'), d: open };
    for (const k of ['Enter', 'Escape', '1', '2', '3', '4', '5', '6', '7', '8', '9']) delete S.keys[k];
    delete S.keys.Submit;
    if (!state.form) {
      const said = existing
        ? h('span', { class: existing.verdict === 'agree' ? 'state-met' : 'state-not_met' },
            existing.verdict === 'agree' ? 'You agreed.' : `You disagreed${existing.criteria.length ? ' (' + existing.criteria.join(', ') + ')' : ''}${existing.note ? ': ' + existing.note : '.'}`)
        : h('span', { class: 'muted' }, 'Was the judge right?');
      set(box, h('div', { class: 'row' }, said, h('span', { class: 'spacer' }),
        h('button', { class: 'btn good', onclick: () => save('agree') }, agreeLabel || 'Agree', h('kbd', {}, 'A')),
        h('button', { class: 'btn bad', onclick: open }, 'Disagree', h('kbd', {}, 'D'))));
    } else {
      const note = h('textarea', { placeholder: 'What should it have decided, and why? (optional if you picked a criterion)' });
      note.value = state.note;
      note.addEventListener('input', () => { state.note = note.value; });
      // The criteria that decided come first; the others are one more step away.
      const shown = state.all ? deciding.concat(ids.filter((id) => !deciding.includes(id))) : deciding;
      const picks = shown.map((id, i) => {
        const b = h('button', { class: 'pick', 'aria-pressed': String(state.picked.has(id)) },
          i < 9 ? h('kbd', {}, String(i + 1)) : null, ' ', critChip(id),
          h('span', { class: effect(id, criteria[id]) }, ' ' + criteria[id].replace('_', ' ')), ' ', texts(id) || '');
        const toggle = () => { state.picked.has(id) ? state.picked.delete(id) : state.picked.add(id); b.setAttribute('aria-pressed', String(state.picked.has(id))); };
        b.addEventListener('click', toggle);
        if (i < 9) keys[String(i + 1)] = toggle;
        return b;
      });
      const cancel = () => { state.form = false; draw(); };
      keys.Enter = keys.Submit = () => save('disagree');
      keys.Escape = cancel;
      delete keys.d;
      set(box, 
        h('div', { class: 'small muted' }, 'Which criterion did the judge get wrong?'),
        h('div', { class: 'picks' }, picks,
          state.all || shown.length === ids.length ? null : h('button', { class: 'btn link small', onclick: () => { state.all = true; draw(); } }, 'Another criterion…')), note,
        h('div', { class: 'row' },
          h('button', { class: 'btn bad', onclick: () => save('disagree') }, 'Save disagreement', h('kbd', {}, 'Enter')),
          h('button', { class: 'btn', onclick: cancel }, 'Cancel', h('kbd', {}, 'Esc'))));
    }
    Object.assign(S.keys, keys);
  }
  draw();
  return h('div', { class: 'verdict' }, box);
}

// ------------------------------------------------------------------ header
function renderTop() {
  const ov = S.ov;
  const mode = ov.pilot ? `Pilot: ${ov.judged.abstract} of ${ov.records} abstracts judged` : 'Complete run';
  set($('#top'), 
    h('div', { class: 'top-row' },
      h('h1', { title: ov.objective || '' }, ov.name), h('span', { class: 'chip mode' }, mode),
      h('span', { class: 'tiny', title: 'Judged by ' + Object.entries(ov.judges).filter(([, v]) => v.length).map(([s, v]) => `${s}: ${v.map((p) => p[0]).join(', ')}`).join('; ') },
        `protocol v${ov.protocol.version}` + (ov.protocol.amendments ? `, ${plural(ov.protocol.amendments, 'amendment')}` : '')),
      h('span', { class: 'spacer' }),
      h('button', { class: 'btn', title: 'Read the decisions again, after the judges have run', onclick: reload }, 'Reload')),
    h('nav', {}, VIEWS.map(([id, label]) => h('button', { 'aria-current': S.view === id ? 'page' : null, onclick: () => go(id) }, label))));
}

async function reload() {
  try {
    await api('reload', {});
    S.data = {}; S.detail = {};
    S.ov = await api('overview');
    S.ovStale = false;
    banner('');
    render();
  } catch (e) { banner(e.message); }
}

// ------------------------------------------------------------------ overview
// One screen, in the order of the pipeline: what each stage kept, what decided it, what each
// target collected. Every block opens the decisions behind it.
function viewOverview(main) {
  const ov = S.ov, st = ov.status;
  const a = st.abstract_screening, r = st.fulltext_retrieval, f = st.fulltext_screening, x = st.extraction;
  const lane = (stage, id) => (ov.lanes[stage].find((l) => l.id === id) || { size: 0 }).size;
  const some = (...xs) => xs.filter(Boolean);
  // Counts are coloured by what happened to the studies: kept, dropped, or undecided.
  const warn = (text) => ({ text: '⚠ ' + text, cls: 'flag' });
  const kept = (text) => ({ text, cls: 'kept' }), dropped = (text) => ({ text, cls: 'dropped' }), unsure = (text) => ({ text, cls: 'unsure' });
  const inNoMap = lane('selection', 'none_selected'), noCoordinates = (x.extracted || 0) - (x.with_analyses || 0);
  const tiles = [
    { label: 'Abstracts', view: 'abstract', n: a.include + a.uncertain, unit: 'passed', cls: 'kept',
      notes: some(a.uncertain && [kept(`${fmt(a.include)} included`), ', ', unsure(`${fmt(a.uncertain)} uncertain`)],
        a.exclude && dropped(`${fmt(a.exclude)} excluded`), a.pending && `${fmt(a.pending)} not judged yet`) },
    { label: 'Full texts', view: 'fulltext', n: f.include, unit: 'included', cls: 'kept',
      notes: some(f.exclude && dropped(`${fmt(f.exclude)} excluded`), r.unavailable && `${fmt(r.unavailable)} without text`,
        f.text_incomplete && `${fmt(f.text_incomplete)} incomplete`, f.pending && `${fmt(f.pending)} not judged yet`) },
    { label: 'Analyses', view: 'selection', n: x.analyses, unit: 'analyses',
      notes: some(`from ${plural(x.with_analyses, 'study', 'studies')}`,
        noCoordinates > 0 && warn(`${plural(noCoordinates, 'study', 'studies')} without coordinates`),
        x.pending && `${fmt(x.pending)} not extracted yet`) },
    { label: 'Targets', view: 'targets', n: lane('selection', 'selected'), unit: 'studies in a map', cls: 'kept',
      notes: some(inNoMap && warn(`${plural(inNoMap, 'study', 'studies')} in no map`), plural(ov.targets.length, 'target')) },
  ];
  // Where to go next: in a pilot the first stage with decisions left to read, in a complete run
  // the first stage not looked at yet.
  const progress = (view) => (ov.lanes[view] || []).reduce((p, l) => [p[0] + l.reviewed, p[1] + l.size], [0, 0]);
  const next = tiles.find((t) => { const [done, size] = progress(t.view); return size && (ov.pilot ? done < size : !done); });
  const part = (n) => (n.cls ? h('span', { class: n.cls }, n.text) : n);
  const tile = (t) => {
    const [done, size] = progress(t.view);
    const stale = ov.superseded[t.view];
    return h('button', { class: 'tile' + (t === next ? ' next' : ''), onclick: () => go(t.view) },
      h('span', { class: 'label' }, t.label),
      h('span', { class: 'big num' }, fmt(t.n), h('span', { class: 'unit ' + (t.cls || '') }, ' ' + t.unit)),
      h('span', { class: 'notes' }, t.notes.map((n) => h('span', {}, [n].flat().map(part)))),
      stale ? h('span', { class: 'flag' }, `${fmt(stale)} re-opened, waiting for the judges`) : null,
      size ? [meter(done / size), h('span', { class: 'foot' }, h('span', { class: 'tiny' }, `${fmt(done)} of ${fmt(size)} reviewed`),
        t === next ? h('span', { class: 'go' }, done ? 'Continue ›' : 'Start here ›') : null)] : null);
  };
  const page = h('div', { class: 'page' }, h('div', { class: 'flow' }, tiles.map(tile)));

  // What decided each screening stage. Bars are shares of the stage's exclusions; "alone" counts
  // the studies no other criterion would have excluded.
  const decided = (stage, total) => {
    const crit = ov.criteria[stage].filter((c) => c.excluded).sort((p, q) => q.excluded - p.excluded);
    if (!crit.length || !total) return null;
    return h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', {}, `What decided ${STAGE_NAME[stage]}`),
        h('span', { class: 'tiny' }, `of ${fmt(total)} excluded; a study can fail several`)),
      crit.map((c) => h('button', { class: 'bar-row', title: `${c.id}: ${c.text}`,
        onclick: () => { Object.assign(stageUi(stage), { criterion: c.id, cur: null }); go(stage); } },
        h('span', { class: 'clamp2' }, critChip(c.id), ' ', c.text),
        h('span', { class: 'n' }, fmt(c.excluded)),
        h('span', { class: 'tiny', title: 'Excluded on this criterion alone: the studies that return if it is wrong.' }, c.alone ? `${fmt(c.alone)} alone` : ''),
        bar(c.excluded / total, 90))));
  };
  add(page, h('div', { class: 'two' }, decided('abstract', a.exclude), decided('fulltext', f.exclude)));

  // What decided the targets: the shared criteria once, then what each target collected.
  const tc = ov.target_counts || [], shared = ov.shared_counts || { left_out_by: [], analyses: 0 };
  if (tc.some((t) => t.considered)) {
    const open = (name) => { Object.assign(targetsUi(), { cur: name, row: null }); go('targets'); };
    const sharedCard = shared.left_out_by.length ? h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', {}, 'What decided analysis selection'),
        h('span', { class: 'tiny' }, `of ${fmt(shared.analyses)} analyses; out of every target`)),
      shared.left_out_by.map((c) => h('div', { class: 'bar-row', title: `${c.id}: ${c.text}` },
        h('span', { class: 'clamp2' }, critChip(c.id), ' ', c.text),
        h('span', { class: 'n' }, fmt(c.n)), h('span', {}), bar(c.n / shared.analyses, 90)))) : null;
    const flagged = (n, bad, why) => [fmt(n), bad ? h('span', { class: 'flag', title: why }, ' ⚠') : null];
    add(page, h('div', { class: 'two' }, sharedCard, h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', {}, 'What each target collected'),
        h('span', { class: 'tiny' }, `of ${fmt(shared.analyses)} analyses`)),
      h('div', { class: 'matrix-wrap flat' }, h('table', { class: 'plain' },
        h('tr', {}, h('th', {}, 'Target'), h('th', { class: 'num' }, 'Studies'), h('th', { class: 'num kept' }, 'In'),
          h('th', { class: 'num dropped', title: 'Left out by a criterion every target shares' }, 'Out, shared'),
          h('th', { class: 'num dropped', title: 'Left out by the target\'s own criteria only' }, 'Out, own')),
        tc.map((t) => h('tr', {}, h('td', {}, h('button', { class: 'btn link', onclick: () => open(t.name) }, breakable(t.name))),
          h('td', { class: 'num' }, flagged(t.studies, !ov.pilot && t.studies < 17, 'Few for a coordinate-based meta-analysis: 17 or more is usual.')),
          h('td', { class: 'num kept' }, flagged(t.analyses, !t.analyses, 'No analysis was selected for this target.')),
          h('td', { class: 'num dropped' }, fmt(t.out_shared)), h('td', { class: 'num dropped' }, fmt(t.out_own)))))))));
  }

  if (ov.unavailable.length) {
    add(page, h('details', { class: 'card' },
      h('summary', {}, `${plural(ov.unavailable.length, 'study', 'studies')} passed abstract screening but had no full text`),
      h('p', { class: 'muted small' }, 'Reported as unavailable, not excluded. To add a paper, put it in a local folder and run retrieval again.'),
      h('ul', { class: 'clean small' }, ov.unavailable.map((it) => h('li', {}, it.title || '(no title)', h('div', { class: 'tiny' }, studyLinks(it)))))));
  }
  set(main, page);
}
function bar(fraction, width) {
  const el = h('span', { class: 'bar' });
  el.style.display = 'block';
  el.style.width = Math.max(2, Math.round((width || 180) * Math.min(1, fraction))) + 'px';
  return el;
}
// Inline style attributes are blocked by the page's content security policy, so widths are set
// through the element's style object.
function meter(fraction) {
  const fill = h('span', { class: 'fill' });
  fill.style.width = Math.round(100 * Math.min(1, fraction)) + '%';
  return h('span', { class: 'meter' }, fill);
}

// ------------------------------------------------------------------ stage views
function stageUi(stage) {
  // One object per stage, kept across renders: the filter, the current item, how much of each group is shown.
  const ui = S.ui[stage] || (S.ui[stage] = {});
  for (const [k, v] of Object.entries({ criterion: '', unreviewed: false, caps: {}, cur: null, cell: null })) {
    if (!(k in ui)) ui[k] = v;
  }
  return ui;
}
const reviewed = (stage, it) => (stage === 'selection' ? it.reviewed >= it.n_cells : !!it.verdict);

function groups(stage) {
  const { items, lanes } = S.data[stage], ui = stageUi(stage);
  return lanes.map((lane) => {
    let rows = items.filter((it) => it.lane === lane.id && (!ui.criterion || (it.deciding || []).includes(ui.criterion)));
    if (ui.criterion) rows = rows.filter((it) => it.deciding.length === 1).concat(rows.filter((it) => it.deciding.length > 1));
    const total = rows.length, done = rows.filter((it) => reviewed(stage, it)).length;
    if (ui.unreviewed) rows = rows.filter((it) => !reviewed(stage, it) || it.pmid === ui.cur);
    const cap = ui.caps[lane.id] != null ? ui.caps[lane.id] : (total <= 15 ? total : 10);
    return { lane, total, done, rows, shown: rows.slice(0, cap) };
  });
}

async function viewStage(main, stage) {
  if (!S.data[stage]) S.data[stage] = await api('stage/' + stage);
  const data = S.data[stage], ui = stageUi(stage);
  if (!data.items.length) {
    const stale = S.ov.superseded[stage];
    set(main, h('div', { class: 'empty' }, h('h2', {}, 'No current decisions here yet'),
      h('p', {}, stale ? `${plural(stale, 'earlier decision')} at this stage ${stale === 1 ? 'is' : 'are'} no longer current: the criteria or the texts changed since, for example through an amendment. They are waiting for the judges. Select Reload after the stage has run again.`
        : 'The judges have not run this stage. Select Reload after it has run.')));
    return;
  }
  const gs = groups(stage), flat = gs.flatMap((g) => g.shown);
  if (!flat.some((it) => it.pmid === ui.cur)) {
    ui.cur = (flat.find((it) => !reviewed(stage, it)) || flat[0] || {}).pmid || null;
    ui.cell = null;
  }
  const move = (step) => {
    const i = flat.findIndex((it) => it.pmid === ui.cur);
    const next = flat[Math.max(0, Math.min(flat.length - 1, i + step))];
    if (next) { ui.cur = next.pmid; ui.cell = null; render(); }
  };
  S.advance = () => {
    const i = flat.findIndex((it) => it.pmid === ui.cur);
    const next = flat.slice(i + 1).find((it) => !reviewed(stage, it)) || flat.find((it) => !reviewed(stage, it) && it.pmid !== ui.cur);
    if (next) { ui.cur = next.pmid; ui.cell = null; }
    render();
  };
  Object.assign(S.keys, { j: () => move(1), k: () => move(-1) });

  // sidebar
  const crits = stage === 'selection' ? [] : Object.keys(data.criteria);
  const filters = h('div', { class: 'filters' });
  if (crits.length) {
    const sel = h('select', { 'aria-label': 'Filter by deciding criterion', onchange: (e) => { ui.criterion = e.target.value; ui.cur = null; render(); } },
      h('option', { value: '' }, 'Decided by any criterion'),
      crits.map((c) => h('option', { value: c, selected: ui.criterion === c }, `${c}: ${data.criteria[c].slice(0, 70)}`)));
    add(filters, sel);
  }
  const box = h('input', { type: 'checkbox', id: 'unrev', checked: ui.unreviewed, onchange: (e) => { ui.unreviewed = e.target.checked; render(); } });
  add(filters, h('div', { class: 'row' }, h('label', { for: 'unrev' }, box, ' Hide what I have reviewed'), h('span', { class: 'spacer' }),
    h('span', { class: 'tiny', title: 'Next and previous' }, h('kbd', {}, 'J'), h('kbd', {}, 'K'))));
  const side = h('div', { class: 'side' }, filters);
  for (const g of gs) {
    if (!g.total) continue;
    add(side, h('div', { class: 'lane-head', title: g.lane.hint },
      h('h3', {}, h('span', {}, g.lane.label), h('span', { class: 'tiny num' }, `${fmt(g.done)} of ${fmt(g.total)} reviewed`))));
    add(side, h('ul', {}, g.shown.map((it) => h('li', {}, sideItem(stage, it, ui)))));
    const left = g.rows.length - g.shown.length;
    if (left > 0) {
      add(side, h('button', { class: 'btn more small', onclick: () => { ui.caps[g.lane.id] = g.shown.length + 10; render(); } },
        `Show 10 more of ${fmt(left)}`));
    }
  }
  const pane = h('div', { class: 'pane' });
  set(main, h('div', { class: 'stage' }, side, pane));
  const cur = data.items.find((it) => it.pmid === ui.cur);
  if (!cur) { add(pane, h('div', { class: 'empty' }, 'Nothing matches this filter.')); return; }
  if (stage === 'selection') await paneSelection(pane, cur, ui); else await paneScreening(pane, stage, cur);
  const active = side.querySelector('[aria-current="true"]');
  if (active) active.scrollIntoView({ block: 'nearest' });
}

function sideItem(stage, it, ui) {
  let mark = '', cls = '', meta;
  if (stage === 'selection') {
    if (it.disagree) { mark = '✗'; cls = 'disagree'; } else if (reviewed(stage, it)) { mark = '✓'; cls = 'agree'; }
    meta = plural(it.n_analyses, 'analysis', 'analyses')
      + (it.n_selected ? ` · ${it.n_selected} selected` : (it.out_by.length ? ' · ' + it.out_by.join(' ') : ''))
      + (it.reviewed && !reviewed(stage, it) ? ' · partly reviewed' : '');
  } else {
    if (it.verdict) { mark = it.verdict.verdict === 'agree' ? '✓' : '✗'; cls = it.verdict.verdict; }
    meta = it.decision + (it.deciding.length ? ' · ' + it.deciding.join(', ') : '');
  }
  return h('button', { class: 'item', 'aria-current': String(it.pmid === ui.cur), onclick: () => { ui.cur = it.pmid; ui.cell = null; render(); } },
    h('span', { class: 'v ' + cls, title: cls ? `You ${cls}d` : 'Not reviewed' }, mark),
    h('span', {}, h('span', { class: 't' }, it.title || '(no title)'), h('span', { class: 'm' }, meta)));
}

async function paneScreening(pane, stage, it) {
  const texts = (id) => S.data[stage].criteria[id];
  const read = h('div', { class: 'read' });
  add(read, h('h2', {}, it.title || '(no title)'),
    h('div', { class: 'meta' }, studyLinks(it), it.source ? ` · text from ${it.source}` : ''),
    h('div', { class: 'decision' }, h('span', { class: 'small muted' }, 'The judge decided'), chip(it.decision),
      it.deciding.length ? h('span', { class: 'small muted' }, 'because of') : null,
      it.deciding.map((c) => critTag(c, it.criteria[c], texts(c)))),
    it.deciding.map((c) => h('div', { class: 'small muted' }, critChip(c), ' ', texts(c))),
    h('p', { class: 'reason' }, it.reason),
    it.flags.map((f) => h('p', { class: 'flag' }, '⚠ ' + f)));

  if (stage === 'abstract') {
    add(read, h('div', { class: 'section' }, h('h4', {}, 'Abstract'),
      h('div', { class: 'abstract' }, it.abstract || 'This record has no abstract. It was judged on its title and metadata.')));
  } else {
    const evidence = h('div', { class: 'section' }, h('h4', {}, 'The quotes the judge relied on, in the paper'), h('p', { class: 'muted small' }, 'Loading…'));
    const more = h('div', {});
    add(read, evidence, more);
    const key = 'fulltext:' + it.pmid;
    const fill = (d) => {
      set(evidence, h('h4', {}, 'The quotes the judge relied on, in the paper'),
        d.contexts.length ? d.contexts.map((c, i) => h('div', { class: 'quote' },
          i && d.contexts[i - 1].criterion === c.criterion ? null
            : h('div', { class: 'small' }, critChip(c.criterion), ' ', h('span', { class: 'muted' }, texts(c.criterion) || '')),
          c.found ? h('div', { class: 'ctx' }, (c.cut_before ? '…' : '') + c.before, h('mark', {}, c.match), c.after + (c.cut_after ? '…' : ''))
            : h('div', {}, h('div', { class: 'ctx' }, `“${c.quote}”`), h('div', { class: 'flag' }, '⚠ These words were not found in the paper as quoted.'))))
          : h('p', { class: 'muted small' }, 'The judge quoted nothing for this decision.'));
      set(more, 
        h('details', {}, h('summary', {}, 'Abstract'), h('div', { class: 'abstract' }, d.abstract || 'No abstract.')),
        d.tables.length ? h('details', {}, h('summary', {}, `${plural(d.tables.length, 'table')} in the file`),
          d.tables.map((t) => h('details', {}, h('summary', {}, `${t.label || 'Table'}${t.coordinates ? ' (coordinates)' : ''}: ${t.caption || ''}`),
            h('div', { class: 'grid-wrap' }, h('table', { class: 'grid' }, t.grid.map((row) => h('tr', {}, row.map((c) => h('td', {}, c)))))),
            t.rows > t.grid.length ? h('p', { class: 'tiny' }, `First ${t.grid.length} of ${t.rows} rows.`) : null,
            t.footer ? h('p', { class: 'tiny' }, t.footer) : null)))
          : h('p', { class: 'muted small' }, 'The file has no parsed tables.'),
        fullTextToggle(it.pmid));
    };
    if (S.detail[key]) fill(S.detail[key]);
    else api('detail/fulltext/' + it.pmid).then((d) => { S.detail[key] = d; if (stageUi(stage).cur === it.pmid && S.view === stage) fill(d); }).catch((e) => banner(e.message));
  }
  add(read, h('details', {}, h('summary', {}, `All ${Object.keys(it.criteria).length} criteria, as the judge marked them`), criteriaTable(it.criteria, texts)));
  set(pane, read, verdictBar({
    existing: it.verdict, criteria: it.criteria, texts, deciding: it.deciding,
    onSave: async (verdict, criteria, note) => {
      it.verdict = await api('verdict', { stage, pmid: it.pmid, verdict, criteria, note });
      S.ovStale = true;
      S.advance();
    },
  }));
}

function fullTextToggle(pmid) {
  const holder = h('div', {});
  const btn = h('button', { class: 'btn small', onclick: async () => {
    btn.disabled = true;
    try {
      const d = await api('text/' + pmid);
      set(holder, h('div', { class: 'fulltext' }, d.text), d.truncated ? h('p', { class: 'tiny' }, 'The text is cut here.') : null);
    } catch (e) { banner(e.message); btn.disabled = false; }
  } }, 'Read the full text');
  add(holder, btn);
  return h('div', { class: 'section' }, holder);
}

// ------------------------------------------------------------------ analyses (selection)
const mark = (v) => (v ? h('span', { class: 'v ' + v, title: `You ${v}d` }, v === 'agree' ? '✓' : '✗') : null);
// Long target names break at their underscores.
const breakable = (name) => name.split('_').flatMap((part, i) => (i ? [h('wbr'), '_' + part] : [part]));
const tableLabel = (t) => (!t || t === 'record' || /^tbl/i.test(t) ? '' : (/^\d+$/.test(t) ? 'Table ' + t : t));

async function paneSelection(pane, it, ui) {
  const key = 'selection:' + it.pmid;
  if (!S.detail[key]) {
    set(pane, h('p', { class: 'loading' }, 'Loading…'));
    S.detail[key] = await api('detail/selection/' + it.pmid);
    if (stageUi('selection').cur !== it.pmid || S.view !== 'selection') return;
  }
  const d = S.detail[key], targets = S.data.selection.targets, all = S.data.selection.criteria;
  const texts = (target) => (id) => (id.startsWith('G') ? all[id] : all[`${target}:${id}`]);
  const changed = () => {
    const cells = Object.values(d.cells).flatMap((row) => Object.values(row));
    it.reviewed = cells.filter((c) => c.verdict).length;
    it.disagree = cells.filter((c) => c.verdict && c.verdict.verdict === 'disagree').length;
    delete S.data.targets;
    S.ovStale = true;
  };
  const cellsOf = (aid) => targets.map((t) => (d.cells[aid] || {})[t]).filter(Boolean);
  // An analysis left out of every target is one decision to the reader, so its row is one cell
  // that says why: the shared criteria that decided, and the reason for the target it came closest to.
  const outOfAll = (aid) => {
    const row = cellsOf(aid);
    return row.length > 1 && row.length === targets.length && !row.some((c) => c.include);
  };
  const sharedOut = (aid) => {
    const row = cellsOf(aid);
    return row[0].deciding.filter((id) => id.startsWith('G') && row.every((c) => c.deciding.includes(id)));
  };
  const closest = (aid) => cellsOf(aid).reduce((best, c) => (c.deciding.length < best.deciding.length ? c : best));
  const said = (row) => {
    const vs = row.map((c) => c.verdict && c.verdict.verdict);
    return vs.every(Boolean) ? (vs.includes('disagree') ? 'disagree' : 'agree') : null;
  };
  const open = (aid, target) => {
    ui.cell = ui.cell && ui.cell[0] === aid && ui.cell[1] === target ? null : [aid, target];
    render();
  };
  const [curAid, curTarget] = ui.cell || [];

  const read = h('div', { class: 'read wide' });
  add(read, h('h2', {}, it.title || '(no title)'), h('div', { class: 'meta' }, studyLinks(it)),
    it.lane === 'none_selected' ? h('p', { class: 'flag' }, '⚠ Included at full text, but no analysis was selected for any target, so this study is in no map.') : null);
  const table = h('table', { class: 'matrix' }, h('tr', {}, h('th', {}, 'Analysis'), targets.map((t) => h('th', {}, breakable(t))), h('th', {})));
  for (const a of d.analyses) {
    const aid = a.analysis_id, row = cellsOf(aid), on = curAid === aid;
    const desc = a.description ? h('div', { class: 'desc', title: 'Select to expand', onclick: (e) => e.currentTarget.classList.toggle('open') }, a.description) : null;
    const name = h('td', { class: 'name' },
      h('button', { class: 'btn link plain', 'aria-expanded': String(on), onclick: () => open(aid, '*') }, a.name || a.table || aid),
      h('div', { class: 'tiny' }, [tableLabel(a.table), plural(a.n_points || 0, 'peak')].filter(Boolean).join(' · ')), desc);
    let cells;
    if (outOfAll(aid)) {
      cells = h('td', { colspan: targets.length }, h('button', { class: 'cell out wide', 'aria-expanded': String(on), onclick: () => open(aid, '*') },
        h('span', { class: 'lead' }, 'Out of every target '), sharedOut(aid).map((id) => [critChip(id, all[id] || ''), ' ']),
        closest(aid).reason));
    } else {
      cells = targets.map((t) => {
        const c = (d.cells[aid] || {})[t];
        if (!c) return h('td', {}, h('span', { class: 'tiny' }, 'not judged'));
        return h('td', {}, h('button', { class: 'cell ' + (c.include ? 'in' : 'out'), 'aria-pressed': String(on && curTarget === t), title: c.reason,
          onclick: () => open(aid, t) }, c.include ? 'In' : 'Out'));
      });
    }
    add(table, h('tr', { class: on ? 'on' : null }, name, cells, h('td', { class: 'num' }, mark(said(row)))));
    if (on) {
      // Every target's reason for this analysis, under its row. Selecting a target takes a verdict on it.
      const shared = outOfAll(aid) ? sharedOut(aid) : [];
      add(table, h('tr', { class: 'why-row' }, h('td', { colspan: targets.length + 2 },
        shared.map((id) => h('div', { class: 'small muted' }, critChip(id), ' ', all[id])),
        h('table', { class: 'plain' }, targets.map((t) => {
          const c = (d.cells[aid] || {})[t];
          if (!c) return null;
          return h('tr', { class: curTarget === t ? 'on' : null },
            h('td', {}, h('button', { class: 'btn link', 'aria-pressed': String(curTarget === t), onclick: () => { ui.cell = [aid, t]; render(); } }, t)),
            h('td', {}, h('span', { class: 'chip' + (c.include ? ' include' : '') }, c.include ? 'In' : 'Out')),
            h('td', { class: 'ids' }, c.deciding.filter((id) => !shared.includes(id)).map((id) => [critChip(id, texts(t)(id) || ''), ' '])),
            h('td', {}, c.reason), h('td', { class: 'num' }, mark(c.verdict && c.verdict.verdict)));
        })))));
    }
  }
  add(read, h('div', { class: 'matrix-wrap' }, table));

  let barEl;
  const hint = (text, button) => h('div', { class: 'verdict' }, h('div', { class: 'inner' }, h('div', { class: 'row' },
    h('span', { class: 'muted' }, text), h('span', { class: 'spacer' }), button)));
  const cur = curAid && d.cells[curAid];
  if (cur && cur[curTarget]) {
    const c = cur[curTarget];
    barEl = verdictBar({
      existing: c.verdict, criteria: c.criteria, texts: texts(curTarget), deciding: c.deciding,
      onSave: async (verdict, picked, note) => {
        c.verdict = await api('verdict', { stage: 'selection', pmid: it.pmid, analysis_id: curAid, target: curTarget, verdict, criteria: picked, note });
        changed();
        ui.cell = [curAid, '*'];
        render();
      },
    });
  } else if (cur) {
    const row = cellsOf(curAid), left = row.filter((c) => !c.verdict);
    const shared = outOfAll(curAid) ? sharedOut(curAid) : [];
    const saveAll = async (verdict, picked, note) => {
      for (const t of targets) {
        const c = cur[t];
        if (!c || (verdict === 'agree' && c.verdict)) continue;
        c.verdict = await api('verdict', { stage: 'selection', pmid: it.pmid, analysis_id: curAid, target: t, verdict, criteria: picked, note });
      }
      changed();
      ui.cell = null;
      render();
    };
    if (shared.length) {
      // A shared criterion decides for every target, so one verdict covers them all.
      const criteria = Object.fromEntries(Object.entries(row[0].criteria).filter(([id]) => id.startsWith('G')));
      const existing = left.length ? null : (row.find((c) => c.verdict.verdict === 'disagree') || row[0]).verdict;
      barEl = verdictBar({ existing, criteria, texts: (id) => all[id], deciding: shared, onSave: saveAll });
    } else {
      const agree = () => saveAll('agree', [], '').catch((e) => banner(e.message));
      if (left.length) S.keys.a = agree;
      barEl = hint(left.length ? 'To disagree, select the target.' : 'You have reviewed this analysis for every target.',
        left.length ? h('button', { class: 'btn good', onclick: agree }, 'Agree', h('kbd', {}, 'A')) : null);
    }
  } else {
    const left = it.n_cells - it.reviewed;
    const agreeAll = async () => {
      try {
        await api('agree-rest', { pmid: it.pmid });
        for (const row of Object.values(d.cells)) for (const c of Object.values(row)) if (!c.verdict) c.verdict = { verdict: 'agree', criteria: [], note: null, via: 'page' };
        changed();
        S.advance();
      } catch (e) { banner(e.message); }
    };
    // No key for this one: it agrees with decisions the reader may not have opened.
    barEl = hint(left > 0 ? 'Select an analysis to read its reasons or to disagree.' : 'You have reviewed every analysis of this study.',
      left > 0 ? h('button', { class: 'btn good', onclick: agreeAll }, it.reviewed ? 'Agree with the rest' : `Agree with all ${plural(it.n_analyses, 'analysis', 'analyses')}`) : null);
  }
  if (d.fulltext_reason) add(read, h('details', {}, h('summary', {}, 'Why this study was included at full text'), h('p', { class: 'reason' }, d.fulltext_reason)));
  if (d.has_text) add(read, fullTextToggle(it.pmid));
  set(pane, read, barEl);
}

// ------------------------------------------------------------------ targets
// One target at a time: the analyses it collected, with the judge's reason beside each name.
// Reading down the list is the check; a row that does not belong takes a verdict.
function targetsUi() {
  return S.ui.targets || (S.ui.targets = { cur: null, row: null, caps: {} });
}
async function viewTargets(main) {
  if (!S.data.targets) S.data.targets = await api('targets');
  const d = S.data.targets, ui = targetsUi();
  if (S.view !== 'targets') return;
  if (!d.targets.some((t) => t.considered)) {
    set(main, h('div', { class: 'empty' }, h('h2', {}, 'No analyses selected yet'),
      h('p', {}, 'The judges have not run analysis selection. Select Reload after it has run.')));
    return;
  }
  if (!d.targets.some((t) => t.name === ui.cur)) { ui.cur = d.targets[0].name; ui.row = null; }
  const t = d.targets.find((x) => x.name === ui.cur);
  const texts = (id) => (id.startsWith('G') ? d.global[id] : t.criteria[id]);
  // Studies come in a seeded random order, so the ones shown first are a sample of the target.
  const studies = t.by_study.slice(0, ui.caps[t.name] || 20);
  const rows = studies.flatMap((s) => s.analyses.map((a) => ({ s, a, key: s.pmid + '|' + a.analysis_id })));
  const cur = rows.find((r) => r.key === ui.row);
  const move = (step) => {
    const i = rows.findIndex((r) => r.key === ui.row);
    const next = rows[Math.max(0, Math.min(rows.length - 1, i + step))];
    if (next) { ui.row = next.key; render(); }
  };
  Object.assign(S.keys, { j: () => move(1), k: () => move(-1) });

  const page = h('div', { class: 'page wide' });
  add(page, h('div', { class: 'seg', role: 'tablist' }, d.targets.map((x) =>
    h('button', { role: 'tab', 'aria-selected': String(x.name === ui.cur), onclick: () => { ui.cur = x.name; ui.row = null; render(); } },
      x.name, h('span', { class: 'count num' }, fmt(x.analyses))))));

  const m = t.map, counts = Object.fromEntries(t.left_out_by.map((c) => [c.id, c.n]));
  add(page, h('div', { class: 'target-head' },
    t.description ? h('p', { class: 'muted' }, t.description) : null,
    h('p', {}, h('strong', {}, `${plural(t.analyses, 'analysis', 'analyses')} from ${plural(t.studies, 'study', 'studies')}`),
      !S.ov.pilot && t.studies < 17 ? h('span', { class: 'flag' }, ' · ⚠ few studies for a meta-analysis (17 or more is usual)') : null,
      m ? h('span', { class: 'muted' }, ` · map: ${plural(m.clusters, 'significant cluster')}`) : null,
      m && m.stale ? h('span', { class: 'flag' }, ' · ⚠ fitted before the current selection') : null),
    t.left_out ? h('p', { class: 'strip small' }, h('span', { class: 'muted' }, `${fmt(t.left_out)} left out by`),
      t.left_out_by.map((c) => h('span', { title: c.text || '' }, critChip(c.id), ' ', h('span', { class: 'num' }, fmt(c.n)))),
      t.no_criterion ? h('span', { title: 'No criterion failed; the judge gave another reason' }, critChip('other'), ' ', h('span', { class: 'num' }, fmt(t.no_criterion))) : null) : null));
  add(page, h('div', { class: 'fold' },
    h('details', {}, h('summary', {}, 'Criteria'),
      Object.entries(Object.assign({}, d.global, t.criteria)).map(([id, text]) => h('div', { class: 'bar-row small' },
        h('span', {}, critChip(id), ' ', text), h('span', { class: 'n' }, counts[id] ? fmt(counts[id]) : ''), bar((counts[id] || 0) / Math.max(1, t.considered), 120)))),
    m ? h('details', {}, h('summary', {}, 'The map'),
      h('p', { class: 'small' }, `${m.estimator} with ${m.corrector} correction, fitted on ${m.analyses} analyses from ${plural(m.studies, 'study', 'studies')}.` +
        (m.unknown_space_points ? ` ${plural(m.unknown_space_points, 'peak')} had no stated space and were treated as MNI.` : '')),
      m.largest.length ? h('table', { class: 'plain narrow' }, h('tr', {}, h('th', {}, 'Largest clusters: peak (x, y, z)'), h('th', { class: 'num' }, 'Peak z'), h('th', { class: 'num' }, 'Size (mm³)')),
        m.largest.map((c) => h('tr', {}, h('td', {}, `${Math.round(c.x)}, ${Math.round(c.y)}, ${Math.round(c.z)}`),
          h('td', { class: 'num' }, Number(c.peak).toFixed(2)), h('td', { class: 'num' }, fmt(c.mm3))))) : null) : null));

  if (!rows.length) {
    add(page, h('p', { class: 'empty' }, '⚠ No analysis was selected for this target.'));
  } else {
    const table = h('table', { class: 'matrix picked' }, h('tr', {}, h('th', {}, 'Study'), h('th', {}, 'Analysis selected, and why'), h('th', { class: 'num' }, 'Peaks'), h('th', {})));
    for (const s of studies) {
      s.analyses.forEach((a, i) => {
        const key = s.pmid + '|' + a.analysis_id, on = key === ui.row;
        add(table, h('tr', { class: on ? 'on' : null },
          i ? null : h('td', { class: 'study', rowspan: s.analyses.length }, h('div', { class: 'clamp2' }, s.title || s.pmid), h('div', { class: 'tiny' }, s.year || '')),
          h('td', {}, h('button', { class: 'rowbtn', 'aria-expanded': String(on), onclick: () => { ui.row = on ? null : key; render(); } },
              h('span', {}, a.name || a.analysis_id), h('span', { class: 'why ' + (on ? '' : 'clip') }, a.reason || 'The judge gave no reason.')),
            on ? h('button', { class: 'btn link small', onclick: () => {
              Object.assign(stageUi('selection'), { cur: s.pmid, cell: [a.analysis_id, t.name], caps: { none_selected: 1e9, selected: 1e9 }, unreviewed: false, criterion: '' });
              go('selection');
            } }, 'See every analysis of this study') : null),
          h('td', { class: 'num' }, a.n_points == null ? '' : a.n_points),
          h('td', { class: 'num' }, mark(a.verdict && a.verdict.verdict))));
      });
    }
    add(page, h('div', { class: 'matrix-wrap' }, table));
    const left = t.by_study.length - studies.length;
    if (left > 0) {
      add(page, h('button', { class: 'btn small more', onclick: () => { ui.caps[t.name] = studies.length + 20; render(); } },
        `Show 20 more of ${plural(left, 'study', 'studies')}`));
    }
  }
  if (d.without_coordinates.length) {
    add(page, h('details', { class: 'card' }, h('summary', {}, `${plural(d.without_coordinates.length, 'included study', 'included studies')} without coordinates, in no map`),
      h('p', { class: 'muted small' }, 'Report these as included, with coordinates unavailable.'),
      h('ul', { class: 'clean small' }, d.without_coordinates.map((s) => h('li', {}, s.title, h('div', { class: 'tiny' }, studyLinks(s)))))));
  }

  const barEl = cur ? verdictBar({
    existing: cur.a.verdict, criteria: cur.a.criteria, texts, deciding: [],
    onSave: async (verdict, picked, note) => {
      cur.a.verdict = await api('verdict', { stage: 'selection', pmid: cur.s.pmid, analysis_id: cur.a.analysis_id, target: t.name, verdict, criteria: picked, note });
      delete S.detail['selection:' + cur.s.pmid];
      delete S.data.selection;
      S.ovStale = true;
      const next = rows.slice(rows.indexOf(cur) + 1).find((r) => !r.a.verdict);
      ui.row = next ? next.key : null;
      render();
    },
  }) : null;
  set(main, page, barEl);
  const active = main.querySelector('tr.on');
  if (active) active.scrollIntoView({ block: 'nearest' });
}

// ------------------------------------------------------------------ findings
async function viewFindings(main) {
  const d = await api('findings');
  const page = h('div', { class: 'page' });
  const total = Object.values(d.stages).reduce((n, s) => n + s.reviewed, 0);
  add(page, h('div', { class: 'card' }, h('h2', {}, 'Your findings'),
    h('p', {}, total ? 'Your disagreements are grouped by the criterion you named, because a criterion is what an amendment can change. Your verdicts never change a decision themselves.'
      : 'You have not reviewed any decision yet. Start from the Overview.'),
    h('div', { class: 'row' },
      h('button', { class: 'btn primary', onclick: finish }, 'Finish review'),
      h('button', { class: 'btn', onclick: () => copy(d.markdown) }, 'Copy the summary'),
      h('span', { class: 'tiny', id: 'copied' }, ''))));
  for (const [stage, s] of Object.entries(d.stages)) {
    if (!s.reviewed && !s.criteria.length) continue;
    const card = h('div', { class: 'card' }, h('h2', {}, STAGE_NAME[stage][0].toUpperCase() + STAGE_NAME[stage].slice(1)),
      h('table', { class: 'plain' }, h('tr', {}, h('th', {}, 'Group'), h('th', { class: 'num' }, 'Decisions'), h('th', { class: 'num' }, 'Reviewed'), h('th', { class: 'num' }, 'Agreed')),
        s.lanes.filter((l) => l.size).map((l) => h('tr', {}, h('td', {}, l.label), h('td', { class: 'num' }, l.size), h('td', { class: 'num' }, l.reviewed),
          h('td', { class: 'num' }, l.reviewed ? `${l.agree} of ${l.reviewed}` : '')))));
    for (const c of s.criteria) add(card, criterionBlock(stage, c));
    const others = Object.keys(s.all_criteria).filter((id) => !s.criteria.some((c) => c.id === id));
    if (others.length) {
      const sel = h('select', {}, others.map((id) => h('option', { value: id }, `${id}: ${(s.all_criteria[id] || '').slice(0, 80)}`)));
      add(card, h('details', {}, h('summary', {}, 'Note something about another criterion'),
        h('div', { class: 'row' }, sel, h('button', { class: 'btn small', onclick: () => { s.criteria.push({ id: sel.value, text: s.all_criteria[sel.value], items: [], note: '' }); card.insertBefore(criterionBlock(stage, s.criteria[s.criteria.length - 1]), card.lastElementChild.previousElementSibling); } }, 'Add a note'))));
    }
    if (s.disagree || s.criteria.length) add(card, h('p', { class: 'tiny' }, `Changing a criterion here is an amendment, and it re-opens all ${fmt(s.decisions)} ${STAGE_NAME[stage]} decisions${S.ov.pilot ? '' : ', which the judges then decide again'}.`));
    add(page, card);
  }
  set(main, page);
}
function criterionBlock(stage, c) {
  const area = h('textarea', { placeholder: 'What should this criterion say? (optional)', rows: 2 });
  area.value = c.note || '';
  const status = h('span', { class: 'tiny' }, '');
  const save = async () => {
    try { await api('criterion-note', { stage, criterion: c.id, note: area.value }); status.textContent = 'Saved.'; }
    catch (e) { banner(e.message); }
  };
  return h('div', { class: 'section' },
    h('h3', {}, critChip(c.id), ' ', c.text || 'No criterion named'),
    c.items.length ? h('ul', { class: 'clean small' }, c.items.map((it) => h('li', {},
      h('span', { class: 'state-not_met' }, `Judged ${it.judged}. `), it.note || 'No note.',
      h('div', { class: 'tiny' }, (it.analysis_id ? `${it.analysis || it.analysis_id} × ${it.target} · ` : '') + (it.title || it.pmid))))) : null,
    h('div', { class: 'notebox' }, area, h('div', { class: 'row' }, h('button', { class: 'btn small', onclick: save }, 'Save note'), status)));
}
async function copy(text) {
  try { await navigator.clipboard.writeText(text); $('#copied').textContent = 'Copied.'; }
  catch (e) { banner('Copying is blocked here. The same summary is written to pilot/findings.md when you finish.'); }
}
async function finish() {
  try {
    const d = await api('done', {});
    S.keys = {};
    set($('#top'), h('div', { class: 'top-row' }, h('h1', {}, S.ov.name)));
    set($('#main'), h('div', { class: 'empty' }, h('h2', {}, 'Review finished'),
      h('p', {}, 'Your verdicts and notes are saved, and the summary is in:'), h('p', { class: 'small' }, d.findings),
      h('p', {}, 'Go back to the agent. It has your findings and will propose criteria edits from them, or continue the run if you had none. You can close this tab.')));
  } catch (e) { banner(e.message); }
}

// ------------------------------------------------------------------ routing
async function render() {
  S.keys = {};
  const m = location.hash.match(/^#\/(\w+)/);
  S.view = m && VIEWS.some(([id]) => id === m[1]) ? m[1] : 'overview';
  const main = $('#main');
  try {
    if (S.view === 'overview' && S.ovStale) { S.ov = await api('overview'); S.ovStale = false; }
    renderTop();
    if (S.view === 'overview') viewOverview(main);
    else if (S.view === 'targets') await viewTargets(main);
    else if (S.view === 'findings') await viewFindings(main);
    else await viewStage(main, S.view);
  } catch (e) { banner(e.message); }
}

document.addEventListener('keydown', (e) => {
  const tag = (e.target.tagName || '').toLowerCase();
  const typing = tag === 'textarea' || tag === 'input' || tag === 'select';
  const submit = e.key === 'Enter' && (e.metaKey || e.ctrlKey);
  if ((e.metaKey || e.ctrlKey || e.altKey) && !submit) return;
  let fn = null;
  if (typing) fn = e.key === 'Escape' ? S.keys.Escape : (submit ? S.keys.Submit : null);
  else if (tag === 'button' && e.key === 'Enter' && !S.keys.Enter) fn = null;
  else fn = S.keys[e.key];
  if (fn) { e.preventDefault(); fn(e); }
});
window.addEventListener('hashchange', () => {
  if (/^#t=/.test(location.hash)) location.reload(); else render();   // a fresh link carries a fresh token
});

(async function boot() {
  S.token = takeToken();
  if (!S.token) {
    set($('#main'), h('div', { class: 'empty' }, h('h2', {}, 'This page needs its link'),
      h('p', {}, 'Open the review page from the link that review_page.py opens. If the agent started it, ask it to open the page again.')));
    return;
  }
  try { S.ov = await api('overview'); } catch (e) { banner(e.message); set($('#main'), ); return; }
  document.title = `Review: ${S.ov.name}`;
  render();
})();
