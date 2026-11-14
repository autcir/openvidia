let theme = localStorage.getItem('openvidia-theme') || 'dark'
let keys = []
let activeModel = ''
let allModels = []
let presets = []
let _logEntries = 0
let modelFilter = 'tested'
let keyFilter = 'active'
let _lastKeyStats = null

// Catalog and health come from the proxy. Never bake model ids into this UI.
let catalogLoaded = false
let catalogRefreshedAt = null
let catalogError = ''
let catalogFetchPending = false
let catalogMetadata = {}

const $ = id => document.getElementById(id)

/* ── Control-plane token ─────────────────────────
   The proxy hands it over in the URL on open. It goes into sessionStorage and
   out of the address bar immediately: leaving it there would put a live
   credential into the window title, the history, and any screenshot. */
const TOKEN_HEADER = 'X-OpenVidia-Token'

function readToken() {
  const fromUrl = new URLSearchParams(location.search).get('token')
  if (fromUrl) {
    sessionStorage.setItem('openvidia-token', fromUrl)
    history.replaceState(null, '', location.pathname)
    return fromUrl
  }
  return sessionStorage.getItem('openvidia-token') || ''
}

let controlToken = readToken()

/* ── API helper ──────────────────────────────── */
function serverMessage(data, fallback) {
  const value = data?.detail || data?.error
  if (typeof value === 'string') return value.slice(0, 240)
  if (value && typeof value.message === 'string') return value.message.slice(0, 240)
  return fallback
}

async function api(method, path, body) {
  const opts = { method, headers: {} }
  if (controlToken) opts.headers[TOKEN_HEADER] = controlToken
  if (body) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body) }
  const r = await fetch(path, opts)
  if (r.status === 401) {
    throw new Error('This dashboard has no control token. Reopen it from the OpenVidia app, or append ?token=<contents of ~/.config/openvidia/control_token>')
  }
  let data
  try { data = await r.json() } catch (_) { throw new Error(`HTTP ${r.status}: server returned an invalid response. Try again or check the service.`) }
  if (!r.ok) throw new Error(serverMessage(data, `HTTP ${r.status}: request failed`))
  if (data && data.ok === false) throw new Error(serverMessage(data, 'Request rejected'))
  return data
}

/* ── Toast ──────────────────────────────────── */
function toast(msg, level = 'info') {
  const icons = { ok: '✓', error: '✕', warn: '⚠', info: 'ℹ' }
  const el = document.createElement('div')
  el.className = 'toast'
  // msg carries server text verbatim — including upstream error bodies — so
  // it is set as text, never parsed as markup.
  const ic = document.createElement('span')
  ic.className = `toast-icon ${level}`; ic.textContent = icons[level] || icons.info
  const tx = document.createElement('span'); tx.textContent = msg
  el.appendChild(ic); el.appendChild(tx)
  $('toast-container').appendChild(el)
  setTimeout(() => { el.classList.add('toast-out'); setTimeout(() => el.remove(), 260) }, 3500)
}

/* ── Theme ──────────────────────────────────── */
function applyTheme(t) {
  theme = t
  document.documentElement.setAttribute('data-theme', t)
  $('themeToggle').textContent = t === 'dark' ? '🌙' : '☀️'
  localStorage.setItem('openvidia-theme', t)
}
$('themeToggle').addEventListener('click', () => applyTheme(theme === 'dark' ? 'light' : 'dark'))
applyTheme(theme)

/* ── Bento Dropdowns ────────────────────────── */
document.querySelectorAll('[data-toggle]').forEach(hd => {
  hd.addEventListener('click', () => hd.closest('[data-dropdown]').classList.toggle('open'))
})

/* ── Running State ──────────────────────────── */
function renderUpstream(up) {
  const badge = $('hUpstream')
  const dot = $('upDot')
  const txt = $('upText')
  if (!badge) return
  const st = (up && up.status) || 'unknown'
  const ms = up && up.latency_ms
  badge.className = `header-stat upstream-badge up-${st}`
  if (st === 'ok') {
    txt.textContent = ms != null ? `${Math.round(ms)}ms` : 'ok'
    badge.title = 'NVIDIA model metadata is reachable; inference availability must be checked separately'
  } else if (st === 'slow') {
    txt.textContent = ms != null ? `slow ${Math.round(ms)}ms` : 'slow'
    badge.title = `NVIDIA metadata is slow (${(up && up.detail) || 'high latency'}); this does not establish inference or key health`
  } else if (st === 'down') {
    txt.textContent = 'down'
    badge.title = `NVIDIA metadata is unreachable (${(up && up.detail) || 'no response'}); inference availability may differ`
  } else {
    txt.textContent = '…'
    badge.title = 'NVIDIA metadata reachability has not been measured yet'
  }
}

async function updateRunningState() {
  try {
    const st = await api('GET', '/api/status')
    const running = st.running
    $('statusText').textContent = running ? 'Running' : 'Stopped'
    $('statusDot').querySelector('.pulse-dot').className = `pulse-dot ${running ? 'running' : ''}`
    $('stopBtn').style.display = running ? '' : 'none'
    $('startBtn').style.display = running ? 'none' : ''
    $('hKeyCount').textContent = `${st.keys || 0} keys`
    renderUpstream(st.upstream)
  } catch (_) {
    $('statusText').textContent = 'Offline'
    $('statusDot').querySelector('.pulse-dot').className = 'pulse-dot'
  }
}

