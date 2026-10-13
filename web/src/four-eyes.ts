/** What the screens say about four-eyes approvals (docs/mvp/APPROVALS.md §4, §5, §6.3): a run's label and a
 *  cycle's sign-off, in the words of report.pdf, the P0 summary and `ds runs` (deltasleuth/approvals.py); the
 *  fresh sign-in an approval step may need; and what a review shows and a decision echoes. */
// Type imports only, so that Node runs the tests of this module without a bundler.
import type { ApiError, Approval, ApprovalDetail, Attests, Decider, Decision, Mode, Revoked, RunApproval } from './api';

/** The UTC day of an ISO time, as retention.ts and the server print it. */
const day = (iso: string) => new Date(iso).toISOString().slice(0, 10);

export const NOT_EVIDENCE = 'Not sign-off evidence.';
const NO_APPROVERS: Record<string, string> = {
  tokens: 'tokens mode has no approvers',
  none: 'developer mode has no approvers',
};

/** Who granted an approval and when, or who revoked it, when and why. */
function decided(what: string, id: string, by: Decider[], revoked: [at: string | null, name: string | null, reason: string | null]) {
  const [at, name, reason] = revoked;
  if (at) return `${what} ${id} was revoked on ${day(at)} by ${name} (${reason}). ${NOT_EVIDENCE}`;
  return `${what} ${id} was granted by ${by.map((d) => `${d.name} on ${day(d.at)}`).join(' and ')}.`;
}

/** What a run's spec approval says, from GET /v1/runs/{id}'s `approval`. */
const STARTED: Record<string, string> = { tokens: 'tokens mode', none: 'developer mode' }; // outside SSO

export function approvalLabel(a: RunApproval): string {
  if (a.state === 'approved' || a.state === 'revoked') {
    const text = decided('Spec approval', a.approval_id ?? '', a.deciders, [a.revoked_at, a.revoked_by_name, a.reason]);
    // an approval attached to a run started outside SSO: it is never signed off
    const started = a.state === 'approved' && a.mode ? STARTED[a.mode] : undefined;
    return started ? `${text} ${NOT_EVIDENCE.slice(0, -1)}: the run started in ${started}.` : text;
  }
  if (a.state === 'not_needed') return 'The spec version needs no four-eyes approval.';
  if (!a.needs) return `Unapproved spec version: the run started before approvals (008). ${NOT_EVIDENCE}`;
  const why = (a.mode && NO_APPROVERS[a.mode]) || 'the run has no approval';
  const verb = a.needs.length === 1 ? 'was' : 'were';
  return `Unapproved spec version: ${a.needs.join(', ')} ${verb} not approved by a second person (${why}). ${NOT_EVIDENCE}`;
}

/** Who took an approval's steps and when, in order. */
export const deciders = (a: Approval): Decider[] =>
  (['first', 'decided'] as const).flatMap((step) => {
    const actor = a[`${step}_by`];
    return actor ? [{ actor, name: a[`${step}_by_name`], at: a[`${step}_at`] ?? '' }] : [];
  });

/** What a cycle's sign-off says, from its requests (newest first): the approved one, else the newest revoked. */
export function signoffLabel(signoffs: Approval[]): string | undefined {
  const found = signoffs.find((s) => s.status === 'approved') ?? signoffs.find((s) => s.status === 'revoked');
  if (!found) return undefined;
  return decided('Sign-off', found.approval_id, deciders(found), [found.revoked_at, found.revoked_by_name, found.revoke_reason]);
}

/** Why a request cannot be made here, in the server's words: tokens and developer modes have no approvers. */
export const ssoOnly = (mode?: Mode) =>
  `Approvals need SSO (auth.oidc); this deployment runs in ${mode === 'tokens' ? 'tokens mode' : 'developer mode'}.`;

/** Where an approver signs in again when an approval step answers 403 reauth_required: the identity provider
 *  authenticates afresh (docs/mvp/AUTH.md §2) and the sign-in returns to the review. */
export const reauthUrl = (approvalId: string) =>
  '/v1/auth/login?reauth=1&next=' + encodeURIComponent(`#/approvals/${encodeURIComponent(approvalId)}`);

export const isReauth = (e: unknown) =>
  e instanceof Error && (e as ApiError).status === 403 && e.message === 'reauth_required';

/** A decision's body. Approving echoes what the review showed, a spec version's content hash and scopes or a
 *  sign-off's attests hash, so that no other content is approved; a note goes only when one was written. */
