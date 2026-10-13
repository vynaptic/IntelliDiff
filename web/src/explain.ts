/** What the Run screen says about an AI draft, the answer to a fixed question: why none can be asked, the
 *  engine's facts shown beside one, which stored answer is a refusal, and which key each cited finding and
 *  observation belongs to. The facts are stored ones the model was given too, never the answer's. */
// Type imports only, so that Node runs the tests of this module without a bundler.
import type { Message, Run } from './api';

const TOP_CAUSES = 10; // agent/tools.py: get_run_summary sends the first root causes only

export const AI_OFF = 'AI is off: no LLM profile is configured.';
export const NO_SUMMARY =
  'No AI summary: a draft must cite a root cause, and this cycle has none (they are written when a cycle ends).';
export const FACTS = 'Engine facts (the comparison decided these, not the AI)';
export const NOT_EVIDENCE = 'AI draft, not evidence. Check each sentence against the facts above and the evidence it cites.';
export const NO_DRAFT = 'No AI draft';
/** A citation as the server's validator reads one (agent/validate.py's CITATION, the same expression): what it
 *  did not check, such as a bracket with a line break inside, is no chip. */
export const CITATION = /\[((?:F|O|RC)-[0-9A-Za-z]+|K:[^\]\n]+)\]/g;

/** A draft's heading; `subject` is its root cause, or null for the run. */
export const draftTitle = (subject: string | null) => (subject ? `AI draft: root cause ${subject}` : 'AI draft: run summary');

/** Why no draft is asked, if none is: the buttons' title and a line under the heading. `ai` unknown leaves the
 *  buttons on (the server then answers); `viewed` is the cycle shown and `causes` its root causes, once loaded. */
export function off(ai: boolean | undefined, run: Pick<Run, 'cycle'>, viewed: number, causes?: number): string | undefined {
  if (ai === false) return AI_OFF;
  if (viewed !== run.cycle) return `AI drafts read the current cycle (cycle ${run.cycle}) only.`;
  return causes === 0 ? NO_SUMMARY : undefined;
}

const COUNTERS = ['cycle', 'total', 'match', 'different', 'indeterminate', 'outstanding', 'cancelled'];
const num = (v: unknown) => (typeof v === 'number' ? v.toLocaleString('en-US') : String(v));

/** The run's stored facts beside its summary: the status, the counters the model receives (each outcome above
 *  zero) and the root causes listed. `run.counters`, never `progress`, which leaves out a scan's identical keys. */
export function runFacts(run: Pick<Run, 'status' | 'counters'>, causes: number): string {
  const c = run.counters;
  const outcomes = Object.entries((c.by_outcome ?? {}) as Record<string, number>).filter(([, n]) => n > 0);
  return [
    `status ${run.status}`,
    ...COUNTERS.filter((name) => name in c).map((name) => `${name} ${num(c[name])}`),
    ...outcomes.map(([outcome, n]) => `${outcome} ${num(n)}`),
    `${num(causes)} root cause${causes === 1 ? '' : 's'}`,
  ].join(' · ');
}

/** What a summary could not read of a longer list. */
export const firstOnly = (causes: number) =>
  causes > TOP_CAUSES ? `This draft read the first ${TOP_CAUSES} of ${num(causes)} root causes.` : undefined;

/** The answer of a session asked a fixed question: its last assistant message. */
export const answerOf = (messages: Message[]) => messages.findLast((m) => m.role === 'assistant');

/** A stored answer that cites nothing is a refusal (the abstention after one repair, or a failed call). */
export const refused = (answer: Message) => !answer.citations?.length;

type Framed = { data?: { key_masked?: string; findings?: Cited[]; observations?: Cited[] } | null };
type Cited = { finding_id?: string; obs_id?: string; key_masked?: string };

/** The masked key of each finding and observation in the session's tool results, by id: what turns `[F-…]` and
 *  `[O-…]` into links to the Key screen. get_findings names each finding's key, get_observations its one key. */
export function citedKeys(messages: Message[]): Record<string, string> {
  const keys: Record<string, string> = {};
  for (const m of messages.filter((m) => m.role === 'tool')) {
    const { data } = JSON.parse(m.content) as Framed;
    for (const row of [...(data?.findings ?? []), ...(data?.observations ?? [])]) {
      const id = row.finding_id ?? row.obs_id;
      const key = row.key_masked ?? data?.key_masked;
      if (id && key) keys[id] = key;
    }
  }
  return keys;
}
