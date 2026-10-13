/** A key of a run: its result, side-by-side masked canonical A/B, findings and observations. `?cite=` names the
 *  finding or observation an AI draft cited, whose row is outlined. */
import { useEffect, useState } from 'react';
import { api, type KeyDetail, type Observation } from './api';
import { Chat, reveal } from './Chat';
import { DocDiff } from './DocDiff';
import { findingCols } from './Run';
import { refusal } from './licensing';
import { href, message, useLicence, useLoad, useWho } from './lib';
import { chatClosed } from './retention';
import { Badge, Err, Stats, Table, when, type Col } from './ui';

const OBS_COLS: Col<Observation>[] = [
  ['Observation', (o) => <span className="mono">{o.obs_id}</span>],
  ['Side', (o) => o.side.toUpperCase()],
  ['Pass', (o) => o.pass_no],
  ['HTTP', (o) => o.http_status],
  ['Latency ms', (o) => o.latency_ms],
  ['Attempts', (o) => o.attempts],
  ['Pages', (o) => o.pages],
  ['Error', (o) => o.error_class],
  ['Observed', (o) => when(o.observed_at)],
];

export function KeyScreen({ runId, keyMasked, query }: { runId: string; keyMasked: string; query: URLSearchParams }) {
  const cycle = query.get('cycle') ?? undefined;
  const detail = useLoad(() => api.key(runId, keyMasked, cycle), [runId, keyMasked, cycle]);
  const run = useLoad(() => api.run(runId), [runId]);
  const ai = useLoad(api.health, []).data?.ai;
  const cite = query.get('cite');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [now, setNow] = useState<KeyDetail>();
  const { can } = useWho();
  const refused = refusal(useLicence(), 'runs');

  useEffect(() => {
    if (cite && detail.data) reveal(cite);
  }, [cite, detail.data]);

  async function compareNow() {
    setBusy(true);
    setError(undefined);
    try {
      setNow(await api.compare(run.data!.spec_id, runId, keyMasked));
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }

  const d = detail.data;
  return (
    <>
      <p>
        <a href={href(['runs', runId], { cycle })}>Run {runId}</a>
      </p>
      <h1>
        Key {keyMasked} <Badge v={d?.result?.outcome} />
      </h1>
      {can('operator') && (
        <button onClick={compareNow} disabled={!run.data || busy || !!refused} title={refused}>
          {busy ? 'Comparing…' : 'Compare now'}
        </button>
      )}
      <Err error={error ?? detail.error ?? run.error} />
      {now && (
        <section className="panel">
          <h2>
            Compared now (<a href={href(['runs', now.run_id, 'keys', keyMasked])}>run {now.run_id}</a>){' '}
            <Badge v={now.result?.outcome} />
          </h2>
          <Detail d={now} />
        </section>
      )}
      {d && <Detail d={d} />}
      {run.data && <Chat runId={runId} keyMasked={keyMasked} closed={chatClosed(run.data)} ai={ai} />}
    </>
  );
}

export function Detail({ d }: { d: KeyDetail }) {
  const r = d.result;
  const facts = {
    cycle: d.cycle,
    state: d.state,
    pass: d.pass_no,
    ...(r && {
      'first outcome': r.first_outcome,
      confirmation: r.confirmation,
      revision: r.revision,
      'side A': [r.side_a_state, r.side_a_reason].filter(Boolean).join(': '),
      'side B': [r.side_b_state, r.side_b_reason].filter(Boolean).join(': '),
      why: r.inconclusive_reason ?? r.drift_reason,
      suppressed: r.suppressed_count,
      ...r.attrs,
    }),
  };
  return (
    <>
      <Stats values={facts} />
      <h2>Canonical A and B (masked)</h2>
      <DocDiff a={d.canonical.a} b={d.canonical.b} findings={d.findings} />
      <h2>Findings</h2>
      <Table rows={d.findings} cols={findingCols()} rowId={(f) => f.finding_id} />
      <h2>Observations</h2>
      <Table rows={d.observations} cols={OBS_COLS} rowId={(o) => o.obs_id} />
    </>
  );
}
