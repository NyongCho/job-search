// Unit boundary test for the real save handler; no browser or network is launched.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../job_search/static/dashboard.js'), 'utf8');

test('applying a list order starts at the top and returning from detail restores the list', () => {
  const positions = new Map([['job-list:recommended:deadline::1', '{"list":600,"page":0}']]);
  function open(detail, navigation = 'navigate') {
    const list = {dataset: {scrollKey: 'recommended:deadline::1'}, scrollTop: 0};
    const back = {addEventListener(type, handler) { this[type] = handler; }};
    const selectors = {'#job-list': list, '.back-link': back};
    if (detail) selectors['#job-detail'] = {dataset: {jobId: '1'}, querySelectorAll() { return []; }};
    vm.runInNewContext(script, {
      document: {querySelector(key) { return selectors[key] || null; }},
      performance: {getEntriesByType() { return [{type: navigation}]; }},
      sessionStorage: {getItem: k => positions.get(k), setItem: (k,v) => positions.set(k,v),
                       removeItem: k => positions.delete(k)},
      window: {addEventListener() {}}, matchMedia: () => ({matches: false}),
    });
    return {list, back};
  }
  assert.equal(open(false).list.scrollTop, 0);
  const detail = open(true);
  assert.equal(detail.list.scrollTop, 600);
  detail.back.click();
  assert.equal(open(false).list.scrollTop, 600);
  assert.equal(open(false).list.scrollTop, 0);
  assert.equal(open(false, 'back_forward').list.scrollTop, 600);
});

test('deadline preview applies the same closure and Korea time rules as saving', () => {
  const {deadlinePreview, deadlineWarning} = vm.runInNewContext(
    script + '\n;({deadlinePreview, deadlineWarning});', {document: {querySelector() { return null; }}});
  const beforeMidnight = Date.parse('2026-11-15T14:59:59.995Z');
  const auto = {kind: 'unknown', closed: true, is_expired: true};
  const input = kind => ({kind, date: '2026-11-15', time: '23:59'});
  assert.equal(deadlinePreview(input('date'), auto, beforeMidnight), false);
  assert.equal(deadlinePreview(input('date'), auto, beforeMidnight + 10), true);
  assert.equal(deadlinePreview(input('datetime'), auto, beforeMidnight), false);
  assert.equal(deadlinePreview(input('rolling'), auto, beforeMidnight), false);
  assert.equal(deadlinePreview(input('unknown'), auto, beforeMidnight), true);
  assert.match(deadlineWarning(false, true), /다시 만료/);
  assert.match(deadlineWarning(true, false), /만료 표시가 해제/);
  assert.equal(deadlineWarning(false, false), '');
});

test('deadline cancel writes nothing and failed saves retain editable input', async () => {
  const element = (extra = {}) => ({
    events: {}, textContent: '', focused: false,
    addEventListener(name, handler) { this.events[name] = handler; },
    setAttribute() {},
    focus() { this.focused = true; },
    ...extra,
  });
  const edit = element(), save = element(), cancel = element(), error = element(), warning = element();
  const radio = element(), fields = element({querySelectorAll() { return []; }});
  const inputs = Object.fromEntries(['mode', 'kind', 'date', 'time', 'word'].map(name => [name, {value: ''}]));
  const form = element({elements: inputs, querySelector(selector) {
    return {'.deadline-fields': fields, '#deadline-warning': warning, '#deadline-error': error,
      'button[type="submit"]': save, '[data-deadline-cancel]': cancel, 'input[name="mode"]:checked': radio}[selector];
  }});
  const auto = {kind: 'unknown', date: '', time: '', word: '', version: 'a', is_expired: true, closed: true};
  const dialog = element({
    dataset: {deadline: JSON.stringify({auto, expired: true, manual: null})},
    open: false, querySelector: () => form,
    showModal() { this.open = true; },
    close() { this.open = false; this.events.close(); },
  });
  const calls = [];
  const wire = vm.runInNewContext(script + '\n;wireDeadline;', {
    document: {querySelector(selector) { return selector === '#deadline-edit' ? edit : null; }},
    location: {search: '?view=expired&sort=deadline&page=2'},
    FormData: class {
      *[Symbol.iterator]() {
        for (const [name, {value}] of Object.entries(inputs)) yield [name, value];
      }
    },
    async fetch(url, {body}) {
      calls.push([url, JSON.parse(body)]);
      return {ok: false, json: async () => ({message: '다시 시도해 주세요.'})};
    },
  });
  wire(dialog, '1');
  edit.events.click();
  cancel.events.click();
  assert.equal(calls.length, 0);
  assert.ok(edit.focused);
  edit.events.click();
  inputs.mode.value = 'manual';
  inputs.kind.value = 'date';
  inputs.date.value = '2099-12-11';
  form.events.input();
  assert.match(warning.textContent, /만료 표시가 해제/);
  await form.events.submit({preventDefault() {}});
  assert.equal(calls[0][0], '/jobs/1/deadline?view=expired&sort=deadline&page=2');
  assert.equal(calls[0][1].date, '2099-12-11');
  assert.ok(dialog.open);
  assert.equal(inputs.date.value, '2099-12-11');
  assert.equal(error.textContent, '다시 시도해 주세요.');
  assert.equal(save.disabled, false);
});

