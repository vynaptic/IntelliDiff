/** Schedules, open to every role: what a schedule never tells, the banners, and each
 *  schedule with what it runs, its cron and zone, its next due time and its newest firing; and one schedule with
 *  its definition, who saved it, what the four-eyes gate says, its next times and its firings. Operators add one,
 *  preview a cron's times, edit, pause, resume and delete one, and move a pinned one to the newer version. Only a
 *  worker fires, and every firing's text, banner, warning and note is the API's. The server still decides. */
import { useState, type FormEvent } from 'react';
import { api, type Firing, type Preview, type SavedSchedule, type Schedule, type ScheduleDetail } from './api';
import { href, message, useLoad, usePaged, usePoll, useWho } from './lib';
import {
  AUTH_MODES, HOURS, NOT_A_FORECAST, PRESETS, WEEKDAYS, blank, bodyOf, cronOf, defaultZone, draftOf, dueLine, gateLine,
  listed, movedTo, specLabel, type Draft,
} from './scheduling';
import { Badge, Err, Named, Table, when, type Col } from './ui';

const POLL_MS = 60_000;

/** A firing as the server words it, with its run's status and a link to the run it names. */
const Fired = ({ text, run_id, run_status }: { text: string; run_id: string | null; run_status?: string | null }) => (
  <>
    {text} <Badge v={run_status} />
    {run_id && <> <a href={href(['runs', run_id])}>run</a></>}
  </>
);

/** The operator's form: a new schedule (of the version `specId`, if given), or the stored one `from`, saved again
 *  at the revision it was loaded at. */
function ScheduleForm({ from, specId, onSaved }: { from?: Schedule; specId?: string; onSaved: (saved: SavedSchedule) => void }) {
  const specs = useLoad(api.specs, []);
  const zones = useLoad(api.scheduleZones, []);
  const [d, setD] = useState<Draft>(() => (from ? draftOf(from) : blank(specId)));
  const [preview, setPreview] = useState<Preview>();
  const [error, setError] = useState<string>();
  if (!specs.data || !zones.data) return <Err error={specs.error ?? zones.error} />;
  const items = specs.data.items;
  const spec_id = d.spec_id || String(items[0]?.spec_id ?? '');
  const zone = defaultZone(zones.data, d.time_zone || Intl.DateTimeFormat().resolvedOptions().timeZone, !!d.time_zone);

  const set = (fields: Partial<Draft>) => {
    setD((was) => ({ ...was, ...fields }));
    setPreview(undefined); // of the form as it was
  };
  // Custom starts from the text the preset wrote.
  const pick = (preset: Draft['preset']) => set({ preset, cron: preset === 'custom' ? cronOf(d) : d.cron });
  const look = () => {
    setError(undefined);
    api.previewSchedule(cronOf(d), zone.zone).then(setPreview, (e) => setError(message(e)));
  };

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(undefined);
    const body = bodyOf({ ...d, spec_id }, items, zone.zone);
    try {
      onSaved(from ? await api.saveSchedule(from.schedule_id, { ...body, revision: from.revision }) : await api.createSchedule(body));
    } catch (err) {
      setError(message(err));
    }
  }

  return (
    <form className="panel" onSubmit={submit}>
      <h2>{from ? `Edit ${from.name}` : 'New schedule'}</h2>
      <div className="inline">
        <label>
          Name{' '}
          <input value={d.name} onChange={(e) => set({ name: e.target.value })} required maxLength={63} size={24} placeholder="nightly-customers" />
        </label>
        <label>
          Spec{' '}
          <select value={spec_id} onChange={(e) => set({ spec_id: e.target.value })} required>
            {items.map((s) => <option key={s.spec_id} value={s.spec_id}>{`${s.name} ${s.version} (#${s.spec_id})`}</option>)}
          </select>
        </label>
        <label>
          <input type="checkbox" checked={d.follow} onChange={(e) => set({ follow: e.target.checked })} /> follow its name: the
          newest stored version at each firing
        </label>
        <label>
          Mode{' '}
          <select value={d.mode} onChange={(e) => set({ mode: e.target.value as Draft['mode'] })}>
            <option>batch</option>
            <option>soak</option>
          </select>
        </label>
        {d.mode === 'soak' && (
          <label>
            <input type="checkbox" checked={d.rolling} onChange={(e) => set({ rolling: e.target.checked })} /> resolve the keys
            again each cycle
          </label>
        )}
      </div>
      <div className="inline">
        <label>
          Runs{' '}
          <select value={d.preset} onChange={(e) => pick(e.target.value as Draft['preset'])}>
            {Object.entries(PRESETS).map(([preset, label]) => <option key={preset} value={preset}>{label}</option>)}
          </select>
        </label>
        {d.preset === 'weekly' && (
          <label>
            on{' '}
            <select value={d.weekday} onChange={(e) => set({ weekday: e.target.value })}>
              {WEEKDAYS.map((day, i) => <option key={day} value={i}>{day}</option>)}
            </select>
          </label>
        )}
        {d.preset === 'monthly' && (
          <label>
            on day <input type="number" min={1} max={28} required value={d.day} onChange={(e) => set({ day: e.target.value })} />
          </label>
        )}
        {d.preset === 'hours' && (
          <label>
            N{' '}
            <select value={d.hours} onChange={(e) => set({ hours: e.target.value })}>
              {HOURS.map((n) => <option key={n}>{n}</option>)}
            </select>
          </label>
        )}
        {!['hours', 'custom'].includes(d.preset) && (
          <label>
            at <input type="time" required value={d.time} onChange={(e) => set({ time: e.target.value })} />
          </label>
        )}
        {d.preset === 'custom' ? (
          <label>
            Cron{' '}
            <input value={d.cron} onChange={(e) => set({ cron: e.target.value })} required maxLength={100} size={24}
              placeholder="minute hour day-of-month month day-of-week" spellCheck={false} />
          </label>
        ) : (
          <code>{cronOf(d)}</code>
        )}
        <label>
          Time zone{' '}
          <select value={zone.zone} onChange={(e) => set({ time_zone: e.target.value })} required>
            {!zone.zone && <option value="">choose a zone</option>}
            {zones.data.zones.map((z) => <option key={z}>{z}</option>)}
          </select>
        </label>
      </div>
      {zone.note && <p className="warn">{zone.note}</p>}
      <div className="inline">
        <label>
          Start within{' '}
          <input type="number" min={15} max={1440} required value={d.start_within} onChange={(e) => set({ start_within: e.target.value })} />{' '}
          min of its time
        </label>
        <label>
          Ends <input value={d.ends_at} onChange={(e) => set({ ends_at: e.target.value })} size={28} placeholder="never, or 2026-12-31T00:00Z" />
        </label>
        <button type="button" onClick={look}>Preview</button>
        <button>{from ? 'Save' : 'Add'}</button>
      </div>
      <Err error={error} />
      {preview && (
        <>
          <p className="muted">
            Its next {preview.upcoming.length} times by tz data {preview.tzdata} ({NOT_A_FORECAST}):
          </p>
          <ol>{preview.upcoming.map((due) => <li key={due.utc}>{dueLine(due, zone.zone)}</li>)}</ol>
          {preview.notes.map((note) => <p key={note} className="warn">{note}</p>)}
        </>
      )}
    </form>
  );
}

