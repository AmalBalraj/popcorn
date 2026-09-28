/* One way in and out of the API.
 *
 * Failures are normalised into ApiError so every screen can render the same
 * kind of message, and `detail` is preserved for the diagnostics disclosure
 * rather than being shown as the primary text.
 */

export class ApiError extends Error {
  constructor(message, { status = 0, detail = '' } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail || '';
  }

  get offline() { return this.status === 0; }
  get notFound() { return this.status === 404; }
  get serverDown() { return this.status === 502 || this.status === 503 || this.status === 504; }
}

let onUnauthorized = null;
export function setUnauthorizedHandler(handler) { onUnauthorized = handler; }

export async function api(url, { method = 'GET', body, signal, headers = {}, timeout } = {}) {
  const options = {
    method,
    credentials: 'same-origin',
    headers: { ...headers },
    signal,
  };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }

  // A hung request should surface as a retryable failure, not a dead spinner.
  let timer;
  if (timeout) {
    const controller = new AbortController();
    timer = setTimeout(() => controller.abort(), timeout);
    options.signal = controller.signal;
  }

  let response;
  try {
    response = await fetch(url, options);
  } catch (error) {
    if (error.name === 'AbortError' && signal?.aborted) throw error;
    throw new ApiError(
      error.name === 'AbortError'
        ? 'That took too long. Check your connection and try again.'
        : "You appear to be offline.",
      { status: 0, detail: `${error.name}: ${error.message}` },
    );
  } finally {
    clearTimeout(timer);
  }

  if (response.status === 401) {
    if (onUnauthorized) onUnauthorized();
    throw new ApiError('Your session has expired. Sign in again.', { status: 401 });
  }

  let payload = null;
  if (response.status !== 204) {
    const type = response.headers.get('Content-Type') || '';
    if (type.includes('application/json')) {
      try { payload = await response.json(); } catch { payload = null; }
    }
  }

  if (!response.ok) {
    const fallback = response.status >= 500
      ? 'Something went wrong on the server.'
      : 'That request could not be completed.';
    throw new ApiError(payload?.error || fallback, {
      status: response.status,
      detail: payload?.detail || '',
    });
  }
  return payload;
}

export const get = (url, options) => api(url, { ...options, method: 'GET' });
export const post = (url, body, options) => api(url, { ...options, method: 'POST', body });
export const put = (url, body, options) => api(url, { ...options, method: 'PUT', body });
export const del = (url, options) => api(url, { ...options, method: 'DELETE' });
