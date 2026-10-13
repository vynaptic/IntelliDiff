/** Runs: the list, newest first, and a form to start a batch or soak run. */
import { useState, type FormEvent } from 'react';
import { api, type Run } from './api';
import { refusal } from './licensing';
import { href, message, useLicence, useLoad, usePaged, useWho } from './lib';
import { Badge, Err, Table, when, type Col } from './ui';

const COLS: Col<Run>[] = [
  ['Run', (r) => <a href={href(['runs', r.run_id])}>{r.run_id}</a>],
  ['Spec', (r) => `${r.spec_name} ${r.spec_version}`],
  ['Mode', (r) => r.mode],
  ['Status', (r) => <Badge v={r.status} />],
  ['Cycle', (r) => r.cycle],
  ['Created', (r) => when(r.created_at)],
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
      <Table rows={runs.items} cols={COLS} rowId={(r) => r.run_id} />
      {runs.more && <button onClick={runs.more}>More</button>}
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
            <option>batch</option>
            <option>soak</option>
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
      <button disabled={!!refused} title={refused}>Start</button>
      <Err error={error ?? specs.error} />
    </form>
  );
}