const Warnings = ({ lines }: { lines?: string[] }) => <>{lines?.map((line) => <p key={line} className="warn">{line}</p>)}</>;

export function SchedulesScreen({ query }: { query: URLSearchParams }) {
  const { can } = useWho();
  const [pages, setPages] = useState(1);
  const found = useLoad(() => listed((cursor) => api.schedules({ cursor }), pages), [pages]);
  const [saved, setSaved] = useState<SavedSchedule>();
  usePoll(found.reload, POLL_MS, true);
  const d = found.data;

  const cols: Col<Schedule>[] = [
    ['Name', (s) => <a href={href(['schedules', s.schedule_id])}>{s.name}</a>],
    ['Spec and version', (s) => specLabel(s)],
    ['Mode', (s) => s.run.mode],
    ['Cron and zone', (s) => <><code>{s.cron}</code> {s.time_zone}</>],
    [`Next due (${NOT_A_FORECAST})`, (s) => s.next_due && dueLine(s.next_due, s.time_zone)],
    ['Last firing', (s) => s.last && <Fired {...s.last} />],
    ['State', (s) => <Badge v={s.state} />],
  ];
  return (
    <>
      <h1>Schedules</h1>
      <Err error={found.error} />
      {d && (
        <>
          <ul className="muted">{d.not.map((line) => <li key={line}>{line}</li>)}</ul>
          {d.banners.map((line) => <p key={line} className="banner" role="note">{line}</p>)}
          <Table rows={d.items} cols={cols} rowId={(s) => s.schedule_id} />
          {d.next_cursor && <button onClick={() => setPages(pages + 1)}>More</button>}
        </>
      )}
      {saved && (
        <p role="status">
          Added <a className="mono" href={href(['schedules', saved.schedule_id])}>{saved.schedule_id}</a> ({saved.name}).
        </p>
      )}
      <Warnings lines={saved?.warnings} />
      {can('operator') && (
        <ScheduleForm
          key={`${saved?.schedule_id}/${query.get('spec_id')}`}
          specId={query.get('spec_id') ?? undefined}
          onSaved={(s) => {
            setSaved(s);
            found.reload();
          }}
        />
      )}
    </>
  );
}

