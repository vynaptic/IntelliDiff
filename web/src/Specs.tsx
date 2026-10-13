/** Specs: the stored specs with their four-eyes approval and their name's schedules, a YAML editor that validates
 *  and saves, and drafting from sample pairs, with the draft's notes, unmatched paths and evidence under the
 *  form. Under SSO a spec author requests a version's approval here (docs/mvp/APPROVALS.md §6.3); an operator
 *  opens the schedule form of a version (docs/mvp/SCHEDULES.md §10) and the table pre-check form of one
 *  (docs/mvp/PRECHECKS.md §11). A version's name opens its stored document: read-only, or for a spec author in
 *  the builder, which "New comparison" opens on a starter (docs/mvp/CONTRACTS.md §7). */
import { useState, type FormEvent } from 'react';
import { ApiError, api, parseExact, type Draft, type Issue, type Pair, type Saved, type Spec } from './api';
import { Builder } from './Builder';
import { STARTER } from './building';
import { href, message, useLoad, useWho } from './lib';
import { bySpec, dueLine, listed, specLabel } from './scheduling';
import { Badge, Err, ISSUE_COLS, Named, Table, cell, when, type Col } from './ui';

const SPEC_COLS: Col<Spec>[] = [
  ['Id', (s) => s.spec_id],
  ['Name', (s) => <a href={href(['specs', s.spec_id])}>{s.name}</a>],
  ['Version', (s) => s.version],
  ['Hash', (s) => <span className="mono">{s.content_hash.slice(0, 12)}</span>],
  ['Created', (s) => when(s.created_at)],
  ['Approval', (s) => s.approval && (
    <>
      <a className="mono" href={href(['approvals', s.approval.approval_id])}>{s.approval.approval_id}</a>{' '}
      <Badge v={s.approval.status} /> {s.approval.scopes.join(', ')}
    </>
  )],
];

/** What an upload's answer says of its approval under SSO. */
function gated(saved: Saved): string {
  if (saved.approval_id) return ` It is approved: ${saved.approval_id}.`;
  if (!saved.needs.length) return ' Its runs need no four-eyes approval.';
  return ` Its runs need a four-eyes approval of ${saved.needs.join(', ')}: request one in the list above.`;
}

export function Specs() {
  const specs = useLoad(api.specs, []);
  // Every schedule an installation holds (200) is in four pages.
  const schedules = useLoad(() => listed((cursor) => api.schedules({ cursor }), 4), []);
  const scheduled = bySpec(schedules.data?.items ?? []);
  const [yaml, setYaml] = useState('');
  const [issues, setIssues] = useState<Issue[]>();
  const [status, setStatus] = useState<string>();
  const [error, setError] = useState<string>();
  const { can, me } = useWho();
  const sso = me?.mode === 'oidc';

  async function request(s: Spec) {
    const note = prompt(`Ask for a four-eyes approval of ${s.name} ${s.version} (spec #${s.spec_id}). A note for the approver (optional):`);
    if (note === null) return;
    setError(undefined);
    try {
      location.hash = href(['approvals', (await api.requestApproval(s.spec_id, note || undefined)).approval_id]);
    } catch (e) {
      setError(message(e));
    }
  }

  const open = (s: Spec) => !s.approval || ['rejected', 'revoked'].includes(s.approval.status);
  const cols: Col<Spec>[] = [
    ...SPEC_COLS,
    ['Schedules', (s) => (
      <>
        {scheduled.get(s.name)?.map((x) => (
          <div key={x.schedule_id}>
            <a href={href(['schedules', x.schedule_id])}>{x.name}</a> {specLabel(x)}:{' '}
            {x.next_due ? `next ${dueLine(x.next_due, x.time_zone)}` : x.state}
          </div>
        ))}
        {can('operator') && <a href={href(['schedules'], { spec_id: s.spec_id })}>Schedule…</a>}
      </>
    )],
  ];
  if (can('operator')) cols.push(['Table pre-check', (s) => <a href={href(['prechecks'], { spec_id: s.spec_id })}>Pre-check…</a>]);
  if (sso && can('spec_author')) cols.push(['', (s) => open(s) && <button onClick={() => request(s)}>Request approval</button>]);

  async function check(action: 'validate' | 'save') {
    setIssues(undefined);
    setStatus(undefined);
    try {
      if (action === 'validate') {
        const { issues } = await api.validateSpec(yaml);
        setIssues(issues);
        if (!issues.length) setStatus('No issues.');
      } else {
        const saved = await api.saveSpec(yaml);
        setStatus(`Saved as spec #${saved.spec_id}.${sso ? gated(saved) : ''}`);
        specs.reload();
      }
    } catch (e) {
      setIssues(e instanceof ApiError ? (e.body as { issues?: Issue[] } | null)?.issues : undefined);
      setStatus(message(e));
    }
  }

  return (
    <>
      <h1>Specs</h1>
      {can('spec_author') && <p><a href="#/specs/new">New comparison</a></p>}
      <Err error={error ?? specs.error ?? schedules.error} />
      <Table rows={specs.data?.items ?? []} cols={cols} rowId={(s) => String(s.spec_id)} />
      {can('spec_author') && (
        <>
          <section className="panel">
            <h2>Upload a spec</h2>
            <input type="file" accept=".yaml,.yml,.json" onChange={(e) => e.target.files?.[0]?.text().then(setYaml)} />
            <textarea rows={16} value={yaml} onChange={(e) => setYaml(e.target.value)} spellCheck={false} />
            <p className="inline">
              <button onClick={() => check('validate')} disabled={!yaml}>Validate</button>
              <button onClick={() => check('save')} disabled={!yaml}>Save</button>
              <span>{status}</span>
            </p>
            {issues && <Table rows={issues} cols={ISSUE_COLS} />}
          </section>
          <Profile
            onDraft={(d) => {
              setYaml(d.yaml);
              setIssues(d.issues);
              setStatus(undefined);
            }}
          />
        </>
      )}
    </>
  );
}

