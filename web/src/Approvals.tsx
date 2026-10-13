/** Four-eyes approvals, under SSO only: one list with a status filter, and a review. A review shows the
 *  earlier requests of the version or cycle above Approve and Reject, then what is approved: a spec version's
 *  needed items, who may not approve it and its diff against the version approved before it; or a sign-off's
 *  checklist, attested census and adjudication, a sampled cycle's draw, frame, strata and ledger, the
 *  labelling it attests, counters, rules and root causes. The server still decides. */
import { useState } from 'react';
import { api, type Approval, type ApprovalDetail, type Attests, type LedgerDraw, type Revoked, type Stratum } from './api';
import { DocDiff } from './DocDiff';
import { bodyChanges, checklist, decisionBody, isReauth, reauthUrl, revokedLine, ssoOnly, type Check } from './four-eyes';
import { href, message, useLoad, usePaged, useWho } from './lib';
import { attestedLine, censusLine, censusWarnings } from './packs';
import { attestedLines, ledgerLine, percent, strataLine } from './samples';
import { Badge, Err, Named, Stats, Table, cell, when, type Col } from './ui';

const STATUSES = ['requested', 'approved', 'rejected', 'revoked'];

export function Approvals({ id, query }: { id?: string; query: URLSearchParams }) {
  const { me } = useWho();
  if (me?.mode !== 'oidc')
    return (
      <>
        <h1>Approvals</h1>
        <p>{ssoOnly(me?.mode)}</p>
      </>
    );
  return id ? <Review key={id} id={id} /> : <List status={query.get('status') ?? ''} />;
}

/** What an approval covers: a spec version's scopes, or the cycle a sign-off accepts. */
function covers(a: Approval) {
  if (a.kind === 'spec') return a.scopes.join(', ') || 'no scope';
  return <a href={href(['runs', a.run_id ?? ''], { cycle: a.cycle ?? undefined })}>{`cycle ${a.cycle} of run ${a.run_id}`}</a>;
}

/** The "Two approvers needed" marker (an author is unattributed), and the first approver once there is one. */
function Approvers({ a }: { a: Approval }) {
  if (a.approvers_needed < 2) return <>one</>;
  return (
    <>
      <span className="badge warn">Two approvers needed</span>
      {a.first_by && <> step 1 by <Named name={a.first_by_name} actor={a.first_by} /></>}
    </>
  );
}

const LIST_COLS: Col<Approval>[] = [
  ['Approval', (a) => <a className="mono" href={href(['approvals', a.approval_id])}>{a.approval_id}</a>],
  ['Status', (a) => <Badge v={a.status} />],
  ['Kind', (a) => (a.kind === 'spec' ? 'spec version' : 'sign-off')],
  ['Spec', (a) => `${a.spec_name} ${a.spec_version} (#${a.spec_id})`],
  ['Covers', covers],
  ['Approvers', (a) => <Approvers a={a} />],
  ['Requested by', (a) => <Named name={a.requested_by_name} actor={a.requested_by} />],
  ['Requested', (a) => when(a.requested_at)],
];

function List({ status }: { status: string }) {
  const rows = usePaged((cursor) => api.approvals({ status, cursor }), [status]);
  return (
    <>
      <h1>Approvals</h1>
      <p className="muted">
        Under SSO a spec version that shows values in clear, suppresses findings or filters what is compared runs only once
        someone who did not write it approved its content; a cycle is parity evidence only once someone who neither wrote
        its spec, started its run nor asked for the sign-off accepted it.
      </p>
      <p className="inline">
        <label>
          Status{' '}
          <select value={status} onChange={(e) => (location.hash = href(['approvals'], { status: e.target.value }))}>
            <option value="">any</option>
            {STATUSES.map((s) => <option key={s}>{s}</option>)}
          </select>
        </label>
      </p>
      <Err error={rows.error} />
      <Table rows={rows.items} cols={LIST_COLS} rowId={(a) => a.approval_id} />
      {rows.more && <button onClick={rows.more}>More</button>}
    </>
  );
}

const HISTORY_COLS: Col<Approval>[] = [
  ['Approval', (a) => <a className="mono" href={href(['approvals', a.approval_id])}>{a.approval_id}</a>],
  ['Status', (a) => <Badge v={a.status} />],
  ['Decided by', (a) => <Named name={a.decided_by_name} actor={a.decided_by} />],
  ['Decided', (a) => when(a.decided_at)],
  ['Note', (a) => a.decision_note],
  ['Revoked because', (a) => a.revoke_reason],
];