$('stopBtn').addEventListener('click', async () => { try { await api('POST', '/api/stop'); toast('Proxy stopped', 'warn') } catch (_) {} await updateRunningState() })
$('startBtn').addEventListener('click', async () => { try { await api('POST', '/api/start'); toast('Proxy started', 'ok') } catch (_) {} await updateRunningState() })

$('restartBtn').addEventListener('click', async () => {
  $('restartBtn').textContent = '⟳…'
  try { await api('POST', '/api/restart'); toast('Restarting…', 'info') } catch (_) { toast('Restart failed', 'error') }
  $('restartBtn').textContent = '⟳'
})

/* ── Stats polling ───────────────────────────── */
$('portDisplay').textContent = '1919'
setInterval(async () => {
  try {
    const s = await api('GET', '/api/stats')
    $('statReqs').textContent = s.requests
    $('statOk').textContent = s.success
    $('statRots').textContent = s.rotations
    const cd = s.cooldowns || 0
    $('statCooldowns').textContent = cd
    $('cdMini').classList.toggle('warn', cd > 0)
    $('hRpm').textContent = `${s.total_rpm || 0} RPM`
    await updateRunningState()
  } catch (_) {}
}, 2000)

/* ── Starred models (= presets = quick-pick shortlist) ────── */
async function loadPresets() {
  try { const r = await api('GET', '/api/presets'); presets = r.presets || [] } catch (_) { presets = [] }
  renderFilters(); renderModelList()
}
async function savePresets(next) {
  try {
    await api('POST', '/api/presets', { presets: next })
    presets = next
    renderFilters(); renderModelList()
    return true
  } catch (e) { toast(`Could not save favorites: ${e.message}`, 'error'); return false }
}

function shortLabel(id) {
  return id.split('/').pop()
}

async function setModel(id) {
  if (catalogLoaded && id && !allModels.some(m => m.id === id)) {
    toast('This model is absent from the current catalog. Refresh or choose another model.', 'error')
    return
  }
  try {
    const result = await api('POST', '/api/model', { model: id })
    activeModel = result.model ?? id
    renderCatalogStatus()
    renderModelList(); renderUsage()
    await Promise.all([loadThinking(), loadEffort(), loadFallback()])
    toast(`Model: ${activeModel || 'none'}`, 'ok')
  } catch (e) { toast(`Model change failed: ${e.message}`, 'error') }
}

async function loadModel() {
  try { const r = await api('GET', '/api/model'); activeModel = r.model || ''; renderCatalogStatus() } catch (_) {}
  loadThinking()
  loadEffort()
  loadFallback()
}

document.querySelectorAll('.think-btn').forEach(b => {
  b.addEventListener('click', () => setThinking(b.dataset.think))
})

/* ── Reasoning toggle ─────────────────────────
   A hybrid reasoning model withholds every byte until it finishes thinking.
   The switch is per active model and stored server-side, so all four CLIs
   pick it up without touching their own configs. */
let thinkingMode = 'auto'

function renderThinking() {
  document.querySelectorAll('.think-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.think === thinkingMode)
  })
}

async function loadThinking() {
  try {
    const r = await api('GET', '/api/thinking')
    thinkingMode = r.mode || 'auto'
    renderThinking()
  } catch (_) {}
}

async function setThinking(mode) {
  try {
    const result = await api('POST', '/api/thinking', { mode })
    thinkingMode = result.mode ?? mode
    renderThinking()
    toast(`Thinking: ${thinkingMode}${activeModel ? ' — ' + activeModel : ''}`, 'ok')
  } catch (e) { toast(`Thinking change failed: ${e.message}`, 'error') }
}

/* ── Reasoning effort ──────────────────────────
   Finer granularity than thinking on/off:
   low (fast), medium (balanced), high (deep). */
let reasoningEffort = 'auto'

document.querySelectorAll('.effort-btn').forEach(b => {
  b.addEventListener('click', () => setEffort(b.dataset.effort))
})

function renderEffort() {
  document.querySelectorAll('.effort-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.effort === reasoningEffort)
  })
}

async function loadEffort() {
  try {
    const r = await api('GET', '/api/reasoning-effort')
    reasoningEffort = r.effort || 'auto'
    renderEffort()
  } catch (_) {}
}

async function setEffort(effort) {
  try {
    const result = await api('POST', '/api/reasoning-effort', { effort })
    reasoningEffort = result.effort ?? effort
    renderEffort()
    toast(`Effort: ${reasoningEffort}${activeModel ? ' — ' + activeModel : ''}`, 'ok')
  } catch (e) { toast(`Effort change failed: ${e.message}`, 'error') }
}

/* ── Fallback toggle ───────────────────────────
   When a model's circuit is open, the proxy can either failover to the
   next healthy preset or return 503. off = never failover, on = always. */
