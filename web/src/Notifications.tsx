/** Alerts and notifications (docs/mvp/ALERTS.md §11), open to every role: the feed's cursor, each channel with its
 *  counts (operators send a test, or send its failed deliveries again), the rules as declared, the active mutes
 *  (admins mute, and end them) and the deliveries, newest first. Channels and rules live in deltasleuth.yaml: no
 *  screen creates one. The server still decides. */
import { useState, type FormEvent } from 'react';
import { ApiError, api, type Delivery, type Mute, type NotifyChannel, type NotifyRule } from './api';
import { message, useLoad, usePaged, useWho } from './lib';
import { MUTE_NOTE, RULES_NOTE, canMute, cursorLine, external, ruleLine, testLine } from './notify';
import { Badge, Err, Named, Table, when, type Col } from './ui';

const STATES = ['pending', 'sent', 'dead', 'suppressed'];

export function NotificationsScreen() {
  const { me, can } = useWho();
  const found = useLoad(api.notifications, []);
  const [filter, setFilter] = useState({ channel: '', state: '' });
  const deliveries = usePaged((cursor) => api.deliveries({ ...filter, cursor }), [filter.channel, filter.state]);
  const [said, setSaid] = useState<string>();
  const [error, setError] = useState<string>();
  const [reauth, setReauth] = useState(false);
  const n = found.data;

  const act = (done: Promise<string>) => {
    setError(undefined);
    setSaid(undefined);
    done.then(
      (line) => {
        setSaid(line);
        found.reload();
      },
      (e) => setError(message(e)),
    );
  };

  async function mute(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const field = (name: string) => String(new FormData(form).get(name) ?? '');
    setError(undefined);
    setReauth(false);
    try {
      const scope = field('scope') === 'run' ? 'run' : 'channel';
      const made = await api.mute({ scope, target: field('target'), hours: Number(field('hours')), reason: field('reason') });
      setSaid(`Muted until ${when(made.until)} (${made.mute_id}).`);
      form.reset();
      found.reload();
    } catch (err) {
      if (err instanceof ApiError && err.status === 403 && err.message === 'reauth_required') setReauth(true);
      else setError(message(err));
    }
  }

  const channels: Col<NotifyChannel>[] = [
    ['Channel', (c) => <><span className="mono">{c.name}</span> {external(c) && <Badge v="external" />}</>],
    ['Kind', (c) => c.kind],
    ['Host', (c) => <span className="mono">{c.host}</span>],
    ['Route', (c) => (c.proxy ? 'via notify.proxy' : c.allow_private_network ? 'private network allowed' : '')],
    ['Pending', (c) => c.pending],
    ['Sent 24 h', (c) => c.sent_24h],
    ['Dead 24 h', (c) => (c.dead_24h ? <span className="warn">{c.dead_24h}</span> : 0)],
    ['Held back 24 h', (c) => c.suppressed_24h],
    ['Last', (c) => [c.last_error ?? (c.last_status ? 'ok' : ''), c.last_status].filter(Boolean).join(' ')],
    ['', (c) => can('operator') && (
      <span className="inline">
        <button onClick={() => act(api.testChannel(c.name).then((t) => testLine(c, t)))}>Send test</button>
        <button
          onClick={() => confirm(`Send the failed deliveries of the last 72 hours to ${c.name} again?`) &&
            act(api.retryChannel(c.name).then((r) => `${r.count} failed deliveries of ${c.name} are pending again.`))}
        >
          Send failed again
        </button>
      </span>
    )],
  ];
  const rules: Col<NotifyRule>[] = [['Rule', (r) => ruleLine(r)]];
  const mutes: Col<Mute>[] = [
    ['Mute', (m) => <span className="mono">{m.mute_id}</span>],
    ['Of', (m) => `${m.scope} ${m.target}`],
    ['Until', (m) => when(m.until)],
    ['By', (m) => <Named actor={m.created_by} />],
    ['Reason', (m) => m.reason],
    ['', (m) => canMute(me) && (
      <button onClick={() => act(api.unmute(m.mute_id).then(() => `Ended ${m.mute_id}.`))}>End</button>
    )],
  ];
  const sent: Col<Delivery>[] = [
    ['Delivery', (d) => <span className="mono">{d.delivery_id}</span>],
    ['Made', (d) => when(d.created_at)],
    ['Channel', (d) => d.channel],
    ['Event', (d) => d.event],
    ['Severity', (d) => d.severity],
    ['State', (d) => <><Badge v={d.state} />{d.why && ` (${d.why})`}</>],
    ['Attempts', (d) => d.attempts],
    ['Last', (d) => [d.last_error, d.last_status].filter(Boolean).join(' ')],
    ['Audit row', (d) => d.seq],
    ['Body', (d) => (
      <details>
        <summary className="mono">SHA-256 {d.body_sha256.slice(0, 12)}…</summary>
        <pre className="mono">{d.body}</pre>
      </details>
    )],
  ];
  return (
    <>
      <h1>Notifications</h1>
      <Err error={found.error ?? error} />
      {said && <p role="status">{said}</p>}
      {n && !n.enabled && <p>Notifications are off: add notify to deltasleuth.yaml and restart.</p>}
      {n?.enabled && (
        <>
          <p className="muted">{cursorLine(n)}</p>
          <h2>Channels</h2>
          <Table rows={n.channels} cols={channels} rowId={(c) => `channel-${c.name}`} />
          <h2>Rules</h2>
          <p className="muted">{RULES_NOTE}</p>
          <Table rows={n.rules} cols={rules} rowId={(r) => `rule-${r.name}`} />
          <h2>Mutes</h2>
          <Table rows={n.mutes} cols={mutes} rowId={(m) => m.mute_id} />
          {canMute(me) && (
            <form className="panel" onSubmit={mute}>
              <h2>Mute</h2>
              <p className="muted">{MUTE_NOTE}</p>
              <div className="inline">
                <label>
                  Of{' '}
                  <select name="scope">
                    <option value="channel">a channel</option>
                    <option value="run">a run</option>
                  </select>
                </label>
                <label>
                  Channel or run <input name="target" required size={30} list="notify-channels" placeholder="ops-hook or R-…" />
                </label>
                <datalist id="notify-channels">
                  {n.channels.map((c) => <option key={c.name} value={c.name} />)}
                </datalist>
                <label>
                  Hours <input name="hours" type="number" min={1} max={168} required size={5} defaultValue={4} />
                </label>
              </div>
              <textarea name="reason" required maxLength={1000} rows={2} placeholder="Why" />
              <button>Mute</button>
              {reauth && (
                <p className="inline">
                  <span className="warn">Sign in again to mute.</span>
                  <button
                    type="button"
                    onClick={() => location.assign('/v1/auth/login?reauth=1&next=' + encodeURIComponent('#/notifications'))}
                  >
                    Sign in again
                  </button>
                </p>
              )}
            </form>
          )}
          <h2>Deliveries</h2>
          <p className="inline">
            <select value={filter.channel} onChange={(e) => setFilter({ ...filter, channel: e.target.value })}>
              <option value="">every channel</option>
              {n.channels.map((c) => <option key={c.name}>{c.name}</option>)}
            </select>
            <select value={filter.state} onChange={(e) => setFilter({ ...filter, state: e.target.value })}>
              <option value="">every state</option>
              {STATES.map((s) => <option key={s}>{s}</option>)}
            </select>
          </p>
          <Err error={deliveries.error} />
          <Table rows={deliveries.items} cols={sent} rowId={(d) => d.delivery_id} />
          {deliveries.more && <button onClick={deliveries.more}>More</button>}
        </>
      )}
    </>
  );
}
