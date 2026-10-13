/** Data hooks, the hash router, who is signed in and the licence. */
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { qs, type Licence, type Me, type Page, type Query } from './api';

export const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** Who is signed in; `can(role)` gates controls, and the server still decides (docs/mvp/AUTH.md §9). */
export const Who = createContext<{ me?: Me; can: (role: string) => boolean }>({ can: () => false });
export const useWho = () => useContext(Who);

/** The installation's licence (docs/mvp/LICENCE.md §12), loaded after `me`; undefined until then, or when it
 *  could not be loaded. Controls it refuses are disabled, and the server still decides. */
export const Licensed = createContext<Licence | undefined>(undefined);
export const useLicence = () => useContext(Licensed);

/** `#/a/b` with each part encoded, plus a query of the non-empty values. */
export const href = (parts: (string | number)[], query: Query = {}) =>
  `#/${parts.map((p) => encodeURIComponent(p)).join('/')}${qs(query)}`;

export function useRoute(): { parts: string[]; query: URLSearchParams } {
  const [hash, setHash] = useState(location.hash);
  useEffect(() => {
    const onChange = () => setHash(location.hash);
    addEventListener('hashchange', onChange);
    return () => removeEventListener('hashchange', onChange);
  }, []);
  const [path, query] = hash.replace(/^#\/?/, '').split('?', 2);
  return { parts: path.split('/').filter(Boolean).map(decodeURIComponent), query: new URLSearchParams(query) };
}

/** The latest result of `load`, run again when `deps` change or on `reload()`. */
export function useLoad<T>(load: () => Promise<T>, deps: unknown[]) {
  const [state, setState] = useState<{ data?: T; error?: string }>({});
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true; // a late answer to an earlier request must not overwrite a newer one
    load().then(
      (data) => live && setState({ data }),
      (e) => live && setState((s) => ({ ...s, error: message(e) })),
    );
    return () => {
      live = false;
    };
  }, [...deps, tick]);
  return { ...state, reload: useCallback(() => setTick((t) => t + 1), []) };
}

/** Pages of `load` accumulated by `more()`, restarted when `deps` change. */
export function usePaged<T>(load: (cursor?: string) => Promise<Page<T>>, deps: unknown[]) {
  const query = JSON.stringify(deps); // a cursor continues only the query that issued it
  const [state, setState] = useState<{ items: T[]; next?: string | null; query?: string; error?: string }>({ items: [] });
  const latest = useRef(0);
  const fetchPage = (cursor?: string) => {
    const mine = ++latest.current;
    load(cursor).then(
      (p) => mine === latest.current && setState((s) => ({ items: cursor ? [...s.items, ...p.items] : p.items, next: p.next_cursor, query })),
      (e) => mine === latest.current && setState((s) => ({ ...s, error: message(e) })),
    );
  };
  useEffect(() => fetchPage(), deps);
  const { next } = state;
  return { ...state, more: next && state.query === query ? () => fetchPage(next) : undefined };
}

export function usePoll(tick: () => void, ms: number, active: boolean): void {
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(tick, ms);
    return () => clearInterval(timer);
  }, [tick, ms, active]);
}
