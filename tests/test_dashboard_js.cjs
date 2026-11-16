// Unit boundary test for the real save handler; no browser or network is launched.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const path = require('node:path');

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
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../job_search/static/dashboard.js'),'utf8'), {
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