test('saving one stage retains sibling unsaved notes and updates its badge', async () => {
  const classList = { add() {}, remove() {} };
  const control = () => ({ disabled: false });
  const makeForm = (stage, note) => {
    const status = {textContent: '', classList};
    const badge = {textContent: '미확인', className: 'stage-result unknown'};
    const stamp = {textContent: ''};
    return { dataset: {stage}, values: {result: 'passed', event_date: '', note}, handler: null,
      addEventListener(_, handler) { this.handler = handler; },
      querySelector(selector) {
        return {'.stage-save-status': status, '.stage-result': badge, '.stage-footer > span': stamp,
          'select': { selectedOptions: [{textContent: '합격'}] }}[selector];
      }, status, badge };
  };
  const forms = [makeForm('document', '서류 확인'), makeForm('interview1', '아직 저장하지 않은 면접 메모')];
  const controls = [control(), control()];
  const detail = {dataset: {jobId:'-1', reviewedAt:''},
    querySelectorAll(selector) { return selector==='[data-status]' ? [] : selector==='.progress-form' ? forms : controls; }};
  const message = {textContent:'',classList};
  const preview = {textContent:''};
  let reloads = 0;
  const writes = [];
  vm.runInNewContext(script, {
    document: {querySelector(selector) {return {'#job-detail':detail,'#save-status':message,
      'meta[name="csrf-token"]':{content:'csrf'}, '.job-row.selected .progress-preview':preview}[selector] || null;}},
    sessionStorage: {getItem(){return null;}, setItem(){}, removeItem(){}},
    location: {reload(){ reloads++; forms.forEach(form=>form.values.note=''); }},
    FormData: class {constructor(form){this.entries=Object.entries(form.values);} [Symbol.iterator](){return this.entries[Symbol.iterator]();}},
    fetch: async (_, options) => {const payload=JSON.parse(options.body); writes.push(payload);
      return {ok:true,json:async()=>({...payload,updated_at:'2026-11-16T00:00:00Z',progress_label:'서류 · 합격'})};},
  });
  forms[0].handler({preventDefault(){}});
  await new Promise(setImmediate);
  assert.equal(forms[1].values.note, '아직 저장하지 않은 면접 메모');
  assert.equal(reloads, 0);
  assert.equal(writes.length, 1);
  assert.equal(writes[0].stage, 'document');
  assert.equal(forms[0].badge.textContent, '합격');
  assert.equal(preview.textContent, '서류 · 합격');
  assert.ok(controls.every(control=>!control.disabled));
});

test('newer screen read wins when an earlier response finishes after a manual save', async () => {
  const requests = [];
  const accepted = [];
  const load = vm.runInNewContext(script + '\n;readCurrentScreen;', {
    document: {querySelector() { return null; }}, location: {href: 'https://dashboard.example/jobs/1'},
    fetch(url, options) {
      assert.equal(options.headers['X-Dashboard-Snapshot'], '1');
      return new Promise(resolve => requests.push(content => resolve({ok: true, text: async () => content})));
    },
  });
  const oldRead = load(html => accepted.push(html));
  const newRead = load(html => accepted.push(html));
  requests[1]('saved value');
  await newRead;
  requests[0]('previous value');
  await oldRead;
  assert.deepEqual(accepted, ['saved value']);
});

test('refresh submission captures the applied filter across pages and only targets an ID for detail', async () => {
  const submissions = [];
  const submit = vm.runInNewContext(script + '\n;submitRefresh;', {
    document: {querySelector() { return null; }}, URLSearchParams,
    fetch: async (url, options) => {
      submissions.push({url, options});
      return {ok: true, json: async () => ({id: 17, complete: false})};
    },
  });
  const page = {view: 'applied', query: 'data & ai'};
  await submit(page, undefined, '7a748bff-e93d-4b95-b8d9-f6b69fa3bd19');
  await submit(page, '-7', '21c66fb5-2f18-4ff8-a003-bccd23c5293a');
  for (const {url, options} of submissions) {
    const target = new URL(url, 'https://dashboard.example');
    assert.equal(target.pathname, '/refreshes');
    assert.deepEqual([...target.searchParams], [['view', 'applied'], ['q', 'data & ai']]);
    assert.equal(options.keepalive, true);
  }
  assert.deepEqual(submissions.map(s => JSON.parse(s.options.body)), [{}, {post_id: -7}]);
});

