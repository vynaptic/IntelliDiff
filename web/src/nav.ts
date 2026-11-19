/** The navigation bar's groups, the link that is current and the browser tab's title, all from the address.
 *  Pure, so that Node runs its tests: it loads no other module. */

const LINKS: Record<string, string> = {
  runs: 'Runs', specs: 'Specs', targets: 'Targets', schedules: 'Schedules', prechecks: 'Pre-checks',
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
  if (screen === 'specs' && id) return id === 'new' ? 'New comparison' : `Spec #${id}`;
  if (screen === 'schedules' && id) return `Schedule ${id}`;
  if (screen === 'prechecks' && id) return `Table pre-check ${id}`;
  if (screen === 'readiness' && id) return sub === 'brief' ? `Cut-over brief of ${id}` : `Readiness of ${id}`;
  if (screen === 'approvals' && id) return `Approval ${id}`;
  return Object.hasOwn(TITLES, screen ?? '') ? TITLES[screen] : TITLES.runs;
}

/** The browser tab's title: the screen, or the one thing it shows, then the product. */
export const titleOf = (parts: string[]): string => `${page(parts)} - DeltaSleuth`;