const FIRING_COLS: Col<Firing>[] = [
  ['Recorded', (f) => when(f.at)],
  ['What happened', (f) => <Fired text={f.text} run_id={f.run_id} />],
  ['Audit row', (f) => f.seq],
];

/** A schedule's firings, newest first, each in the server's words; read again when the schedule moves on. */
function Firings({ id, version }: { id: string; version: string }) {
  const firings = usePaged((cursor) => api.scheduleFirings(id, { cursor }), [id, version]);
  return (
    <>
      <h2>Firings</h2>
      <Err error={firings.error} />
      <Table rows={firings.items} cols={FIRING_COLS} rowId={(f) => `seq-${f.seq}`} />
      {firings.more && <button onClick={firings.more}>More</button>}
    </>
  );
}

export function ScheduleScreen({ id }: { id: string }) {
  const { can } = useWho();
  const found = useLoad(() => api.schedule(id), [id]);
  const [editing, setEditing] = useState<ScheduleDetail>(); // as it was loaded when Edit was pressed
  const [warnings, setWarnings] = useState<string[]>();
  const [error, setError] = useState<string>();
  usePoll(found.reload, POLL_MS, true);
  const d = found.data;
  const gate = d?.runs.approval;

  const act = (done: Promise<SavedSchedule | { deleted_at: string }>) => {
    setError(undefined);
    done.then((saved) => setWarnings('warnings' in saved ? saved.warnings : undefined), (e) => setError(message(e))).then(() => {
      setEditing(undefined);
      found.reload();
    });
  };
  return (
    <>
      <h1>
        Schedule {d?.name ?? id} <Badge v={d?.state} />
      </h1>
      <p><a href="#/schedules">All schedules</a></p>
      <Err error={found.error ?? error} />
      <Warnings lines={warnings} />
      {d && (
        <>
          <p className="muted">
            <span className="mono">{d.schedule_id}</span> · {specLabel(d)} · {d.run.mode}
            {d.run.soak?.rolling && ' (the keys resolved again each cycle)'} · <code>{d.cron}</code> {d.time_zone} · starts
            within {d.start_within} min of its time{d.ends_at && ` · ends ${when(d.ends_at)}`}
          </p>
          <p className="muted">
            Revision {d.revision}, saved by <Named name={d.set_by_name} actor={d.set_by} /> {when(d.set_at)} in{' '}
            {AUTH_MODES[d.auth_mode]}; created by <Named name={d.created_by_name} actor={d.created_by} /> {when(d.created_at)}
            {d.paused_at && <>; paused by <Named name={d.paused_by_name} actor={d.paused_by} /> {when(d.paused_at)}</>}
          </p>
          {gate && (
            <p className={gate.state === 'unapproved' ? 'warn' : 'muted'}>
              {gateLine(gate)}
              {gate.approval_id && <> <a href={href(['approvals', gate.approval_id])}>review</a></>}
            </p>
          )}
          {d.last && <p>Last firing: <Fired {...d.last} /></p>}
          {can('operator') && d.state !== 'deleted' && (
            <p className="inline">
              <button onClick={() => setEditing(editing ? undefined : d)}>Edit</button>
              {d.state === 'active' && <button onClick={() => act(api.pauseSchedule(d.schedule_id))}>Pause</button>}
              {d.state === 'paused' && <button onClick={() => act(api.resumeSchedule(d.schedule_id))}>Resume</button>}
              <button
                onClick={() => confirm(`Delete schedule ${d.name}? A time due now is recorded as missed; its runs and firings still name it.`) &&
                  act(api.deleteSchedule(d.schedule_id))}
              >
                Delete
              </button>
              {d.newer && (
                <button onClick={() => act(api.saveSchedule(d.schedule_id, movedTo(d, d.newer!.spec_id)))}>
                  Move to the newer version: {d.newer.version} (spec {d.newer.spec_id})
                </button>
              )}
            </p>
          )}
          {editing && <ScheduleForm from={editing} onSaved={(saved) => act(Promise.resolve(saved))} />}
          <h2>Next due ({NOT_A_FORECAST})</h2>
          {d.upcoming.length ? (
            <ol>{d.upcoming.map((due) => <li key={due.utc}>{dueLine(due, d.time_zone)}</li>)}</ol>
          ) : (
            <p className="muted">None.</p>
          )}
          <Firings id={d.schedule_id} version={`${d.revision}/${d.last?.at}`} />
        </>
      )}
    </>
  );
}