function Review({ id }: { id: string }) {
  const found = useLoad(() => api.approval(id), [id]);
  // What a revoke made here answered. The review keeps it: the reload after it unmounts the Revoke panel.
  const [revoked, setRevoked] = useState<Revoked>();
  const a = found.data;
  return (
    <>
      <p>
        <a href={href(['approvals'])}>Approvals</a>
      </p>
      <h1>
        Approval <span className="mono">{id}</span> <Badge v={a?.status} />
      </h1>
      <Err error={found.error} />
      {a && (
        <>
          <Summary a={a} />
          <section>
            <h2>Earlier requests</h2>
            <Table rows={a.review.history} cols={HISTORY_COLS} rowId={(h) => h.approval_id} />
          </section>
          {a.status === 'requested' && <Decide a={a} reload={found.reload} />}
          {a.status === 'approved' && !revoked && (
            <Revoke
              a={a}
              done={(answer) => {
                setRevoked(answer);
                found.reload();
              }}
            />
          )}
          {revoked && (
            <section className="panel">
              <h2>Revoked</h2>
              <p>{revokedLine(revoked)}</p>
            </section>
          )}
          {a.kind === 'spec' ? <SpecReview a={a} /> : <SignoffReview a={a} attests={a.attests!} />}
        </>
      )}
    </>
  );
}

function Summary({ a }: { a: ApprovalDetail }) {
  const spec = `${a.spec_name} ${a.spec_version} (spec #${a.spec_id})`;
  return (
    <section>
      <p>
        {a.kind === 'spec' ? (
          <>
            The whole content of {spec}, owner {a.spec_owner ?? 'not named'}; its runs need{' '}
            {a.scopes.join(', ') || 'no scope'}. Content hash <span className="mono">{a.review.content_hash}</span>.
          </>
        ) : (
          <>
            The sign-off of {covers(a)} ({spec}): its facts, hash <span className="mono">{a.attests_sha256}</span>.
          </>
        )}
      </p>
      <p>
        Requested by <Named name={a.requested_by_name} actor={a.requested_by} /> on {when(a.requested_at)}
        {a.request_note && <>: {a.request_note}</>}
      </p>
      {a.approvers_needed > 1 && (
        <p>
          <Approvers a={a} />
          {a.first_at && ` on ${when(a.first_at)}`}
        </p>
      )}
      {a.decided_by && (
        <p>
          {a.status === 'rejected' ? 'Rejected' : 'Approved'} by <Named name={a.decided_by_name} actor={a.decided_by} /> on{' '}
          {when(a.decided_at)}
          {a.decision_note && <>: {a.decision_note}</>}
        </p>
      )}
      {a.revoked_by && (
        <p className="warn">
          Revoked by <Named name={a.revoked_by_name} actor={a.revoked_by} /> on {when(a.revoked_at)}: {a.revoke_reason}
        </p>
      )}
    </section>
  );
}

function Decide({ a, reload }: { a: ApprovalDetail; reload: () => void }) {
  const { can, me } = useWho();
  const [note, setNote] = useState('');
  const [reauth, setReauth] = useState(false);
  const [error, setError] = useState<string>();
  const approver = can('rule_approver') && me?.kind === 'user'; // rejecting has no four-eyes check

  async function decide(decision: 'approve' | 'reject') {
    setError(undefined);
    setReauth(false);
    try {
      await api.decide(a.approval_id, decisionBody(a, decision, note));
      setNote('');
      reload();
    } catch (e) {
      if (isReauth(e)) setReauth(true);
      else setError(message(e));
    }
  }

  return (
    <section className="panel">
      <h2>Decision</h2>
      {a.kind === 'signoff' && <p>Approving decides each rule and root cause listed below.</p>}
      <label>
        Note: required to reject, and not kept for the first of two approvers
        <textarea rows={3} maxLength={1000} value={note} onChange={(e) => setNote(e.target.value)} />
      </label>
      <p className="inline">
        <button onClick={() => decide('approve')} disabled={!a.you.can_decide}>Approve</button>
        <button onClick={() => decide('reject')} disabled={!approver}>Reject</button>
        {a.you.reason && <span className="muted">{a.you.reason}</span>}
      </p>
      {reauth && (
        <p className="inline">
          <span className="warn">An approval needs a sign-in in the last 15 minutes. Sign in again, then approve.</span>
          <button onClick={() => location.assign(reauthUrl(a.approval_id))}>Sign in again</button>
        </p>
      )}
      <Err error={error} />
    </section>
  );
}

