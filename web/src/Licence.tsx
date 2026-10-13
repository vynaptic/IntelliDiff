/** The licence, open to every role: the installation id, the licence in force and
 *  its renewal, the state and its warnings, what it allows, the pairs counted with their seats, and who
 *  installed or withdrew each licence and why. Licences are installed on the host (ds licence install). In
 *  an open build it says that no licence is needed, with the id and what is allowed: no pair is
 *  counted, and only a licence an earlier vendor build stored is listed, as one that does not count. */
import { api, type CountedPair, type InstalledLicence } from './api';
import { allowsLine, installLine, licenceLine, namedLine, pairsLine } from './licensing';
import { useLoad } from './lib';
import { Badge, Err, Named, Table, when, type Col } from './ui';

const PAIRS: Col<CountedPair>[] = [
  ['Pair (spec name)', (p) => p.name],
  ['First run', (p) => when(p.first_at)],
  ['Targets', (p) => p.targets.join(', ')],
  ['Seat', (p) => (p.seated ? 'seated' : <span className="warn">no seat</span>)],
];
const INSTALLED: Col<InstalledLicence>[] = [
  ['Licence', (i) => <span className="mono">{i.licence_id}</span>],
  ['Tier', (i) => i.tier],
  ['Period', (i) => i.starts && `${i.starts} to ${i.ends}`],
  ['In force', (i) => (i.in_force ? 'yes' : '')],
  ['Installed', (i) => <><Named name={i.installed_by_name} actor={i.installed_by} /> {when(i.installed_at)}</>],
  ['Withdrawn', (i) => i.withdrawn_at && (
    <><Named name={i.withdrawn_by_name} actor={i.withdrawn_by} /> {when(i.withdrawn_at)}: {i.withdraw_reason}</>
  )],
  ['Does not count', (i) => i.problem && <span className="warn">{i.problem}</span>],
];

export function LicenceScreen() {
  const found = useLoad(api.licence, []);
  const l = found.data;
  const open = l?.state === 'open';
  return (
    <>
      <h1>
        Licence <Badge v={l?.state} />
      </h1>
      <Err error={found.error} />
      {l && (
        <>
          <p>
            Installation <span className="mono">{l.installation_id ?? '-'}</span>
          </p>
          <p>{namedLine(l)}</p>
          {l.renewal && <p>Renewal {licenceLine(l.renewal)}</p>}
          {l.warnings.map((line) => (
            <p key={line} className="warn">{line}</p>
          ))}
          {l.allows && <p>Allows: {allowsLine(l.allows)}</p>}
          {l.pairs && !open && (
            <section>
              <h2>Pairs</h2>
              <p className="muted">{pairsLine(l.pairs)}</p>
              <Table rows={l.pairs.counted} cols={PAIRS} rowId={(p) => `pair-${p.name}`} />
            </section>
          )}
          {(!open || l.installed.length > 0) && (
            <>
              <h2>Licences installed</h2>
              <Table rows={l.installed} cols={INSTALLED} rowId={(i) => i.licence_id} />
            </>
          )}
          <p className="muted">{installLine(l)}</p>
        </>
      )}
    </>
  );
}
