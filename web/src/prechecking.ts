/** What the Pre-checks screens build besides the API's own texts: the body each form
 *  sends, the name a report or a draft is saved as, whether a pair can be drafted, a pre-check's pairs counted by
 *  status, the verdicts its filter offers in the API's words, a schema code's words and the list read over its
 *  pages. Every text of a pair, the totals, the
 *  banner and each verdict's words are the API's: nothing here words one. So are the schema guard's lines on the
 *  Run screen, of which this module only says which is its banner and what its resume is called. The server
 *  still decides. */
// Type imports only, so that Node runs the tests of this module without a bundler.
import type {
  CatalogueIn, PairStatus, PrecheckDetail, PrecheckIn, PrecheckLevel, PrecheckPairDetail, PrecheckVerdict, Prechecks,
  RunSchema, TablePrecheck,
} from './api';

export const LEVELS: PrecheckLevel[] = ['schema', 'counts', 'columns', 'distinct']; // each includes the one before
export const REPORTS = ['precheck.csv', 'precheck-columns.csv', 'precheck.pdf'];
export const EVALUATION = 'Unlicensed evaluation'; // a vendor build's label of one made without a licence, under what it is not
const STATUSES: PairStatus[] = ['pending', 'running', 'done', 'cancelled']; // as a pair moves through them

/** A new pre-check as its form holds it. */
export interface Draft {
  every: boolean; // the newest version of every stored spec of two tables, not the versions chosen
  spec_ids: string[];
  level: PrecheckLevel;
}

/** A new pre-check's form: of the version `spec_id`, if given, at level columns. */
export const blank = (spec_id?: string): Draft => ({ every: false, spec_ids: spec_id ? [spec_id] : [], level: 'columns' });

/** What a form sends: the versions chosen, or every stored spec of two tables; never both, never a name. */
export const bodyOf = (d: Draft): PrecheckIn =>
  d.every ? { level: d.level, all_specs: true } : { level: d.level, spec_ids: d.spec_ids.map(Number) };

/** Two schemas paired by name as their form holds them: each side's target and schema (blank: the target's
 *  default), the globs (separated by spaces or commas) and the level. */
export interface Schemas {
  a: string;
  schema_a: string;
  b: string;
  schema_b: string;
  include: string;
  exclude: string;
  level: Exclude<PrecheckLevel, 'distinct'>;
}

/** The levels two schemas take: distinct counts need a stored spec. */
export const CATALOGUE_LEVELS: Schemas['level'][] = ['schema', 'counts', 'columns'];

/** A new pre-check of two schemas: at level counts. */
export const blankSchemas = (): Schemas => ({ a: '', schema_a: '', b: '', schema_b: '', include: '', exclude: '', level: 'counts' });

const globs = (text: string) => text.split(/[\s,]+/).filter(Boolean);

/** What the form of two schemas sends: a blank schema and blank globs are left out. */
export function catalogueOf(d: Schemas): CatalogueIn {
  const side = (target: string, schema: string) => (schema.trim() ? { target, schema: schema.trim() } : { target });
  const body: CatalogueIn = { a: side(d.a, d.schema_a), b: side(d.b, d.schema_b), level: d.level };
  for (const name of ['include', 'exclude'] as const) if (globs(d[name]).length) body[name] = globs(d[name]);
  return body;
}

/** Whether a pair can be drafted: two tables paired by name, read. */
export const draftable = (p: Pick<PrecheckPairDetail, 'spec_id' | 'relations' | 'result'>) =>
  p.spec_id === null && p.relations.every((relation) => relation !== null) && !!p.result?.names;

/** The name a draft is saved as: the CLI's. */
export const draftFile = (id: string, n: number) => `${id}-pair-${n}.draft.yaml`;

/** The name a report is saved as: the server's own, which says so of an unlicensed evaluation. */
export const reportFile = (p: Pick<TablePrecheck, 'precheck_id' | 'evaluation'>, name: string) =>
  `${p.precheck_id}-${p.evaluation ? 'unlicensed-evaluation-' : ''}${name}`;

/** Whether a pre-check has ended, and how. */
export const ended = (p: Pick<TablePrecheck, 'cancelled_at' | 'finished_at'>) =>
  p.cancelled_at ? 'cancelled' : p.finished_at ? 'finished' : 'not finished';

/** Its pairs counted by status, in the order a pair moves through them: counts, never a share. */
export const progressOf = (p: Pick<TablePrecheck, 'progress'>): [PairStatus, number][] =>
  STATUSES.flatMap((status) => (p.progress[status] ? [[status, p.progress[status]] as [PairStatus, number]] : []));

/** The verdicts a filter offers: those its pairs have, worst first, each in the API's words with its count. */
export const filterOptions = (d: Pick<PrecheckDetail, 'verdicts' | 'worded'>): { verdict: PrecheckVerdict; label: string }[] =>
  (Object.keys(d.worded) as PrecheckVerdict[])
    .filter((verdict) => d.verdicts[verdict])
    .map((verdict) => ({ verdict, label: `${d.worded[verdict]} (${d.verdicts[verdict]})` }));

/** A schema code as words: `class_differs` is class differs. */
export const codeWords = (code: string) => code.replaceAll('_', ' ');

/** The list's first `pages` pages as one answer. */
export async function listed(load: (cursor?: string) => Promise<Prechecks>, pages: number): Promise<Prechecks> {
  const found = await load();
  for (let n = 1; n < pages && found.next_cursor; n++) {
    const page = await load(found.next_cursor);
    found.items.push(...page.items);
    found.next_cursor = page.next_cursor;
  }
  return found;
}

/** The schema guard's banner on the Run screen: its pause's line, the API's first, while it holds the run paused. */
export const guardBanner = (schema: Pick<RunSchema, 'paused' | 'lines'>): string | undefined =>
  schema.paused ? schema.lines[0] : undefined;

/** Its other lines, under the banner: every line of a run it has not paused. */
export const guardLines = (schema: Pick<RunSchema, 'paused' | 'lines'>): string[] =>
  schema.paused ? schema.lines.slice(1) : schema.lines;

/** What a run's control is called: a resume that would accept the changes that stand says so. */
export const controlLabel = (action: string, schema: Pick<RunSchema, 'accepts'>) =>
  action === 'resume' && schema.accepts ? 'Resume (accept the changes)' : action;
