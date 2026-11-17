/**
 * The tab's half of a worker-hosted custom chip's HTTP egress
 * (simulation/customChips/chipHttpRelay.ts): the worker emits
 * `chip_http_request`, the tab fetches from the student's machine and answers.
 */
import { describe, expect, it, vi } from 'vitest';
import { performChipHttpRequest } from '../simulation/customChips/chipHttpRelay';

const b64 = (s: string) => btoa(s);
const unb64 = (s: string) => atob(s);

describe('performChipHttpRequest', () => {
  it('performs the request and returns status and body', async () => {
    const fetchImpl = vi.fn(async () => new Response('{"pose":[1,2]}', { status: 200 }));
    const resp = await performChipHttpRequest(
      {
        id: 7,
        method: 'post',
        url: 'http://localhost:3000/pose',
        content_type: 'application/json',
        body_b64: b64('{"j":1}'),
      },
      fetchImpl as unknown as typeof fetch,
    );
    expect(resp).toMatchObject({ id: 7, status: 200, error: '', truncated: false });
    expect(unb64(resp.body_b64)).toBe('{"pose":[1,2]}');
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('http://localhost:3000/pose');
    expect(init.method).toBe('POST');
    expect(init.redirect).toBe('manual');
    expect(init.credentials).toBe('omit');
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json');
    expect(new TextDecoder().decode(init.body as Uint8Array)).toBe('{"j":1}');
  });

  it('sends no body with GET', async () => {
    const fetchImpl = vi.fn(async () => new Response('', { status: 204 }));
    await performChipHttpRequest(
      { id: 1, method: 'GET', url: 'http://localhost:3000/', body_b64: b64('x') },
      fetchImpl as unknown as typeof fetch,
    );
    const [, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(init.body).toBeUndefined();
  });

  it('cuts a reply at max_bytes', async () => {
    const fetchImpl = vi.fn(async () => new Response('0123456789', { status: 200 }));
    const resp = await performChipHttpRequest(
      { id: 2, method: 'GET', url: 'http://localhost:3000/', max_bytes: 4 },
      fetchImpl as unknown as typeof fetch,
    );
    expect(unb64(resp.body_b64)).toBe('0123');
    expect(resp.truncated).toBe(true);
  });

  it('refuses a method or scheme a chip may not use, without fetching', async () => {
    const fetchImpl = vi.fn();
    const a = await performChipHttpRequest(
      { id: 3, method: 'CONNECT', url: 'http://localhost:3000/' },
      fetchImpl as unknown as typeof fetch,
    );
    const b = await performChipHttpRequest(
      { id: 4, method: 'GET', url: 'file:///etc/passwd' },
      fetchImpl as unknown as typeof fetch,
    );
    expect(a.error).toMatch(/CONNECT/);
    expect(b.error).toMatch(/scheme/);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it('turns a network/CORS failure into an error, never a throw', async () => {
    const fetchImpl = vi.fn(async () => {
      throw new TypeError('Failed to fetch');
    });
    const resp = await performChipHttpRequest(
      { id: 5, method: 'GET', url: 'http://localhost:3000/' },
      fetchImpl as unknown as typeof fetch,
    );
    expect(resp.id).toBe(5);
    expect(resp.error).toMatch(/CORS/);
  });

  it('times out', async () => {
    const fetchImpl = vi.fn(
      (_url: string, init: RequestInit) =>
        new Promise<Response>((_res, rej) => {
          init.signal?.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError')));
        }),
    );
    const resp = await performChipHttpRequest(
      { id: 6, method: 'GET', url: 'http://localhost:3000/', timeout_ms: 20 },
      fetchImpl as unknown as typeof fetch,
    );
    expect(resp.error).toMatch(/timed out/);
  });
});