function Revoke({ a, done }: { a: ApprovalDetail; done: (answer: Revoked) => void }) {
  const { can, me } = useWho();
  const [reason, setReason] = useState('');
  const [error, setError] = useState<string>();
  if (!(can('rule_approver') && me?.kind === 'user')) return null;
  const rests = a.kind === 'spec' ? ': its running runs pause, its queued one-shot runs stop and its sign-offs are revoked' : '';

  async function revoke() {
    if (!confirm(`Revoke ${a.approval_id}${rests}?`)) return;
    setError(undefined);
    try {
      done(await api.revoke(a.approval_id, reason));
    } catch (e) {
      setError(message(e));
    }
  }

  return (
    <section className="panel">
      <h2>Revoke</h2>
      <p className="inline">
        <label>
          Reason <input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={200} size={40} />
        </label>
        <button onClick={revoke} disabled={!reason}>Revoke</button>
      </p>
      <Err error={error} />
    </section>
  );
}

/** Who may not approve the request, and whether an unattributed author makes two approvers necessary. */
function Exclusion({ a }: { a: ApprovalDetail }) {
  const { persons, unattributed } = a.review.exclusion;
  return (
    <section>
      <h2>Who may not approve it</h2>
      <p>
        {persons.length
          ? persons.map((p, i) => (
              <span key={p.actor}>
                {i > 0 && ', '}
                <Named name={p.name} actor={p.actor} />
              </span>
            ))
          : 'Nobody named.'}
      </p>
      {unattributed && (
        <p className="warn">
          An author is unattributed (a CLI, token, developer-mode or older upload, a token-started run or a token
          request): two rule_approvers outside this set must approve.
        </p>
      )}
    </section>
  );
}

type Item = { scope: string; pointer: string; value: unknown };
const NEEDED_COLS: Col<Item>[] = [
  ['Scope', (n) => n.scope],
  ['Pointer', (n) => <code>{n.pointer}</code>],
  ['Value', (n) => <code>{cell(n.value)}</code>],
];

const LEDGER_COLS: Col<LedgerDraw>[] = [
  ['Draw', (d) => <span className="mono">{d.draw_id}</span>],
  ['Made', (d) => (d.order === 'this' ? 'this draw' : d.order ?? '')],
  ['Drawn', (d) => when(d.drawn_at)],
  ['Status', (d) => <Badge v={d.status} />],
  ['Draw, status and close note', (d) => ledgerLine(d)],
];

/** Every draw of the population under any name, in commit order, with its status and the fix its close records. */
function Ledger({ rows, caption }: { rows: LedgerDraw[]; caption: string }) {
  return (
    <section>
      <h2>Draws of this population</h2>
      <p className="muted">{caption}</p>
      <Table rows={rows} cols={LEDGER_COLS} rowId={(d) => `draw-${d.draw_id}`} />
    </section>
  );
}

function SampleReview({ a, sample }: { a: ApprovalDetail; sample: NonNullable<Attests['sample']> }) {
  const strata = (sample.judgement?.strata ?? []) as Stratum[];
  return (
    <>
      <section>
        <h2>Sample and bounds (observed)</h2>
        {attestedLines(sample).map((line) => <p key={line}>{line}</p>)}
        {strata.map((s) => <p key={s.label}>{strataLine(s)}</p>)}
        <p className="muted">
          Each stratum's bound is one-sided 95% Clopper-Pearson against its target (
          {Object.entries(sample.targets).map(([tier, t]) => `${tier} ≤ ${percent(t)}`).join(', ')}); no bound is given
          for the strata pooled.
        </p>
      </section>
      <Ledger
        rows={a.review.ledger ?? []}
        caption="Earlier draws must have passed or been closed with a fix before this one was drawn; a later failure spends this pass."
      />
    </>
  );
}

/** A sign-off's labelled accuracy, in the server's words: what the labelling it attests
 *  says and may not be read as, then each other labelling of its cycle in one line. */
