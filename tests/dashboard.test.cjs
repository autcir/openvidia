const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const path = require('node:path')

// Execute the shipped dashboard, with DOM and transport adapters only. The
// action handlers, catalog refresh and rendering are the production functions.
function dashboard(fetch) {
  function element() {
    return { children: [], value: '', textContent: '', className: '', style: {},
      classList: { add() {}, remove() {}, toggle() {} },
      setAttribute() {}, addEventListener() {}, focus() {}, remove() {},
      appendChild(x) { this.children.push(x) },
      querySelector() { return element() },
      set innerHTML(value) { this.children = []; this.html = value },
      get innerHTML() { return this.html || '' }
    }
  }
  const nodes = new Map()
  const context = vm.createContext({ fetch, console, URLSearchParams, Date, AbortController,
    localStorage: { getItem() { return null }, setItem() {} },
    sessionStorage: { getItem() { return 'test-control-token' }, setItem() {} },
    location: { search: '', pathname: '/' }, history: { replaceState() {} },
    document: { documentElement: element(), querySelectorAll() { return [] },
      createElement: element, getElementById(id) { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id) } },
    EventSource: class {}, setInterval() {}, setTimeout() {}, clearTimeout() {}
  })
  const source = fs.readFileSync(path.join(__dirname, '../openvidia/web/main.js'), 'utf8')
  vm.runInContext(source.split('/* ── Init ─')[0], context)
  vm.runInContext('renderUsage = () => {}; const notices = []; toast = (text, level) => notices.push({text, level})', context)
  return { run: code => vm.runInContext(code, context), nodes }
}
const reply = (data, status = 200) => ({ ok: status < 400, status, json: async () => data, text: async () => JSON.stringify(data) })

for (const [action, route, variable, before, after, response] of [
  ["setModel('vendor/new')", '/api/model', 'activeModel', 'vendor/old', 'vendor/new', {ok:true, model:'vendor/new'}],
  ["setThinking('off')", '/api/thinking', 'thinkingMode', 'auto', 'off', {ok:true, mode:'off'}],
  ["setEffort('high')", '/api/reasoning-effort', 'reasoningEffort', 'auto', 'high', {ok:true, effort:'high'}]
]) {
  test(`${route}: authenticate and apply confirmed state`, async () => {
    const calls = []
    const d = dashboard(async (url, options) => { calls.push({url, options}); return reply(url === route ? response : {}) })
    d.run(`${variable} = ${JSON.stringify(before)}`)
    await d.run(action)
    assert.equal(calls[0].url, route)
    assert.equal(calls[0].options.headers['X-OpenVidia-Token'], 'test-control-token')
    assert.equal(d.run(variable), after)
    assert.equal(d.run('notices.at(-1).level'), 'ok')
  })
  for (const result of [reply({error:'denied'}, 401), reply({error:'unavailable'}, 503), reply({ok:false,error:'invalid setting'})]) {
    test(`${route}: rejected requests preserve previous state (${result.status})`, async () => {
      const d = dashboard(async () => result)
      d.run(`${variable} = ${JSON.stringify(before)}`)
      await d.run(action)
      assert.equal(d.run(variable), before)
      assert.equal(d.run('notices.at(-1).level'), 'error')
    })
  }
}

test('catalog refresh discovers new models and preserves the last good list after an outage', async () => {
  let response = reply({data:[{id:'vendor/new', capabilities:{tools:true}}]})
  const d = dashboard(async url => url === '/v1/models' ? response : reply({}))
  await d.run('fetchModels()')
  assert.equal(d.run('allModels[0].id'), 'vendor/new')
  assert.equal(d.run('allModels[0].capabilities.tools'), true)
  assert.match(d.nodes.get('catalogStatus').textContent, /Catalog refreshed/)
  response = reply({error:'busy'}, 503)
  await d.run('fetchModels()')
  assert.equal(d.run('allModels[0].id'), 'vendor/new')
  assert.match(d.nodes.get('catalogStatus').textContent, /refresh failed/)
  response = reply({data:[{id:'vendor/newer'}]})
  await d.run('fetchModels()')
  assert.equal(d.run('allModels[0].id'), 'vendor/newer')
  assert.doesNotMatch(d.nodes.get('catalogStatus').textContent, /refresh failed/)
})

test('invalid catalog does not destroy a previous successful result', async () => {
  let response = reply({data:[{id:'vendor/model'}]})
  const d = dashboard(async url => url === '/v1/models' ? response : reply({}))
  await d.run('fetchModels()')
  response = reply({error:'bad catalog'})
  await d.run('fetchModels()')
  assert.equal(d.run('allModels.length'), 1)
  assert.match(d.nodes.get('catalogStatus').textContent, /Invalid model catalog/)
})