let fallbackMode = 'off'

document.querySelectorAll('.fallback-btn').forEach(b => {
  b.addEventListener('click', () => setFallback(b.dataset.fallback))
})

function renderFallback() {
  document.querySelectorAll('.fallback-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.fallback === fallbackMode)
  })
}

async function loadFallback() {
  try {
    const r = await api('GET', '/api/fallback')
    fallbackMode = r.fallback || 'off'
    renderFallback()
  } catch (_) {}
}

async function setFallback(mode) {
  try {
    await api('POST', '/api/fallback', { fallback: mode })
    fallbackMode = mode
    renderFallback()
    toast(`Fallback: ${mode}${activeModel ? ' — ' + activeModel : ''}`, 'ok')
  } catch (e) { toast(`Fallback change failed: ${e.message}`, 'error') }
}

/* ── Models browser ──────────────────────────── */
const FILTERS = ['tested', 'recent', 'starred', 'all']

function validationState(model) {
  const evidence = model.validation
  if (!evidence || typeof evidence !== 'object') return { eligible: false, kind: '', label: 'Tools not tested', detail: 'Use Check tools for streaming and a tool-result round trip.' }
  const tested = Date.parse(evidence.tested_at)
  const expires = Date.parse(evidence.expires_at)
  const now = Date.now()
  const fresh = Number.isFinite(tested) && Number.isFinite(expires) && tested <= now && expires > now && expires > tested
  const checksPassed = evidence.stream === true && evidence.tool_call === true && evidence.tool_result === true
  const unhealthy = model.health?.circuit_open || model.health?.consecutive_failures > 0
  const recorded = Number.isFinite(tested) ? `Checked ${new Date(tested).toLocaleString()}` : 'Check time unavailable'
  const reason = typeof evidence.reason === 'string' ? evidence.reason.replaceAll('_', ' ') : ''
  if (evidence.status === 'failed') return { eligible: false, kind: 'failed', label: 'Latest tool check failed', detail: `${recorded}${reason ? ' — ' + reason : ''}. Run Check tools again.` }
  if (evidence.status !== 'passed' || !checksPassed) return { eligible: false, kind: 'warn', label: 'Tool check incomplete', detail: `${recorded}. Streaming, tool call and tool result must all pass.` }
  if (!fresh) return { eligible: false, kind: 'warn', label: 'Tool check expired', detail: `${recorded}. Run Check tools to refresh the evidence.` }
  if (unhealthy) return { eligible: false, kind: 'failed', label: 'Currently failing', detail: `${recorded}; recent traffic reports failures. Run Check tools before relying on this model.` }
  return { eligible: true, kind: 'passed', label: 'Tools passed', detail: `${recorded}; valid until ${new Date(expires).toLocaleString()}. Streaming, tool call and tool result passed; this is not a coding-quality benchmark.`, opencode: evidence.opencode === true }
}
function getFilteredModels() {
  let list
  if (modelFilter === 'tested') {
    list = allModels.filter(m => m.id !== 'openvidia' && validationState(m).eligible)
      .sort((a, b) => Date.parse(b.validation.tested_at) - Date.parse(a.validation.tested_at) || a.id.localeCompare(b.id))
  } else if (modelFilter === 'recent') {
    // Official update dates measure recency, never relative model strength.
    list = allModels.filter(m => m.id !== 'openvidia' && m.catalog?.endpoint_kind === 'free_chat' && Number.isFinite(Date.parse(m.catalog.updated_at)))
      .sort((a, b) => Date.parse(b.catalog.updated_at) - Date.parse(a.catalog.updated_at) || a.id.localeCompare(b.id))
  } else if (modelFilter === 'starred') {
    // in the order they were starred
    list = presets.map(id => allModels.find(m => m.id === id) || { id, owned_by: '', unavailable: catalogLoaded })
  } else {
    list = [...allModels]
  }
  const q = ($('modelSearch').value || '').toLowerCase()
  if (q) list = list.filter(m => [m.id, m.catalog?.display_name || '', ...(Array.isArray(m.catalog?.use_cases) ? m.catalog.use_cases : [])].some(value => String(value).toLowerCase().includes(q)))
  // Keep Recent strictly ordered by official date; the header shows the active model.
  if (activeModel && modelFilter !== 'recent') {
    const i = list.findIndex(m => m.id === activeModel)
    if (i > 0) { const [a] = list.splice(i, 1); list.unshift(a) }
  }
  return list
}

function renderFilters() {
  const c = $('modelFilters'); c.innerHTML = ''
  FILTERS.forEach(f => {
    const b = document.createElement('button')
    b.className = `browser-filter ${f === modelFilter ? 'active' : ''}`
    if (f === 'tested') { b.textContent = 'Tested tools'; b.title = 'Fresh successful streaming, tool-call and tool-result checks; not a model-strength ranking' }
    else if (f === 'recent') { b.textContent = 'Recent'; b.title = 'Recent free chat API models from NVIDIA Build, ordered by official update date; recency is not a quality ranking' }
    else if (f === 'starred') b.innerHTML = `★ Favorites <span class="filter-count">${presets.length}</span>`
    else b.textContent = 'All'
    b.onclick = () => { modelFilter = f; renderFilters(); renderModelList(); renderCatalogStatus() }
    c.appendChild(b)
  })
}