export function decisionBody(a: ApprovalDetail, decision: Decision['decision'], note: string): Decision {
  const body: Decision = { decision, note: note || undefined };
  if (decision === 'reject') return body;
  if (a.kind === 'spec') return { ...body, content_hash: a.review.content_hash, scopes: a.scopes };
  return { ...body, attests_sha256: a.attests_sha256 ?? undefined };
}

/** What a revoke did, from its answer: the runs it paused and stopped, and the sign-offs revoked with it. */
export const revokedLine = (r: Revoked) =>
  `Runs paused: ${r.paused.join(', ') || 'none'}. Runs stopped: ${r.stopped.join(', ') || 'none'}. ` +
  `Sign-offs revoked: ${r.revoked.join(', ') || 'none'}.`;

/** A key list someone wrote (a --keys file or a request's keys, the spec's inline list or its key table): not
 *  a census. */
export const explicitKeys = (source: string | null) => source === 'explicit' || source === 'list' || source === 'table';

export interface Check { check: string; attested: string; note?: string; flag?: boolean }

/** A sign-off's checklist from what it attests (§1.2): the checks a request passed (a table scan's counts
 *  among them), then the approver's own judgement of the key source and the coverage. */
export function checklist(t: Attests): Check[] {
  const c = t.counters;
  const holding = c.reconciliation.filter((i) => i.holds).length;
  return [
    { check: 'A live run made under approvals', attested: `${t.mode} run ${t.run_id}, started in ${t.auth_mode} mode` },
    { check: 'Ended, with its own counters', attested: `cycle ${t.cycle}, ended ${day(t.ended_at)}` },
    { check: 'Complete', attested: `${c.compared} compared, ${c.cancelled} cancelled, ${c.outstanding} owed a recheck` },
    ...(t.scan ? [{ check: 'Scanned to the end', attested: scanned(t.scan) }] : []),
    { check: 'Counters reconcile', attested: `${holding} of ${c.reconciliation.length} identities hold` },
    { check: 'Uncut', attested: 'the key set was not cut at maxKeys' },
    { check: 'No unexplained CRITICAL', attested: `${t.unexplained_critical} key(s)` },
    { check: 'Approved content', attested: `spec approval ${t.spec_approval_id}, content hash ${t.content_hash}` },
    explicitKeys(t.keys_source)
      ? { check: 'Key source', attested: t.keys_source ?? '', note: 'An explicit key list, not a census of either side.', flag: true }
      : { check: 'Key source', attested: t.keys_source ?? 'not recorded', note: 'Your judgement.' },
    {
      check: 'Coverage',
      attested:
        `coverage ${c.coverage ?? 'none'}, ${c.indeterminate} indeterminate, ${c.absent_both} absent on both sides, ` +
        `${c.api_uncomparable} not comparable through the API`,
      note: 'Your judgement.',
    },
  ];
}

/** A table scan's counts, as a sign-off attests them. */
function scanned(s: NonNullable<Attests['scan']>): string {
  const read = `rows a ${s.rows_a}, b ${s.rows_b} read, skipped a ${s.skipped_a}, b ${s.skipped_b}`;
  return `${s.done ? 'done' : 'stopped before the end'}: ${read}; ${s.matched} identical, ${s.candidates} compared by key`;
}

type Mark = { json_pointer: string; change_type: string };
const isNode = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null;
const kind = (v: unknown) => (v === null ? 'null' : Array.isArray(v) ? 'array' : typeof v);
// A pointer segment as DocDiff locates it: RFC 6901, and a leading `@` doubled (a single one names a duplicate).
const segment = (k: string) => k.replace(/~/g, '~0').replace(/\//g, '~1').replace(/^@/, '@@');

/** Where two spec bodies differ, as marks DocDiff highlights: `previous` against this version. */
export function bodyChanges(a: unknown, b: unknown, at = ''): Mark[] {
  if (a === undefined && b === undefined) return [];
  if (a === undefined) return [{ json_pointer: at, change_type: 'extra_in_b' }];
  if (b === undefined) return [{ json_pointer: at, change_type: 'missing_in_b' }];
  if (kind(a) !== kind(b)) return [{ json_pointer: at, change_type: 'type_changed' }];
  if (!isNode(a) || !isNode(b)) return a === b ? [] : [{ json_pointer: at, change_type: 'value_changed' }];
  const own = (v: Record<string, unknown>, k: string) => (Object.hasOwn(v, k) ? v[k] : undefined);
  return [...new Set([...Object.keys(a), ...Object.keys(b)])].flatMap((k) =>
    bodyChanges(own(a, k), own(b, k), `${at}/${segment(k)}`),
  );
}
