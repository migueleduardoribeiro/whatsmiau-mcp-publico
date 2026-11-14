'use strict';
// Execute the real handlers against a small DOM fixture. No WhatsApp calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor() { this.handlers = {}; this.value = ''; this.textContent = ''; this.hidden = false; this.disabled = false; this.open = false; this.children = []; this.checked = false; this.dataset = {}; this.classList = {add(){}, remove(){}}; }
  addEventListener(type, handler) { this.handlers[type] = handler; }
  replaceChildren(...children) { this.children = children; }
  append(...children) { this.children.push(...children); }
  showModal() { this.open = true; }
  close() { this.open = false; this.handlers.close?.(); }
  focus() {}
  reset() {}
  removeAttribute() {}
  setAttribute() {}
  querySelectorAll() { return this.children.map(c => c.children?.[0]).filter(Boolean); }
}
const elements = new Map();
const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
const calls = [];
let fail = false;
const sandbox = {
  document: {getElementById: get, querySelectorAll: () => [], querySelector: () => null, addEventListener(){}, createElement: () => new Element(), body: get('body'), hidden: false},
  window: {addEventListener(){}},
  navigator: {clipboard: {writeText: async text => calls.push({copy: text})}},
  AbortController, setTimeout: () => 1, clearTimeout(){}, setInterval(){}, console,
  fetch: async (path, options) => {
    calls.push({path, options});
    let result = {instances: []};
    if (path.endsWith('/webhook')) result = {webhook: {url: 'https://example.org/hook', enabled: true, base64: false, events: ['CALL']}, revision: 'revision', supported_events: ['CALL', 'MESSAGES_UPSERT'], unsupported_events: [], managed: false};
    if (path.endsWith('/details')) result = {id: 'sample', name: 'Atendimento', api_url: 'https://example.org/v1'};
    if (path.endsWith('/credentials/reveal')) result = {api_key: 'fixture-secret'};
    return {status: fail ? 502 : 200, ok: !fail, json: async () => fail ? {error: 'API indisponível'} : result};
  },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('static/app.js', 'utf8'), sandbox);
const run = code => vm.runInContext(code, sandbox);
const tick = async () => { await new Promise(resolve => setImmediate(resolve)); };
async function main() {
  await tick(); calls.length = 0;
  await run("openAction({id:'sample',name:'Atendimento'}, 'delete')");
  assert.equal(calls.length, 0);
  assert.equal(get('action-submit').disabled, true);
  get('action-name').value = 'Wrong'; get('action-name').handlers.input();
  assert.equal(get('action-submit').disabled, true);
  await get('action-form').handlers.submit({preventDefault(){}});
  assert.equal(calls.length, 0);
  get('action-name').value = 'Atendimento'; get('action-name').handlers.input();
  assert.equal(get('action-submit').disabled, false);
  await get('action-form').handlers.submit({preventDefault(){}});
  assert.equal(calls.filter(c => c.path.endsWith('/delete')).length, 1);
  assert.deepEqual(JSON.parse(calls[0].options.body), {name: 'Atendimento', confirm: true});
  calls.length = 0;
  run("openAction({id:'sample',name:'Atendimento'}, 'logout')");
  assert.equal(calls.length, 0); get('action-dialog').close(); assert.equal(calls.length, 0);
  await run("openWebhook({id:'sample',name:'Atendimento'})");
  assert.equal(get('webhook-url').value, 'https://example.org/hook');
  assert.equal(get('webhook-submit').disabled, false);
  assert.equal(get('webhook-events').children[0].children[0].checked, true);
  await get('webhook-form').handlers.submit({preventDefault(){}});
  assert.equal(calls.filter(c => c.path.endsWith('/webhook') && c.options.method === 'POST').length, 1);
  fail = true; await run("openWebhook({id:'sample',name:'Atendimento'})");
  assert.equal(get('webhook-submit').disabled, true); assert.equal(get('webhook-error').textContent, 'API indisponível'); fail = false;
  calls.length = 0;
  await run("openInfo({id:'sample',name:'Atendimento'})");
  assert.equal(calls.some(c => c.path.endsWith('/credentials/reveal')), false);
  assert.equal(get('credential-key').value, '••••••••••••••••');
  assert.equal(get('credential-copy').disabled, true);
  await get('credential-reveal').handlers.click();
  assert.equal(get('credential-key').value, 'fixture-secret');
  await get('credential-copy').handlers.click();
  assert.ok(calls.some(c => c.copy === 'fixture-secret'));
  get('info-dialog').close(); assert.equal(get('credential-key').value, '••••••••••••••••');
  assert.equal(get('credential-copy').disabled, true);
  // Closing while an API request is pending must not reveal into a closed dialog.
  await run("openInfo({id:'sample',name:'Atendimento'})");
  const original = sandbox.fetch;
  let resolve;
  sandbox.fetch = () => new Promise(r => { resolve = r; });
  const pending = get('credential-reveal').handlers.click();
  get('info-dialog').close();
  resolve({status: 200, ok: true, json: async () => ({api_key: 'late-secret'})});
  await pending; sandbox.fetch = original;
  assert.equal(get('credential-key').value, '••••••••••••••••');
  // Reopening a dialog during a previous write must become usable when it ends.
  await run("openWebhook({id:'sample',name:'Atendimento'})");
  sandbox.fetch = (path, options) => options.method === 'POST' ? new Promise(r => { resolve = r; }) : original(path, options);
  const saving = get('webhook-form').handlers.submit({preventDefault(){}});
  get('webhook-dialog').close(); await run("openWebhook({id:'sample',name:'Atendimento'})");
  assert.equal(get('webhook-submit').disabled, true);
  resolve({status: 200, ok: true, json: async () => ({})}); await saving;
  assert.equal(get('webhook-submit').disabled, false);
  run("openAction({id:'sample',name:'Atendimento'}, 'logout')");
  const removing = get('action-form').handlers.submit({preventDefault(){}});
  get('action-dialog').close(); run("openAction({id:'sample',name:'Atendimento'}, 'logout')");
  assert.equal(get('action-submit').disabled, true);
  resolve({status: 200, ok: true, json: async () => ({})}); await removing;
  assert.equal(get('action-submit').disabled, false);
  sandbox.fetch = original;
  console.log('UI: confirmations, webhook prefill/save/errors, explicit reveal/copy and late-response cleanup passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
