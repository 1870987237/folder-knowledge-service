import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import type { Api } from './api';
import { errorMessage } from './api';
import type { Overview } from './types';

export interface Claim { token: string; leaseUntil: number }
export const SessionContext = createContext<{
  api: Api; overview: Overview; refresh: () => void; revision: number;
  claims: Map<string, Claim>;
} | null>(null);
export function useSession() { const value = useContext(SessionContext); if (!value) throw new Error('Missing session'); return value; }
export function useResource<T>(path: string | null, pollMs = 0) {
  const { api, revision } = useSession();
  return useApiResource<T>(api, path, pollMs, revision);
}

export function useApiResource<T>(api: Api, path: string | null, pollMs = 0, revision = 0) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  const current = useRef(0);
  const refresh = useCallback(() => setReload(v => v + 1), []);
  useEffect(() => { setData(undefined); }, [path]);
  useEffect(() => {
    let alive = true, busy = false;
    const generation = ++current.current;
    async function load(quiet = false) {
      if (!path || busy) { if (!path) setLoading(false); return; }
      busy = true; if (!quiet) setLoading(true);
      try {
        const next = await api.request<T>(path);
        if (alive && generation === current.current) { setData(next); setError(''); }
      } catch (e) { if (alive && generation === current.current && !(e instanceof DOMException && e.name === 'AbortError')) setError(errorMessage(e)); }
      finally { busy = false; if (alive && generation === current.current) setLoading(false); }
    }
    void load();
    const timer = pollMs ? window.setInterval(() => { if (!document.hidden) void load(true); }, pollMs) : undefined;
    return () => { alive = false; window.clearInterval(timer); };
  }, [api, path, pollMs, revision, reload]);
  return { data, error, loading, refresh };
}
