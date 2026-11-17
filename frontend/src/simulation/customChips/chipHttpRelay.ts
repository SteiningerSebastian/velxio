/**
 * chipHttpRelay — the tab's half of a worker-hosted custom chip's HTTP egress.
 *
 * A custom chip on an ESP32 runs inside the backend's QEMU worker. When it
 * calls vx_net_request / vx_net_step for a URL on the STUDENT's machine (a
 * robot simulator on localhost), the worker cannot reach it: its localhost is
 * the server or the Docker container. So the worker emits a
 * `chip_http_request` event (backend/app/services/chip_net.py, BrowserRelay),
 * this module performs the fetch from the browser, and the bridge sends the
 * answer back as `esp32_chip_http_response`.
 *
 * The worker has already checked the URL against the deployment's allowlist;
 * this side re-checks only the shape (http/https, a method a chip may use) so
 * a malformed event cannot turn into an arbitrary request.
 *
 * What the peer must do (see docs/wiki/custom-chips-api-reference.md):
 *   - answer CORS: `Access-Control-Allow-Origin` (the Velxio origin or `*`),
 *     and an OPTIONS preflight for a POST with a JSON body;
 *   - on a public Velxio (https://…), Chrome asks the student once for
 *     "local network access" before the first request to localhost.
 */

export interface ChipHttpRequest {
  id: number;
  method: string;
  url: string;
  content_type?: string;
  body_b64?: string;
  timeout_ms?: number;
  max_bytes?: number;
}

export interface ChipHttpResponse {
  id: number;
  status: number;
  body_b64: string;
  truncated: boolean;
  error: string;
}

const ALLOWED_METHODS = new Set(['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']);
const DEFAULT_TIMEOUT_MS = 5000;
const DEFAULT_MAX_BYTES = 1 << 20;

function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function bytesToB64(bytes: Uint8Array): string {
  let bin = '';
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(bin);
}

/** Read at most `max` bytes of a response body; reports whether it was cut. */
async function readCapped(resp: Response, max: number): Promise<{ body: Uint8Array; truncated: boolean }> {
  if (!resp.body) {
    const buf = new Uint8Array(await resp.arrayBuffer());
    return buf.length > max
      ? { body: buf.subarray(0, max), truncated: true }
      : { body: buf, truncated: false };
  }
  const reader = resp.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  let truncated = false;
  for (;;) {
    const { done, value } = await reader.read();
    if (done || !value) break;
    if (total + value.length > max) {
      chunks.push(value.subarray(0, max - total));
      total = max;
      truncated = true;
      await reader.cancel().catch(() => undefined);
      break;
    }
    chunks.push(value);
    total += value.length;
  }
  const body = new Uint8Array(total);
  let off = 0;
  for (const c of chunks) {
    body.set(c, off);
    off += c.length;
  }
  return { body, truncated };
}

/** Perform one relayed request. Never throws: failures come back in `error`. */
export async function performChipHttpRequest(
  req: ChipHttpRequest,
  fetchImpl: typeof fetch = globalThis.fetch.bind(globalThis),
): Promise<ChipHttpResponse> {
  const id = Number(req.id);
  const fail = (error: string): ChipHttpResponse => ({
    id, status: 0, body_b64: '', truncated: false, error,
  });

  const method = String(req.method ?? '').toUpperCase();
  if (!ALLOWED_METHODS.has(method)) return fail(`method ${method || '(none)'} is not one a chip may use`);
  let url: URL;
  try {
    url = new URL(String(req.url ?? ''));
  } catch {
    return fail(`unparsable URL ${String(req.url)}`);
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    return fail(`scheme ${url.protocol} is not http or https`);
  }

  const timeoutMs = req.timeout_ms && req.timeout_ms > 0 ? req.timeout_ms : DEFAULT_TIMEOUT_MS;
  const maxBytes = req.max_bytes && req.max_bytes > 0 ? req.max_bytes : DEFAULT_MAX_BYTES;
  const headers: Record<string, string> = {};
  if (req.content_type) headers['Content-Type'] = req.content_type;
  const hasBody = method !== 'GET' && method !== 'HEAD';
  const body = hasBody && req.body_b64 ? b64ToBytes(req.body_b64) : undefined;

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const resp = await fetchImpl(url.toString(), {
      method,
      headers,
      body: body as BodyInit | undefined,
      // The server path never follows redirects either (an allowed host
      // must not be able to bounce the request somewhere else).
      redirect: 'manual',
      credentials: 'omit',
      cache: 'no-store',
      signal: ctrl.signal,
    });
    if (resp.type === 'opaqueredirect') {
      return fail(`${method} ${url} answered a redirect, which a chip request does not follow`);
    }
    const { body: data, truncated } = await readCapped(resp, maxBytes);
    return { id, status: resp.status, body_b64: bytesToB64(data), truncated, error: '' };
  } catch (e) {
    if (ctrl.signal.aborted) return fail(`${method} ${url} timed out after ${timeoutMs} ms`);
    // A CORS refusal, a closed port and a denied local-network permission all
    // surface as the same TypeError; say what to check.
    const msg = e instanceof Error ? e.message : String(e);
    return fail(
      `${method} ${url} failed in the browser (${msg}) - is the simulator running, ` +
        `and does it answer CORS (Access-Control-Allow-Origin)?`,
    );
  } finally {
    clearTimeout(timer);
  }
}