function renderCatalogStatus() {
  const absent = catalogLoaded && activeModel && !allModels.some(m => m.id === activeModel)
  $('activeModelDisplay').textContent = (activeModel || 'none') + (absent ? ' — unavailable in catalog' : '')
  const status = $('catalogStatus')
  status.className = `catalog-status${catalogError || absent ? ' warn' : ''}`
  const freshness = catalogRefreshedAt ? `Catalog refreshed ${catalogRefreshedAt.toLocaleTimeString()}` : 'Catalog not loaded'
  status.textContent = catalogError
    ? `${freshness} — refresh failed; showing the last successful catalog. ${catalogError}`
    : `${freshness}${absent ? ' — choose another active model; it is no longer listed.' : ''}`
  const count = allModels.filter(m => m.id !== 'openvidia' && m.catalog?.endpoint_kind === 'free_chat' && Number.isFinite(Date.parse(m.catalog.updated_at))).length
  const officialTime = Date.parse(catalogMetadata.refreshed_at)
  $('catalogInfo').textContent = modelFilter === 'tested'
    ? `${allModels.filter(m => m.id !== 'openvidia' && validationState(m).eligible).length} models have fresh tool checks. OpenCode has a separate client-test badge. Use Check tools in Recent or All. No quality ranking.`
    : count
    ? `Recent: ${count} free chat API models from NVIDIA Build’s latest page. Ordered by update date, not benchmark strength.${Number.isFinite(officialTime) ? ' Metadata checked ' + new Date(officialTime).toLocaleString() + '.' : ''}${catalogMetadata.stale ? ' Metadata is stale; showing the last successful refresh.' : ''} All contains the full API catalog.`
    : 'Recent metadata is unavailable. Open All for the complete API catalog; previews without API access are not selectable.'
  $('catalogInfo').className = `catalog-status${catalogMetadata.stale || !count ? ' warn' : ''}`
}

async function fetchModels() {
  if (catalogFetchPending) return
  catalogFetchPending = true
  const controller = new AbortController()
  const deadline = setTimeout(() => controller.abort(), 15000)
  try {
    const r = await fetch('/v1/models', { signal: controller.signal })
    if (!r.ok) throw new Error(`HTTP ${r.status}`)
    const d = await r.json()
    const models = d.data || d.models
    if (!Array.isArray(models) || models.some(m => !m || typeof m.id !== 'string')) throw new Error('Invalid model catalog')
    allModels = models.map(m => ({ ...m, owned_by: m.owned_by || '', health: m.health || null }))
    catalogMetadata = d.catalog && typeof d.catalog === 'object' ? d.catalog : {}
    catalogLoaded = true
    catalogRefreshedAt = new Date()
    catalogError = ''
    await loadModel()
    renderFilters(); renderModelList(); renderUsage()
  } catch (e) {
    catalogError = e.name === 'AbortError' ? 'Request timed out' : e.message
    if (!catalogLoaded) $('modelList').textContent = 'Catalog unavailable. Retrying automatically.'
  } finally {
    clearTimeout(deadline)
    catalogFetchPending = false
  }
  renderCatalogStatus()
}
$('refreshModelsBtn').addEventListener('click', fetchModels)
// Refresh the catalog every 60s: NVIDIA removes/renames models without notice,
// and the list has to follow along without a page reload.
setInterval(fetchModels, 60000)

async function testModel(id, btn) {
  btn.className = 'browser-test testing'; btn.textContent = '…'
  const r = document.createElement('div'); r.className = 'test-result'
  btn.parentElement.parentElement.after(r)
  try {
    const res = await api('POST', '/api/test-model', { model: id })
    if (res.ok) { r.className = 'test-result ok'; r.textContent = `✓ ${String(res.response || '').slice(0, 80)}`; btn.className = 'browser-test done'; btn.textContent = '✓' }
    else { r.className = 'test-result fail'; r.textContent = `✗ ${serverMessage(res, 'Availability check failed')}`.slice(0, 120); btn.className = 'browser-test fail'; btn.textContent = '✗' }
  } catch (e) { r.className = 'test-result fail'; r.textContent = `✗ ${e.message.slice(0, 80)}`; btn.className = 'browser-test fail'; btn.textContent = '✗' }
  setTimeout(() => r.remove(), 8000)
  setTimeout(() => { btn.className = 'browser-test'; btn.textContent = '▶' }, 5000)
}

async function validateModel(id, button) {
  button.disabled = true
  button.textContent = 'Checking…'
  try {
    const result = await api('POST', '/api/validate-model', { model: id })
    if (!result.validation || typeof result.validation !== 'object') throw new Error('Server did not return tool-check evidence')
    const model = allModels.find(m => m.id === id)
    if (model && result.validation) model.validation = result.validation
    const state = validationState(model || { validation: result.validation })
    toast(state.eligible ? `Tools passed: ${id}` : `${state.label}: ${id}`, state.eligible ? 'ok' : 'warn')
    renderModelList(); renderCatalogStatus()
  } catch (e) { toast(`Tool check failed: ${e.message}`, 'error') }
  finally {
    button.disabled = false
    button.textContent = 'Check tools'
    await fetchModels()
  }
}