test('recovering after navigation resubmits a missing POST with its original identity and filter', async () => {
  const requests = [];
  const saved = {id: 'ae6f89f0-5238-489b-8c34-e01ddcd9ee9d', scope: {view: 'interested', query: 'Agent'}, postId: 7};
  let accepted = false;
  const recover = vm.runInNewContext(script + '\n;recoverRefresh;', {
    document: {querySelector() { return null; }}, URLSearchParams,
    fetch: async (url, options = {}) => {
      requests.push([url, options]);
      if (!options.method) return {ok: true, json: async () => ({runs: accepted ? [{id: 11}] : []})};
      accepted = true;
      return {ok: true, json: async () => ({id: 11})};
    },
  });
  assert.equal((await recover(saved)).id, 11);
  assert.equal((await recover(saved)).id, 11);
  assert.equal(requests.filter(([, options]) => options.method === 'POST').length, 1);
  assert.equal(requests[0][0], '/refreshes?request=' + saved.id);
  const [url, options] = requests[1];
  assert.equal(url, '/refreshes?view=interested&q=Agent');
  assert.equal(options.headers['X-Refresh-Request'], saved.id);
  assert.deepEqual(JSON.parse(options.body), {post_id: 7});
});

test('an obsolete failed read cannot overwrite feedback from a newer successful screen', async () => {
  let failOld;
  let calls = 0;
  const load = vm.runInNewContext(script + '\n;readCurrentScreen;', {
    document: {querySelector() { return null; }}, location: {href: 'https://dashboard.example/jobs/1'},
    fetch() {
      if (++calls === 1) return new Promise((resolve, reject) => { failOld = reject; });
      return Promise.resolve({ok: true, text: async () => 'new state'});
    },
  });
  const rendered = [];
  const old = load(html => rendered.push(html));
  await load(html => rendered.push(html));
  failOld(new Error('network disconnected'));
  await old;
  assert.deepEqual(rendered, ['new state']);
});

test('a confirmed deadline repaints while the worker still reads the linked JD', async () => {
  let snapshots = 0;
  const context = {
    document: {querySelector() { return null; }, querySelectorAll() { return []; }},
    fetch: async () => ({ok: true, json: async () => ({
      id: 1, cursor: 2, complete: false, counts: {running: 1, done: 0, skipped: 0, failed: 0},
      changes: [{kind: 'deadline', status: 'running'}],
    })}),
    setTimeout() {}, clearTimeout() {}, onSnapshot() { snapshots++; },
  };
  const pollOnce = vm.runInNewContext(script + `
    runs.set(1, {id: 1, complete: false, cursor: 1});
    refreshScreen = async () => onSnapshot();
    poll;
  `, context);
  await pollOnce();
  assert.equal(snapshots, 1);
});

test('each refresh control reports the same requests as its usage links', () => {
  const status = ids => ({
    textContent: '',
    parentElement: {querySelectorAll() { return ids.map(id => ({dataset: {runId: String(id)}})); }},
  });
  const list = status([10]), detail = status([10, 12]);
  const context = {document: {
    querySelector() { return null; }, querySelectorAll() { return [list, detail]; },
  }};
  vm.runInNewContext(script + `
    runs.set(10, {id: 10, complete: false, counts: {queued: 6, done: 2, failed: 1, skipped: 0, running: 0}});
    runs.set(12, {id: 12, complete: true, counts: {queued: 0, done: 2, failed: 1, skipped: 0, running: 0}});
    showProgress();
  `, context);
  assert.match(list.textContent, /새로고침 중 · 3\/9개 항목/);
  assert.match(detail.textContent, /#12 완료 · 3\/3개 항목 · 실패 1/);
});

test('an empty destination tab polls until active work can no longer change its cards or totals', async () => {
  const responses = [{active: true, cursor: 7}, {active: false, cursor: 8}];
  const requests = [], delays = [];
  let snapshots = 0;
  const pollOnce = vm.runInNewContext(script + `
    activity = {active: true, cursor: 7};
    refreshScreen = async () => onSnapshot();
    poll;
  `, {
    document: {querySelector() { return null; }, querySelectorAll() { return []; }},
    fetch: async url => {
      requests.push(url);
      return {ok: true, json: async () => responses.shift()};
    },
    setTimeout(fn, ms) { delays.push(ms); }, clearTimeout() {},
    onSnapshot() { snapshots++; },
  });
  await pollOnce();
  assert.equal(snapshots, 0);
  assert.deepEqual(delays, [1500]);
  await pollOnce();
  assert.deepEqual(requests, ['/refreshes/activity', '/refreshes/activity']);
  assert.equal(snapshots, 1);
  assert.deepEqual(delays, [1500]);
});
