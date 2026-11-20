/** The sample pairs a draft spec is made from (Specs.tsx's profile form). */
import { parseExact, type Pair } from './api.ts';

/** Pairs from a JSON array of {key, a, b} or JSON Lines of {key, side, status, body} sample records.
 *  As in `ds profile`, a side has a document only with a 2xx JSON body (a 404 body is not one). */
export function readPairs(text: string): Pair[] {
  if (text.trimStart().startsWith('[')) return parseExact(text) as Pair[];
  const pairs = new Map<string, Pair>();
  for (const line of text.split('\n').filter((l) => l.trim())) {
    type Sample = { key: string; side: 'a' | 'b'; status?: number; body?: unknown };
    const { key, side, status = 200, body } = parseExact(line) as Sample;
    const pair = pairs.get(key) ?? { key, a: null, b: null };
    pair[side] = status >= 200 && status < 300 ? (body ?? null) : null;
    pairs.set(key, pair);
  }
  return [...pairs.values()];
}