function renderModelList() {
  const f = getFilteredModels()
  $('modelCount').textContent = `${f.length} / ${allModels.length}`
  if (!f.length) {
    const hint = modelFilter === 'tested'
      ? 'No fresh passing tool checks. Open Recent or All and click Check tools. An availability response alone does not qualify.'
      : modelFilter === 'recent'
      ? 'No recent API models with official metadata. Open All to browse the complete catalog.'
      : modelFilter === 'starred'
      ? 'No starred models — tap ☆ on any model in All'
      : 'No models found'
    $('modelList').innerHTML = `<div class="browser-empty">${hint}</div>`
    return
  }
  $('modelList').innerHTML = ''
  f.forEach(m => {
    const item = document.createElement('div')
    item.className = `browser-item ${m.id === activeModel ? 'active' : ''}${m.unavailable ? ' unavailable' : ''}`
    // m.id / m.owned_by / m.health come straight from the upstream /v1/models
    // response (enriched by the proxy with ModelHealth). Remote data never
    // becomes markup.
    const info = document.createElement('div'); info.className = 'browser-info'
    const officialName = typeof m.catalog?.display_name === 'string' ? m.catalog.display_name : ''
    const nm = document.createElement('span'); nm.className = 'browser-name'; nm.textContent = officialName || m.id
    info.appendChild(nm)
    if (officialName && officialName !== m.id) {
      const id = document.createElement('span'); id.className = 'browser-id'; id.textContent = m.id
      info.appendChild(id)
    }
    const updated = Date.parse(m.catalog?.updated_at)
    if (Number.isFinite(updated)) {
      const date = document.createElement('span'); date.className = 'browser-date'
      date.textContent = `Updated ${new Date(updated).toISOString().slice(0, 10)}`
      date.title = 'Official NVIDIA Build update date; not a benchmark or release-date guarantee'
      info.appendChild(date)
    }
    if (Array.isArray(m.catalog?.use_cases) && m.catalog.use_cases.length) {
      const tags = document.createElement('span'); tags.className = 'browser-use-cases'
      tags.textContent = m.catalog.use_cases.slice(0, 3).join(' · ')
      tags.title = m.catalog.use_cases.join(' · ')
      info.appendChild(tags)
    }
    const evidence = validationState(m)
    const result = document.createElement('span'); result.className = `validation-badge ${evidence.kind}`
    const checkedAt = Date.parse(m.validation?.tested_at)
    const expiresAt = Date.parse(m.validation?.expires_at)
    result.textContent = evidence.label + (Number.isFinite(checkedAt) ? ` · checked ${new Date(checkedAt).toLocaleTimeString()}` : '')
    if (evidence.eligible && Number.isFinite(expiresAt)) result.textContent += ` · expires ${new Date(expiresAt).toLocaleTimeString()}`
    result.title = evidence.detail
    info.appendChild(result)
    if (evidence.opencode) {
      const client = document.createElement('span'); client.className = 'validation-badge passed'
      client.textContent = 'OpenCode client passed'; client.title = evidence.detail
      info.appendChild(client)
    }
    item.appendChild(info)
    if (m.unavailable) {
      const badge = document.createElement('span'); badge.className = 'health-badge health-bad'; badge.textContent = 'not listed'; item.appendChild(badge)
      item.title = 'Absent from the current catalog. Remove this favorite or wait for a refresh.'
    }
    // Health badge: open circuit (model down) or high failure rate
    // (model degraded) → the problem is NVIDIA, not the keys.
    if (m.health) {
      const h = m.health
      let cls = '', label = ''
      if (h.circuit_open) { cls = 'health-bad'; label = 'down' }
      else if (h.consecutive_failures > 0) { cls = 'health-bad'; label = 'failing' }
      else if (h.median_ttft > 5 && h.requests >= 4) { cls = 'health-slow'; label = `slow ${Math.round(h.median_ttft)}s` }
      if (cls) {
        const hb = document.createElement('span')
        hb.className = `health-badge ${cls}`; hb.textContent = label
        hb.title = `${h.requests} req · ${Math.round(h.failure_rate * 100)}% fail · median ttft ${Math.round(h.median_ttft)}s — upstream NVIDIA, not your keys`
        item.appendChild(hb)
      }
    }
    if (m.owned_by) {
      const ow = document.createElement('span'); ow.className = 'browser-owner'; ow.textContent = m.owned_by
      item.appendChild(ow)
    }
    const acts = document.createElement('div'); acts.className = 'browser-acts'
    const test = document.createElement('button'); test.className = 'browser-test'; test.textContent = '▶'; test.title = 'Check availability only — this does not certify tools or OpenCode compatibility'
    test.disabled = Boolean(m.unavailable)
    test.onclick = e => { e.stopPropagation(); testModel(m.id, test) }
    acts.appendChild(test)
    const validate = document.createElement('button'); validate.className = 'browser-validate'; validate.textContent = 'Check tools'
    validate.title = 'Run streaming, tool-call and tool-result checks; this uses provider quota and does not benchmark coding quality'
    validate.disabled = Boolean(m.unavailable)
    validate.onclick = e => { e.stopPropagation(); validateModel(m.id, validate) }
    acts.appendChild(validate)
    const starred = presets.includes(m.id)
    const star = document.createElement('button')
    star.className = `browser-star ${starred ? 'on' : ''}`
    star.textContent = starred ? '★' : '☆'
    star.title = starred ? 'Unstar — remove from shortlist' : 'Star — add to shortlist'
    star.onclick = async e => {
      e.stopPropagation()
      const next = starred ? presets.filter(x => x !== m.id) : [...presets, m.id]
      if (await savePresets(next)) toast(`${starred ? 'Unstarred' : 'Starred'} ${shortLabel(m.id)}`, 'ok')
    }
    acts.appendChild(star)
    item.appendChild(acts)
    if (!m.unavailable) item.onclick = () => setModel(m.id)
    $('modelList').appendChild(item)
  })
}
$('modelSearch').addEventListener('input', renderModelList)