function AccuracyReview({ a, attested }: { a: ApprovalDetail; attested?: string }) {
  const found = useLoad(
    async () => ({
      others: (await api.labellings(a.run_id ?? '', a.cycle ?? 0)).items.filter((v) => v.labelling_id !== attested),
      limits: attested && (await api.labelling(attested)).limits,
    }),
    [a.approval_id],
  );
  return (
    <section>
      <h2>Labelled accuracy (observed)</h2>
      {attested ? a.review.lines?.map((line) => <p key={line}>{line}</p>) : <p>No labelling a person labelled is attested.</p>}
      {found.data?.limits && <p className="muted">{found.data.limits}</p>}
      <Err error={found.error} />
      {found.data?.others.length ? <h3>Other labellings of this cycle</h3> : null}
      {found.data?.others.map((v) => <p key={v.labelling_id}>{v.summary}</p>)}
    </section>
  );
}

function SpecReview({ a }: { a: ApprovalDetail }) {
  const { body, previous } = a.review;
  const before = previous?.body;
  return (
    <>
      <section>
        <h2>What its runs need approved</h2>
        <p className="muted">
          The approval covers the whole content hash: review the tolerances, normalisers, key sources, select and map in
          the diff below as well.
        </p>
        <Table rows={a.review.needed as Item[]} cols={NEEDED_COLS} />
      </section>
      {a.review.ledger && (
        <Ledger rows={a.review.ledger} caption="The sampling this version pre-registers draws from a population with these draws." />
      )}
      <Exclusion a={a} />
      <section>
        <h2>
          {previous
            ? `Changes since spec #${previous.spec_id} (${previous.version}), approved as ${previous.approval_id}`
            : 'The whole content: no earlier version of this spec is approved'}
        </h2>
        <p className="muted">A: the version approved before; B: this version. Inline keys are masked.</p>
        <DocDiff a={before ?? null} b={body} findings={bodyChanges(before, body)} />
      </section>
    </>
  );
}

const CHECK_COLS: Col<Check>[] = [
  ['Check', (c) => c.check],
  ['Attested', (c) => c.attested],
  ['Note', (c) => <span className={c.flag ? 'warn' : undefined}>{c.note}</span>],
];
const RULE_COLS: Col<Attests['rules'][number]>[] = [
  ['Rule', (r) => <span className="mono">{r.rule_id}</span>],
  ['Classification', (r) => r.classification],
  ['When', (r) => <code>{r.when}</code>],
  ['Reason', (r) => r.reason],
  ['Ticket', (r) => r.ticket],
  ['Expires', (r) => r.expires],
  ['Keys suppressed', (r) => r.keys],
];
const CAUSE_COLS: Col<Attests['root_causes'][number]>[] = [
  ['Root cause', (c) => <span className="mono">{c.root_cause_id}</span>],
  ['Severity', (c) => <Badge v={c.max_severity} />],
  ['Taxonomy', (c) => c.taxonomy_code],
  ['Change', (c) => c.change_type],
  ['Path', (c) => <code>{c.path_pattern}</code>],
  ['Keys', (c) => c.affected_keys],
];

function SignoffReview({ a, attests }: { a: ApprovalDetail; attests: Attests }) {
  const counters = Object.fromEntries(Object.entries(attests.counters).filter(([name]) => name !== 'reconciliation'));
  return (
    <>
      <section>
        <h2>Checklist</h2>
        <Table rows={checklist(attests)} cols={CHECK_COLS} />
      </section>
      <section>
        <h2>Census and adjudication</h2>
        {(attests.format ?? 0) >= 1 && attests.census ? (
          <>
            <p>Census: {censusLine(attests.census)}</p>
            {censusWarnings(attests.census).map((w) => <p className="warn" key={w}>{w}</p>)}
            <p>{attestedLine(attests.adjudication)}</p>
          </>
        ) : (
          <p className="warn">Requested before evidence packs (attests format 0): it attests no census or adjudication.</p>
        )}
      </section>
      {attests.sample && <SampleReview a={a} sample={attests.sample} />}
      {(attests.format ?? 0) >= 3 && <AccuracyReview a={a} attested={attests.accuracy?.labelling_id} />}
      <section>
        <h2>Counters of cycle {attests.cycle}</h2>
        <Stats values={counters} />
      </section>
      <Exclusion a={a} />
      <section>
        <h2>Known-diff rules</h2>
        <p className="muted">Approving accepts each rule and the keys it suppressed.</p>
        <Table rows={attests.rules} cols={RULE_COLS} rowId={(r) => r.rule_id} />
      </section>
      <section>
        <h2>Root causes</h2>
        <p className="muted">Approving accepts each unsuppressed discrepancy class; every claim is observed.</p>
        <Table rows={attests.root_causes} cols={CAUSE_COLS} rowId={(c) => c.root_cause_id} />
      </section>
    </>
  );
}
