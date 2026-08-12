import assert from 'node:assert/strict'
import test from 'node:test'

const values = new Map()
globalThis.localStorage = {
  getItem: (key) => values.get(key) || null,
  setItem: (key, value) => values.set(key, String(value)),
  removeItem: (key) => values.delete(key),
}

const { api, getToken, mediaUrl, setToken } = await import('../src/lib/api.js')

test.beforeEach(() => {
  values.clear()
})

test('token migration and media URLs are deterministic', () => {
  values.set('vimax_token', 'legacy')
  assert.equal(getToken(), 'legacy')
  setToken('current')
  assert.equal(getToken(), 'current')
  assert.equal(values.has('vimax_token'), false)
  assert.equal(mediaUrl('/api/media'), '/api/media?token=current')
  assert.equal(mediaUrl('/api/media?v=1'), '/api/media?v=1&token=current')
})

test('api sends bearer token and JSON body', async () => {
  setToken('secret')
  let request
  globalThis.fetch = async (path, options) => {
    request = { path, options }
    return { status: 200, ok: true, text: async () => '{"ok":true}' }
  }

  const result = await api('POST', '/api/test', { value: 1 })

  assert.deepEqual(result, { ok: true })
  assert.equal(request.path, '/api/test')
  assert.equal(request.options.headers.Authorization, 'Bearer secret')
  assert.equal(request.options.headers['Content-Type'], 'application/json')
  assert.equal(request.options.body, '{"value":1}')
})

test('api exposes structured server errors', async () => {
  globalThis.fetch = async () => ({
    status: 413,
    ok: false,
    text: async () => '{"error":"too large"}',
  })

  await assert.rejects(
    api('POST', '/api/upload', { data: 'x' }),
    (error) => error.message === 'too large' && error.status === 413 && error.body.error === 'too large',
  )
})