/* ── Key management: CRUD + filters ──────────── */
function copyToClipboard(t) { navigator.clipboard.writeText(t).then(() => toast('Key copied', 'ok')).catch(() => {}) }

/* Keys live server-side. The page only ever holds masked strings, so copying
   asks for the one key it needs, when the user asks for it. */
async function copyKey(i) {
  try {
    const d = await api('POST', '/api/keys/reveal', { index: i })
    if (d.ok) copyToClipboard(d.key); else toast(d.error || 'Reveal failed', 'error')
  } catch (e) { toast(`Reveal failed: ${e.message}`, 'error') }
}

async function removeKey(i) {
  try {
    const d = await api('POST', '/api/keys/remove', { index: i })
    if (!d.ok) return toast(d.error || 'Remove failed', 'error')
    keys = d.keys || []
    toast('Key removed', 'warn')
    renderKeys(await api('GET', '/api/keys/stats').catch(() => null))
  } catch (e) { toast(`Remove failed: ${e.message}`, 'error') }
}
function timeAgo(ts) { if (!ts) return ''; const s = Math.floor(Date.now() / 1000 - ts); if (s < 60) return `${s}s`; if (s < 3600) return `${Math.floor(s / 60)}m`; return `${Math.floor(s / 3600)}h` }

function keyStatus(k, i, stats) {
  const s = stats ? stats[String(i)] : null
  if (s && s.cooldown > 0) return 'cooldown'
  if (s && s.success > 0) return 'fresh'
  return 'idle'
}

function updateFilterCounts(stats) {
  if (!stats) return
  const sm = stats.key_stats || {}
  let active = 0, cooldown = 0, fresh = 0, idle = 0
  keys.forEach((k, i) => {
    const st = keyStatus(k, i, sm)
    if (st === 'cooldown') cooldown++
    else if (st === 'fresh') fresh++
    else idle++
    if (i === stats.active_index) active++
  })
  $('fAll').textContent = keys.length
  $('fActive').textContent = active
  $('fCooldown').textContent = cooldown
  $('fFresh').textContent = fresh
  $('fIdle').textContent = idle
}

function renderKeys(data) {
  _lastKeyStats = data
  const list = $('keysList')
  list.innerHTML = ''
  $('keyCount').textContent = String(keys.length)
  $('keysEmpty').classList.toggle('hidden', keys.length > 0)
  const ai = data ? data.active_index : -1
  const sm = data ? data.key_stats || {} : {}
  updateFilterCounts(data)
  keys.forEach((k, i) => {
    const s = sm[String(i)]
    const status = keyStatus(k, i, sm)
    if (keyFilter !== 'all' && keyFilter !== status && !(keyFilter === 'active' && i === ai)) return
    const isCd = s && s.cooldown > 0
    const row = document.createElement('div')
    row.className = `key-row ${i === ai ? 'active' : ''}`
    const dot = document.createElement('span')
    dot.className = `key-dot key-${status}`
    row.appendChild(dot)
    const val = document.createElement('span')
    val.className = 'key-val'; val.textContent = k; val.title = 'Click to copy'
    val.onclick = () => copyKey(i)
    row.appendChild(val)
    const acts = document.createElement('div'); acts.className = 'key-acts'
    if (i === ai) { const b = document.createElement('span'); b.className = `key-badge ${isCd ? 'cd' : ''}`; b.textContent = isCd ? '⏳' : 'active'; acts.appendChild(b) }
    const info = document.createElement('span'); info.className = 'key-info'
    if (isCd) { info.textContent = `⏳ ${Math.ceil(s.cooldown)}s`; info.title = s.cooldown_reason || '' }
    else if (s && s.requests > 0) { info.textContent = `${s.success}✓${s.failed > 0 ? ' ' + s.failed + '✗' : ''} ${timeAgo(s.last_used)}`; info.title = s.last_error || '' }
    else { info.textContent = 'idle' }
    acts.appendChild(info)
    const cp = document.createElement('button'); cp.className = 'key-act'; cp.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'; cp.title = 'Copy'; cp.onclick = () => copyKey(i)
    acts.appendChild(cp)
    const del = document.createElement('button'); del.className = 'key-act danger'; del.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>'; del.title = 'Remove'; del.onclick = () => removeKey(i)
    acts.appendChild(del)
    row.appendChild(acts)
    list.appendChild(row)
  })
}

