// Shared API client + job-watching helpers, ported from the legacy webui so the
// Vue app talks to the same Python backend (/api/*) identically.

export const getToken = () => localStorage.getItem('sceneforge_token') || localStorage.getItem('vimax_token') || ''
export const setToken = (t) => {
  localStorage.setItem('sceneforge_token', t)
  localStorage.removeItem('vimax_token')
}

// media (<img>/<a>/<video>) can't send headers; carry the token as a query param
export const mediaUrl = (p) => {
  const t = getToken()
  if (!t) return p
  return p + (p.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(t)
}

export const api = async (m, p, b) => {
  const headers = { 'Content-Type': 'application/json' }
  const tok = getToken()
  if (tok) headers['Authorization'] = 'Bearer ' + tok
  const r = await fetch(p, { method: m, headers, body: b ? JSON.stringify(b) : undefined })
  if (r.status === 401) {
    const nt = prompt('需要访问令牌（管理员配置的 SCENEFORGE_WEB_TOKEN）：', '')
    if (nt) { setToken(nt); location.reload() }
    throw new Error('未授权')
  }
  const t = await r.text()
  let j
  try { j = JSON.parse(t) } catch { j = { raw: t } }
  if (!r.ok) { const err = new Error(j.error || ('HTTP ' + r.status)); err.status = r.status; err.body = j; throw err }
  return j
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// read a File -> base64 (without the data: prefix), for *_upload endpoints
export const fileToB64 = (f) => new Promise((res, rej) => {
  const r = new FileReader()
  r.onload = () => res(String(r.result).split(',')[1])
  r.onerror = rej
  r.readAsDataURL(f)
})

// poll a background job until it finishes; onTick(progressArray) each tick
export async function pollJob(jobId, onTick) {
  for (;;) {
    const j = await api('GET', '/api/production/jobs/' + jobId)
    if (j.state && j.state !== 'running') return j
    if (onTick) onTick(j.progress || [])
    await sleep(2000)
  }
}

// watch a job via SSE (near-real-time push); fall back to polling if the browser
// can't keep an EventSource open. Resolves with the final job snapshot.
export function watchJob(jobId, onTick) {
  if (typeof EventSource === 'undefined') return pollJob(jobId, onTick)
  return new Promise((resolve) => {
    let settled = false, es
    const finish = (job) => { if (settled) return; settled = true; try { es && es.close() } catch (e) {} resolve(job) }
    const fallback = () => { if (settled) return; settled = true; try { es && es.close() } catch (e) {} pollJob(jobId, onTick).then(resolve) }
    try { es = new EventSource(mediaUrl('/api/production/jobs/' + jobId + '/stream')) }
    catch (e) { fallback(); return }
    const handle = (ev, isFinal) => {
      let snap
      try { snap = JSON.parse(ev.data) } catch (e) { return }
      if (onTick) onTick(snap.progress || [])
      if (isFinal || (snap.state && snap.state !== 'running')) finish(snap)
    }
    es.onmessage = (ev) => handle(ev, false)
    es.addEventListener('done', (ev) => handle(ev, true))
    es.onerror = () => { if (!settled && es.readyState === 2) fallback() }
  })
}
