import { afterEach, describe, expect, it, vi } from 'vitest';
import { Api, parseObject, query } from './api';

afterEach(() => vi.unstubAllGlobals());
describe('admin session boundary', () => {
  it('uses bearer authentication without caching, and uploads ZIP bytes intact', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('{}'));
    vi.stubGlobal('fetch', fetcher);
    const api = new Api('test-only-token', vi.fn());
    const zip = new Blob(['test zip']);
    await api.request('/templates/a/versions/1/package', 'PUT', zip);
    const [url, init] = fetcher.mock.calls[0];
    expect(url).toBe('/admin/api/templates/a/versions/1/package');
    expect(init).toMatchObject({ cache: 'no-store', method: 'PUT', body: zip, headers: { Authorization: 'Bearer test-only-token', 'Content-Type': 'application/zip' } });
  });
  it('expires the session on 401 but preserves it on validation errors', async () => {
    const unauthorized = vi.fn();
    const api = new Api('test-only-token', unauthorized);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response('{"error":"workspace_view_mismatch"}', { status: 400 })).mockResolvedValueOnce(new Response('{}', { status: 401 })));
    await expect(api.request('/binding')).rejects.toThrow('挂载目录一致');
    expect(unauthorized).not.toHaveBeenCalled();
    await expect(api.request('/overview')).rejects.toMatchObject({ status: 401 });
    expect(unauthorized).toHaveBeenCalledOnce();
  });
  it('aborts outstanding calls and rejects late responses after logout', async () => {
    let resolve!: (r: Response) => void;
    const fetcher = vi.fn().mockImplementation(() => new Promise<Response>(r => { resolve = r; }));
    vi.stubGlobal('fetch', fetcher);
    const api = new Api('test-only-token', vi.fn());
    const pending = api.request('/overview');
    const rejected = expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    api.dispose();
    expect(fetcher.mock.calls[0][1].signal.aborted).toBe(true);
    resolve(new Response('{"private":"stale"}'));
    await rejected;
    await expect(api.request('/overview')).rejects.toMatchObject({ name: 'AbortError' });
    expect(fetcher).toHaveBeenCalledOnce();
  });
});
it('rejects non-object configuration and encodes literal query characters', () => {
  for (const input of ['null', '[]', '1', '{']) expect(() => parseObject(input)).toThrow();
  expect(parseObject('{"name":"docs"}')).toEqual({ name: 'docs' });
  expect(new URLSearchParams(query({ path: 'a&b/中文', offset: 0, cursor: null })).get('path')).toBe('a&b/中文');
});