async function pollKeyStats() {
  try {
    const data = await api('GET', '/api/keys/stats')
    renderKeys(data)
  } catch (_) {}
}


/* ── Key filter buttons ──────────────────────── */
document.querySelectorAll('.key-filter').forEach(btn => {
  btn.addEventListener('click', () => {
    keyFilter = btn.dataset.filter
    document.querySelectorAll('.key-filter').forEach(b => b.classList.toggle('active', b === btn))
    renderKeys(_lastKeyStats)
  })
})

/* ── Add key form ────────────────────────────── */
$('addKeyBtn').addEventListener('click', () => { $('keyAddForm').classList.remove('hidden'); $('newKeyInput').value = ''; $('newKeyInput').focus() })
$('cancelAddKeyBtn').addEventListener('click', () => $('keyAddForm').classList.add('hidden'))
$('confirmAddKeyBtn').addEventListener('click', async () => {
  const v = $('newKeyInput').value.trim()
  if (!v) return toast('Enter a key', 'warn')
  try {
    // The pool is server-side, so duplicate detection lives there too — the
    // page cannot compare a cleartext key against its masked list.
    const d = await api('POST', '/api/keys/add', { key: v })
    if (!d.ok) return toast(d.error || 'Add failed', 'warn')
    keys = d.keys || []
    $('keyAddForm').classList.add('hidden')
    toast('Key added', 'ok')
    renderKeys(await api('GET', '/api/keys/stats').catch(() => null))
  } catch (e) { toast(`Add failed: ${e.message}`, 'error') }
})
$('newKeyInput').addEventListener('keydown', e => { if (e.key === 'Enter') $('confirmAddKeyBtn').click() })

/* ── Log ────────────────────────────────────── */
$('clearLogBtn').addEventListener('click', () => { $('logArea').innerHTML = '<div class="log-empty">Cleared</div>'; _logEntries = 0; $('logCount').textContent = '0' })

function appendLog(level, msg, timestamp) {
  const occurred = typeof timestamp === 'number' && Number.isFinite(timestamp) ? new Date(timestamp * 1000) : null
  const knownTime = occurred && Number.isFinite(occurred.getTime())
  const t = knownTime ? occurred.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : 'unknown'
  const el = document.createElement('div')
  el.className = `log-entry ${level}`
  const time = document.createElement('span'); time.className = 'log-time'; time.textContent = t; time.title = knownTime ? occurred.toISOString() : 'Event timestamp unavailable'
  const text = document.createElement('span'); text.className = 'log-msg'; text.textContent = msg
  el.appendChild(time); el.appendChild(text)
  const e = $('logArea').querySelector('.log-empty')
  if (e) e.remove()
  $('logArea').appendChild(el)
  $('logArea').scrollTop = $('logArea').scrollHeight
  _logEntries++; $('logCount').textContent = _logEntries
  $('logDot').className = 'log-dot active'
  clearTimeout($('logDot')._timer)
  $('logDot')._timer = setTimeout(() => { $('logDot').className = 'log-dot' }, 2000)
}

// EventSource cannot set request headers, which is the reason the middleware
// accepts ?token= as well as the header.
new EventSource(`/api/logs/stream?token=${encodeURIComponent(controlToken)}`).onmessage = e => {
  try {
    const { msg, timestamp } = JSON.parse(e.data)
    if (typeof msg !== 'string') return
    let level = 'info'
    if (/cooldown|429|rate.limit|⏳/.test(msg)) level = 'cooldown'
    else if (/error|Error|✗|failed/.test(msg)) level = 'error'
    else if (/OK|✔|✓|revived/.test(msg)) level = 'ok'
    appendLog(level, msg, timestamp)
  } catch (_) {}
}

/* ── CLI Setup tab (opencode / Codex / Claude Code / Grok / Jcode) ──────── */
let currentCli = 'opencode'
const CLI_TABS = [
  { id: 'opencode', label: 'opencode', icon: '⚡' },
  { id: 'codex',    label: 'Codex',    icon: '⬡' },
  { id: 'claude',   label: 'Claude',   icon: '✦' },
  { id: 'grok',     label: 'Grok',     icon: '✸' },
  { id: 'jcode',    label: 'Jcode',    icon: '◆' },
]

function renderCliTabs() {
  const c = $('cliTabs')
  if (!c) return
  c.innerHTML = ''
  CLI_TABS.forEach(t => {
    const b = document.createElement('button')
    b.className = `cli-tab ${t.id === currentCli ? 'active' : ''}`
    b.innerHTML = `<span class="cli-tab-icon">${t.icon}</span>${t.label}`
    b.onclick = () => { currentCli = t.id; renderCliTabs(); renderUsage() }
    c.appendChild(b)
  })
}

