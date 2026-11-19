/** Runs: the list, newest first, each with its keys by outcome as a bar and its result in a line, and a form to
 *  start a batch or soak run. */
import { useState, type FormEvent } from 'react';
import { api, type Run } from './api';
import { shortId } from './format';
import { label } from './labels';
import { refusal } from './licensing';
import { href, message, useLicence, useLoad, usePaged, useWho } from './lib';
import { resultLine } from './outcomes';
import { Badge, Err, MoreRows, OutcomeBar, Table, When, type Col } from './ui';

const byOutcome = (r: Run) => r.counters.by_outcome as Record<string, number> | undefined;

const COLS: Col<Run>[] = [
  ['Run', (r) => <a className="mono" href={href(['runs', r.run_id])} title={r.run_id}>{shortId(r.run_id)}</a>],
  ['Spec', (r) => `${r.spec_name} ${r.spec_version}`],
  ['Mode', (r) => label(r.mode)],
  ['Status', (r) => <Badge v={r.status} />],
  ['Result', (r) => (
    <>
      <OutcomeBar byOutcome={byOutcome(r)} small />
      {resultLine(r.counters)}
    </>
  )],
  ['Cycle', (r) => r.cycle],
  ['Created', (r) => <When at={r.created_at} />],
];

export function Runs() {
  const runs = usePaged(api.runs, []);
  const { can } = useWho();
  return (
    <>
      <h1>Runs</h1>
      <p className="muted">Open a run to see its differences and ask AI about them.</p>
      {can('operator') && <NewRun />}
      <Err error={runs.error} />
      <Table
        rows={runs.items}
        cols={COLS}
        rowId={(r) => r.run_id}
        loading={runs.loading}
        empty={can('operator') ? 'No runs yet. A run needs a stored spec: create one under "Specs", then start a run with the form above.' : 'No runs yet.'}
      />
      <MoreRows shown={runs.items.length} more={runs.more} />
    </>
  );
}

function NewRun() {
  const specs = useLoad(api.specs, []);
  const [mode, setMode] = useState('batch');
  const [error, setError] = useState<string>();
  const refused = refusal(useLicence(), 'runs');

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const keys = String(form.get('keys')).split('\n').map((k) => k.trim()).filter(Boolean);
    try {
      const { run_id } = await api.createRun({
        spec_id: Number(form.get('spec_id')),
        mode,
        keys: keys.length ? keys : undefined,
        soak: mode === 'soak' ? { rolling: form.has('rolling') } : undefined,
      });
      location.hash = href(['runs', run_id]);
    } catch (err) {
      setError(message(err));
    }
  }

  return (
    <form className="panel" onSubmit={submit}>
      <h2>New run</h2>
      <div className="inline">
        <label>
          Spec{' '}
          <select name="spec_id" required>
            {specs.data?.items.map((s) => (
              <option key={s.spec_id} value={s.spec_id}>{`${s.name} ${s.version} (#${s.spec_id})`}</option>
            ))}
          </select>
        </label>
        <label>
          Mode{' '}
          <select value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="batch">Batch</option>
            <option value="soak">Soak</option>
          </select>
        </label>
        {mode === 'soak' && (
          <label>
            <input type="checkbox" name="rolling" /> resolve the keys again each cycle
          </label>
        )}
      </div>
      <label>
        Keys, one per line (none: the spec's key source; a table pair without one scans both tables)
        <textarea name="keys" rows={4} />
      </label>
      <p className="muted">A multi-part key: its parts joined by |</p>
      <button className="primary" disabled={!!refused} title={refused}>Start run</button>
      <Err error={error ?? specs.error} />
    </form>
  );
}