test('retired active model and favorites remain visible but cannot be selected or probed', async () => {
  let calls = 0
  const d = dashboard(async url => { if (url === '/v1/models') { calls++; return reply({data:[{id:'vendor/live'}]}) }; return reply({model:'vendor/retired'}) })
  d.run("activeModel = 'vendor/retired'; presets = ['vendor/retired']; modelFilter = 'starred'")
  await d.run('fetchModels()')
  assert.match(d.nodes.get('activeModelDisplay').textContent, /unavailable/)
  const item = d.nodes.get('modelList').children[0]
  assert.match(item.className, /unavailable/)
  assert.equal(item.onclick, undefined)
  assert.equal(item.children.at(-1).children[0].disabled, true)
  await d.run("setModel('vendor/retired')")
  assert.equal(calls, 1)
  assert.equal(d.run('activeModel'), 'vendor/retired')
})

test('Verified is computed from current observed health, without frozen model ids', () => {
  const d = dashboard(async () => reply({}))
  d.run(`allModels = [
    {id:'new-vendor/new-model',health:{success:2,failure_rate:0,median_ttft:1}},
    {id:'vendor/down',health:{success:3,failure_rate:0,circuit_open:true}},
    {id:'vendor/unknown'},
    {id:'vendor/failing',health:{success:1,failure_rate:0.8}},
    {id:'openvidia',health:{success:1,failure_rate:0}}
  ]; modelFilter = 'verified'`)
  assert.equal(d.run('getFilteredModels().map(m => m.id).join()'), 'new-vendor/new-model')
})

test('favorite changes roll back on API failure', async () => {
  const d = dashboard(async () => reply({error:'offline'}, 503))
  d.run("presets = ['vendor/old']")
  assert.equal(await d.run("savePresets(['vendor/new'])"), false)
  assert.equal(d.run('presets.join()'), 'vendor/old')
})


test('refreshes do not overlap while the provider is slow', async () => {
  let resolveFetch
  let requests = 0
  const d = dashboard(async url => {
    if (url !== '/v1/models') return reply({})
    requests++
    return new Promise(resolve => { resolveFetch = resolve })
  })
  const first = d.run('fetchModels()')
  await d.run('fetchModels()')
  assert.equal(requests, 1)
  resolveFetch(reply({data:[]}))
  await first
  assert.equal(d.run('catalogFetchPending'), false)
})

test('Recent defaults to official dated free chat API models, preserving date order', () => {
  const d = dashboard(async () => reply({}))
  assert.equal(d.run('modelFilter'), 'recent')
  d.run(`allModels = [
    {id:'vendor/old',catalog:{endpoint_kind:'free_chat',updated_at:'2026-10-01T00:00:00Z'}},
    {id:'vendor/new',catalog:{endpoint_kind:'free_chat',updated_at:'2026-11-13T00:00:00Z'}},
    {id:'vendor/legacy'},
    {id:'vendor/embedding',catalog:{endpoint_kind:'other',updated_at:'2026-11-14T00:00:00Z'}},
    {id:'vendor/undated',catalog:{endpoint_kind:'free_chat',updated_at:'not-a-date'}},
    {id:'openvidia',catalog:{endpoint_kind:'free_chat',updated_at:'2026-11-14T00:00:00Z'}}
  ]; activeModel = 'vendor/old'`)
  assert.equal(d.run('getFilteredModels().map(m => m.id).join()'), 'vendor/new,vendor/old')
  d.run("modelFilter = 'all'")
  assert.equal(d.run('getFilteredModels().length'), 6)
})

test('Recent renders official names, exact IDs, dates and use cases as text', async () => {
  const model = {id:'vendor/current', catalog:{endpoint_kind:'free_chat',updated_at:'2026-11-13T12:00:00Z',display_name:'Current <Model>',use_cases:['Agentic coding','Tool use']}}
  const d = dashboard(async url => url === '/v1/models' ? reply({data:[model],catalog:{refreshed_at:'2026-11-13T12:30:00Z',stale:false}}) : reply({model:'vendor/other'}))
  await d.run('fetchModels()')
  const info = d.nodes.get('modelList').children[0].children[0]
  assert.equal(info.children[0].textContent, 'Current <Model>')
  assert.equal(info.children[1].textContent, 'vendor/current')
  assert.equal(info.children[2].textContent, 'Updated 2026-11-13')
  assert.equal(info.children[3].textContent, 'Agentic coding · Tool use')
  assert.match(d.nodes.get('catalogInfo').textContent, /not benchmark strength/)
  d.nodes.get('modelSearch').value = 'agentic'
  assert.equal(d.run('getFilteredModels().length'), 1)
  assert.equal(d.run('activeModel'), 'vendor/other')
})

test('Recent exposes metadata outages and keeps full API catalog accessible', async () => {
  const d = dashboard(async url => url === '/v1/models' ? reply({data:[{id:'vendor/legacy'}],catalog:{stale:true,matched_models:0}}) : reply({}))
  await d.run('fetchModels()')
  assert.equal(d.run('getFilteredModels().length'), 0)
  assert.match(d.nodes.get('catalogInfo').textContent, /metadata is unavailable/)
  d.run("modelFilter = 'all'")
  assert.equal(d.run('getFilteredModels()[0].id'), 'vendor/legacy')
  d.run("allModels = [{id:'vendor/current',catalog:{endpoint_kind:'free_chat',updated_at:'2026-11-13T00:00:00Z'}}]; renderCatalogStatus()")
  assert.match(d.nodes.get('catalogInfo').textContent, /Metadata is stale/)
})