/** A stored spec: what it is, who stored it and its stored document, as the API renders it (parsed, its inline
 *  keys masked; the upload's own text is not kept): as YAML, read-only, or for a spec author in the builder, whose
 *  saves are new versions. `new` is a comparison not stored yet, built from the starter. */
export function SpecScreen({ id }: { id: string }) {
  const author = useWho().can('spec_author');
  const fresh = id === 'new';
  const found = useLoad(async () => (fresh ? undefined : api.spec(id, author)), [id, author]);
  const s = found.data;
  // Masked inline keys cannot be saved back: a list of business codes is shown as it is.
  const standIns = !!s?.body.key.source?.keys && (s.body.key.class ?? 'business_code') !== 'business_code';
  return (
    <>
      <h1>{fresh ? 'New comparison' : s ? `${s.name} ${s.version}` : `Spec #${id}`}</h1>
      <p><a href="#/specs">All specs</a></p>
      <Err error={found.error} />
      {s && (
        <>
          <p className="muted">
            Spec #{s.spec_id} · content hash <span className="mono">{s.content_hash}</span> · stored {when(s.created_at)} by{' '}
            {s.created_by ? <Named name={s.created_by_name} actor={s.created_by} /> : 'an unrecorded actor'}
          </p>
          {s.form ? <Builder id={id} form={s.form} standIns={standIns} /> : <pre className="panel">{s.yaml}</pre>}
        </>
      )}
      {fresh && (author ? <Builder id={id} form={STARTER} /> : <p className="muted">A new comparison needs the spec_author role.</p>)}
    </>
  );
}

/** Pairs from a JSON array of {key, a, b} or JSON Lines of {key, side, status, body} sample records.
 *  As in `ds profile`, a side has a document only with a 2xx JSON body (a 404 body is not one). */
function readPairs(text: string): Pair[] {
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

function Profile({ onDraft }: { onDraft: (draft: Draft) => void }) {
  const targets = useLoad(api.targets, []);
  const [draft, setDraft] = useState<Draft>();
  const [error, setError] = useState<string>();

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const field = (name: string) => String(form.get(name));
    setError(undefined);
    try {
      const pairs = readPairs(await (form.get('samples') as File).text());
      const d = await api.profile({
        pairs,
        spec_name: field('spec_name'),
        target_a: field('target_a'),
        target_b: field('target_b'),
        key_name: field('key_name'),
      });
      setDraft(d);
      onDraft(d);
    } catch (err) {
      setError(message(err));
    }
  }

  const evidence = draft?.evidence ?? [];
  const cols = Object.keys(evidence[0] ?? {}).map((k): Col<Record<string, unknown>> => [k, (row) => cell(row[k])]);
  return (
    <form className="panel" onSubmit={submit}>
      <h2>Draft a spec from sample pairs</h2>
      <p className="muted">A JSON array of {'{key, a, b}'}, or JSON Lines of {'{key, side, status, body}'} records (a side without a 2xx JSON body has no document). The draft and its issues open in the editor above.</p>
      <div className="inline">
        <input type="file" name="samples" accept=".json,.jsonl" required />
        {['spec_name', 'target_a', 'target_b', 'key_name'].map((name) => (
          <label key={name}>
            {name}{' '}
            <input name={name} required list={name.startsWith('target') ? 'targets' : undefined} />
          </label>
        ))}
        <datalist id="targets">
          {targets.data?.items.map((t) => <option key={t.target_id} value={t.target_id} />)}
        </datalist>
        <button>Draft</button>
      </div>
      <Err error={error ?? targets.error} />
      {draft && (
        <>
          <ul>{draft.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
          {(['a', 'b'] as const).map((side) => (
            <p key={side}>
              unmatched[{side}]: {draft.unmatched[side].length ? <code>{draft.unmatched[side].join(', ')}</code> : 'none'}
            </p>
          ))}
          <Table rows={evidence} cols={cols} />
        </>
      )}
    </form>
  );
}
