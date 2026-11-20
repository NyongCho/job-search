// Unit boundary test for the real save handler; no browser or network is launched.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../job_search/static/dashboard.js'), 'utf8');

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
    location: {search: '?view=expired&page=2'},
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
  assert.equal(calls[0][0], '/jobs/1/deadline?view=expired&page=2');
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
