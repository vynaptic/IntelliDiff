/** What the screens say about the licence: why a control it refuses is
 *  disabled, a run paused for it, the names a run's reports are saved as, and a licence, its pairs and what it
 *  allows as `ds licence` prints them; and, of an open build, that it needs no licence. The server still
 *  decides. */
// Type imports only, so that Node runs the tests of this module without a bundler.
import type { Allows, Licence, LicenceFields, Run } from './api';

export const LICENCE_PAUSE = 'run_paused_licence';
const FEATURES = { runs: 'new runs', signoff: 'four-eyes sign-off', packs: 'evidence packs' };

/** Why a control is disabled, when the licence does not allow `feature`: its first warning. Undefined when it
 *  allows it, or when the standing is unknown or not loaded: the server decides. */
export function refusal(licence: Licence | undefined, feature: keyof typeof FEATURES): string | undefined {
  if (!licence?.allows || licence.allows[feature]) return undefined;
  return licence.warnings[0] ?? `The licence does not allow ${FEATURES[feature]}: see the Licence screen.`;
}

/** A soak whose boundary paused its next cycle for the licence, as `ds runs` says it. */
export const pausedLine = (run: Pick<Run, 'status' | 'stop_reason' | 'cycle'>) =>
  run.status === 'paused' && run.stop_reason === LICENCE_PAUSE
    ? `Paused for the licence at the end of cycle ${run.cycle - 1}`
    : undefined;

/** The name a run's report `name` is saved as, as the API's Content-Disposition and `ds report` name it
 *  (`licence.file_name`): `R-…-<name>`, or `R-…-unlicensed-evaluation-<name>` for a run made in evaluation. */
export const reportFile = (run: Pick<Run, 'run_id' | 'licence'>, name: string) =>
  `${run.run_id}-${run.licence?.state === 'evaluation' ? 'unlicensed-evaluation-' : ''}${name}`;

const pairs = (n: number | null) => (n === null ? 'unlimited pairs' : `${n} pair${n === 1 ? '' : 's'}`);

/** A licence's customer, order, tier, pairs and period, as `ds licence` prints them. */
export const licenceLine = (l: LicenceFields) =>
  `${l.licence_id}: ${l.customer} (order ${l.order}), ${l.tier}, ${pairs(l.pairs)}, ${l.starts} to ${l.ends}`;

/** The licence named, as `ds licence` prints it: none in an evaluation; unknown, not an evaluation, when the
 *  standing cannot be read; and none needed in an open build, which is no evaluation either. */
export function namedLine(l: Pick<Licence, 'state' | 'licence'>): string {
  if (l.state === 'unknown') return 'Licence unknown: the standing could not be read (see the server log).';
  if (l.state === 'open') return 'This build needs no licence and limits nothing.';
  return l.licence ? `Licence ${licenceLine(l.licence)}` : 'No licence: an unlicensed evaluation.';
}

/** The Licence screen's last line: how a licence arrives, or, in an open build, that none does. */
export const installLine = (l: Pick<Licence, 'state'>) =>
  l.state === 'open'
    ? 'Any number of pairs, four-eyes sign-off, evidence packs and runs under SSO work without a licence.' +
      ' ds licence install refuses every file: this build embeds no vendor key to verify one with.'
    : 'A licence file is installed on the host with ds licence install, and applies at once. It is' +
      ' issued for the installation id.';

/** What the licence allows, as `ds licence` prints it. */
export const allowsLine = (allows: Allows) =>
  Object.entries(allows)
    .map(([name, ok]) => `${name} ${ok ? 'yes' : 'no'}`)
    .join(', ');

/** How many pairs are seated and where their window opens. */
export function pairsLine(p: NonNullable<Licence['pairs']>): string {
  const since = p.since ? `the runs since ${p.since}` : 'every run';
  return `${p.limit === null ? 'Unlimited pairs' : `${pairs(p.limit)} seated`}; ${p.counted.length} counted from ${since}.`;
}
