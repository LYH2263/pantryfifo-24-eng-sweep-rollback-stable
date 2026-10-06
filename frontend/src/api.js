export class ApiError extends Error {
  constructor(message, status, body) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

export async function api(path, opts = {}) {
  const r = await fetch('/api' + path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!r.ok) {
    let detail = r.statusText
    let body = null
    try { body = await r.json(); detail = body.detail || JSON.stringify(body) } catch {}
    throw new ApiError(typeof detail === 'string' ? detail : JSON.stringify(detail), r.status, body)
  }
  if (r.status === 204) return null
  return r.json()
}