function renderUsage() {
  const m = activeModel || 'openvidia'
  const examples = {
    opencode: {
      title: 'opencode',
      steps: [
        { label: 'Auto-setup (opencode is the one CLI OpenVidia configures)', code: `openvidia setup` },
        { label: 'Manual — ~/.config/opencode/opencode.json', code: `{
  "provider": {
    "openvidia": {
      "npm": "@ai-sdk/openai-compatible",
      "options": { "apiKey": "ignored", "baseURL": "http://localhost:1919/v1" },
      "models": { "${m}": { "name": "OpenVidia", "tools": true } }
    }
  },
  "model": "openvidia/${m}",
  "compaction": { "auto": true, "prune": true, "reserved": 8000 }
}` },
        { label: 'Run', code: `opencode` },
      ],
    },
    codex: {
      title: 'Codex CLI',
      steps: [
        { label: 'Run against the proxy (nothing is written)', code: `openvidia run codex` },
        { label: 'If Codex needs its own provider block — ~/.codex/config.toml', code: `model = "${m}"
model_provider = "openvidia"

[model_providers.openvidia]
name = "OpenVidia"
base_url = "http://localhost:1919/v1"
env_key = "OPENVIDIA_API_KEY"
wire_api = "responses"

[model_providers.openvidia.models.${m}]
name = "OpenVidia (NVIDIA NIM via proxy)"
context_window = 128000
supports_tool_use = true
supports_parallel_tool_use = true
cost_input_tokens = 0
cost_output_tokens = 0` },
        { label: 'Env', code: `export OPENVIDIA_API_KEY=ignored` },
        { label: 'Run', code: `codex --model ${m}` },
      ],
    },
    claude: {
      title: 'Claude Code',
      steps: [
        { label: 'Run against the proxy (nothing is written)', code: `openvidia run claude` },
        { label: 'With arguments', code: `openvidia run claude -p "explain this file"` },
        { label: 'Equivalent by hand — one shell, no config edits', code: `ANTHROPIC_BASE_URL=http://localhost:1919 \\
  ANTHROPIC_API_KEY=ignored \\
  claude --model ${m}` },
      ],
    },
    grok: {
      title: 'Grok (xAI)',
      steps: [
        { label: 'Run against the proxy (nothing is written)', code: `openvidia run grok` },
        { label: 'If Grok needs its own provider block — ~/.grok/config.toml', code: `[models]
default = "${m}"

[model.${m}]
api_key = "ignored"
base_url = "http://localhost:1919/v1"
api_backend = "chat_completions"
context_window = 128000` },
        { label: 'Run', code: `grok -m ${m} "explain this codebase"` },
      ],
    },
    jcode: {
      title: 'Jcode',
      steps: [
        { label: 'Auto-setup (configures ~/.jcode/config.toml)', code: `openvidia setup` },
        { label: 'Manual — ~/.jcode/config.toml', code: `[provider]
default_provider = "openvidia"
default_model = "${m}"

[providers.openvidia]
type = "openai-compatible"
base_url = "http://localhost:1919/v1"
auth = "none"
default_model = "${m}"
requires_api_key = false
model_catalog = true
stream_idle_timeout_secs = 300

[[providers.openvidia.models]]
id = "${m}"
context_window = 202752` },
        { label: 'Run', code: `jcode` },
        { label: 'Switch model inside Jcode', code: `/model openvidia` },
      ],
    },
  }

  const ex = examples[currentCli]
  if (!ex) return
  const container = $('usageCode') || $('cliSetup')
  if (!container) return
  // s.code interpolates activeModel, which is server-persisted state. Building
  // this as an HTML string would make a stored model name executable markup,
  // so the whole block is assembled from nodes and text.
  container.innerHTML = ''
  const wrap = document.createElement('div'); wrap.className = 'cli-setup-content'
  const h = document.createElement('h4'); h.textContent = ex.title
  wrap.appendChild(h)
  ex.steps.forEach((s, i) => {
    const step = document.createElement('div'); step.className = 'cli-step'
    const lab = document.createElement('div'); lab.className = 'cli-step-label'
    lab.textContent = `${i + 1}. ${s.label}`
    const pre = document.createElement('pre'); pre.className = 'cli-step-code'
    const code = document.createElement('code'); code.textContent = s.code
    pre.appendChild(code)
    step.appendChild(lab); step.appendChild(pre)
    wrap.appendChild(step)
  })
  container.appendChild(wrap)
}

/* ── Init ───────────────────────────────────── */
;(async () => {
  try {
    const d = await api('GET', '/api/keys'); keys = d.keys || []
    await loadModel()
    await loadPresets()
    await fetchModels()
    // Render with real stats: renderKeys(undefined) leaves active_index at -1,
    // and the default 'active' filter then matches nothing — the list showed
    // empty for the first two seconds while claiming N keys were loaded.
    renderKeys(await api('GET', '/api/keys/stats').catch(() => null))
    if (keys.length) toast(`${keys.length} keys loaded`, 'ok')
  } catch (_) {}
  await updateRunningState()
  renderCliTabs()
  renderUsage()
  setInterval(pollKeyStats, 2000)
})()
