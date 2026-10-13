/** Which controls the Run screen's "Labelled accuracy" block offers, which labelling it prints, and its draw
 *  dialog's hint (docs/mvp/ACCURACY.md §6.3, §10). Every figure, warning and limit shown is the API's text: none is
 *  built here. The server still decides. */
// Type imports only, so that Node runs the tests of this module without a bundler.
import type { ApiError, Labelling, RunDetail } from './api';

export const DRAW_HINT = '100 items give an interval about ±10 points wide at worst; 400 about ±5';

/** A cycle takes a new labelling, or labels, until its purge starts (410) or its sign-off is requested (409). */
export const takesLabelling = (run: RunDetail, cycle: number) =>
  !run.retention.purged.includes(cycle) &&
  !run.signoffs.some((s) => s.cycle === cycle && (s.status === 'requested' || s.status === 'approved'));

/** A labelling is labelled once. */
export const takesLabels = (run: RunDetail, v: Labelling) => v.labelled_at === null && takesLabelling(run, v.cycle);

/** The labelling whose lines the block prints, of a cycle's (newest first), as the reports choose it: the one a
 *  sign-off attests, else the newest a person labelled, else the newest labelled. */
export function stated(labellings: Labelling[]): Labelling | undefined {
  const labelled = labellings.filter((v) => v.labelled_at !== null);
  return labelled.find((v) => v.attested) ?? labelled.find((v) => v.labelled_by?.startsWith('user:')) ?? labelled[0];
}

/** A draw the server refuses until its operator acknowledges that labellers cannot find records (a tokenised key
 *  with no path in clear): the dialog then offers the acknowledgement. */
export const needsAcknowledgement = (e: unknown) =>
  e instanceof Error && (e as ApiError).status === 422 && e.message.includes('acknowledge_unlocatable');
