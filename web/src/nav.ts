/** The navigation bar's groups, the link that is current, the browser tab's title and the trail back from a
 *  screen of one thing, all from the address. The stored specs are "Comparisons" on the screens; their address
 *  stays `#/specs`.
 *  Pure, so that Node runs its tests: it loads no other module. */

const LINKS: Record<string, string> = {
  runs: 'Runs', specs: 'Comparisons', targets: 'Targets', schedules: 'Schedules', prechecks: 'Pre-checks',
  readiness: 'Readiness', approvals: 'Approvals', admin: 'Admin', notifications: 'Notifications', licence: 'Licence',
};

export interface NavLink { screen: string; label: string }

/** The links by what they are for: comparing, automating, deciding, and the installation. A group shows the
 *  screens `who` can use: approvals under single sign-on, the admin screen for an admin, the notifications
 *  where they are set up, and the licence where a build enforces one. */
export function navOf(who: { sso: boolean; admin: boolean; notifications: boolean; licensed: boolean }): NavLink[][] {
  const groups = [
    ['runs', 'specs', 'targets'],
    ['schedules', 'prechecks'],
    ['readiness', who.sso && 'approvals'],
    [who.admin && 'admin', who.notifications && 'notifications', who.licensed && 'licence'],
  ];
  return groups
    .map((group) => group.flatMap((screen) => (screen ? [{ screen, label: LINKS[screen] }] : [])))
    .filter((group) => group.length > 0);
}

/** The link an address belongs to; the runs list for an empty or unknown one, as the router shows it. */
export const currentOf = (parts: string[]): string => (Object.hasOwn(LINKS, parts[0] ?? '') ? parts[0] : 'runs');

const TITLES: Record<string, string> = { ...LINKS, prechecks: 'Table pre-checks', signin: 'Sign in' };

function page([screen, id, sub, key]: string[]): string {
  if (screen === 'runs' && id) return sub === 'keys' && key ? `Key ${key}` : `Run ${id}`;
  if (screen === 'specs' && id) return id === 'new' ? 'New comparison' : `Comparison #${id}`;
  if (screen === 'schedules' && id) return `Schedule ${id}`;
  if (screen === 'prechecks' && id) return `Table pre-check ${id}`;
  if (screen === 'readiness' && id) return sub === 'brief' ? `Cut-over brief of ${id}` : `Readiness of ${id}`;
  if (screen === 'approvals' && id) return `Approval ${id}`;
  return Object.hasOwn(TITLES, screen ?? '') ? TITLES[screen] : TITLES.runs;
}

/** The browser tab's title: the screen, or the one thing it shows, then the product. */
export const titleOf = (parts: string[]): string => `${page(parts)} - DeltaSleuth`;

export interface Crumb { label: string; href: string }

const at = (...parts: string[]) => `#/${parts.map(encodeURIComponent).join('/')}`;
const DETAILS = ['runs', 'specs', 'schedules', 'prechecks', 'readiness', 'approvals']; // screens that open one thing

/** The way back from a screen of one thing: its list, and for a key its run (at the cycle the key is shown
 *  for) and for a cut-over brief its pair. Nothing above a list. */
export function crumbsOf([screen, id, sub]: string[], query: Record<string, string>): Crumb[] {
  if (!DETAILS.includes(screen) || !id) return [];
  const list = { label: LINKS[screen], href: at(screen) };
  if (screen === 'runs' && sub === 'keys')
    return [list, { label: `Run ${id}`, href: at(screen, id) + (query.cycle ? `?cycle=${encodeURIComponent(query.cycle)}` : '') }];
  if (screen === 'readiness' && sub === 'brief') return [list, { label: id, href: at(screen, id) }];
  return [list];
}
