/** What the Notifications screen and every screen's banners say, in the words
 *  `ds notify` prints: the feed's cursor, a channel (a SaaS one badged external), a rule, a mute, a delivery and a
 *  test's answer; and who sees the Mute form. The server still decides. */
// Type imports only, so that Node runs the tests of this module without a bundler.
import type { Delivery, Me, Mute, Notifications, NotifyChannel, NotifyRule, Tested } from './api';

export const RULES_NOTE = 'Declared in deltasleuth.yaml (notify.rules); a change applies at the next restart.';
export const MUTE_NOTE =
  'A mute holds back a channel’s or a run’s notifications, stored as suppressed and never sent, for 1 to 168 hours;' +
  ' every screen says so until it ends. Its reason is audited and reaches the SIEM: never personal data.';
const num = (n: number) => n.toLocaleString('en-US');
const minute = (iso: string) => `${new Date(iso).toISOString().slice(0, 16).replace('T', ' ')} UTC`;

/** Slack and Teams deliveries leave the site's network for a SaaS service. */
export const external = (c: Pick<NotifyChannel, 'kind'>) => c.kind === 'slack' || c.kind === 'teams';

/** The banners every screen shows, viewers included, as the server words them; none while notifications are off. */
export const bannerLines = (n: Notifications | undefined) => (n?.enabled ? n.banners : []);

/** The feed's cursor, as `ds notify` prints it. */
export function cursorLine(n: Pick<Notifications, 'label' | 'cursor'>): string {
  const c = n.cursor;
  if (!c) return 'Notifications are off.';
  const unread = c.behind ? `; unread since ${minute(c.behind)}` : '';
  return `label ${n.label}: audit row ${c.seq ?? '-'} of ${c.head} notified${unread}`;
}

/** A delivery's or a channel's newest attempt: its error (or ok) and its HTTP or SMTP status. */
function last(x: { last_error: string | null; last_status: number | null }): string {
  if (!x.last_error && !x.last_status) return '';
  return `last ${x.last_error ?? 'ok'}${x.last_status ? ` (${x.last_status})` : ''}`;
}

/** A channel's counts, as `ds notify` prints them. */
export function channelLine(c: NotifyChannel): string {
  const tail = last(c);
  return (
    `channel ${c.name} (${c.kind}, ${c.host}): ${num(c.pending)} pending; in 24 h ${num(c.sent_24h)} sent, ` +
    `${num(c.dead_24h)} dead, ${num(c.suppressed_24h)} held back${tail ? `; ${tail}` : ''}`
  );
}

/** A rule as declared: its events, its channels and its filters. */
export function ruleLine(r: NotifyRule): string {
  const filters = [
    r.specs && `specs ${r.specs.join(', ')}`,
    `modes ${r.modes.join(', ')}`,
    r.when && `when ${Object.entries(r.when).filter(([, v]) => v !== null).map(([k, v]) => `${k} ${v}`).join(', ')}`,
    r.reasons && `reasons ${r.reasons.join(', ')}`,
    r.states && `states ${r.states.join(', ')}`,
    r.statuses && `statuses ${r.statuses.join(', ')}`,
  ].filter(Boolean);
  return `${r.name}: ${r.events.join(', ')} to ${r.channels.join(', ')} (${filters.join('; ')})`;
}

/** An active mute, as `ds notify` lists it. */
export const muteLine = (m: Mute) =>
  `${m.mute_id}: ${m.scope} ${m.target} until ${minute(m.until)} by ${m.created_by}: ${m.reason}`;

/** A delivery, as `ds notify deliveries` lists it. */
export function deliveryLine(d: Delivery): string {
  const tail = last(d);
  return (
    `${d.delivery_id}: ${d.channel} ${d.event} ${d.severity} ${d.state}${d.why ? ` (${d.why})` : ''}, ` +
    `${d.attempts} attempt(s)${d.seq ? `, audit row ${d.seq}` : ''}${d.run_id ? `, run ${d.run_id}` : ''}` +
    (tail ? `, ${tail}` : '')
  );
}

/** A test's answer, as `ds notify test` prints it (its error line without `error: `). */
export function testLine(channel: Pick<NotifyChannel, 'name' | 'kind'>, t: Tested): string {
  const protocol = channel.kind === 'email' ? 'SMTP' : 'HTTP';
  if (t.state !== 'sent') {
    return `${channel.name}: ${t.error}${t.status ? ` (${protocol} ${t.status})` : ''} (${t.delivery_id})`;
  }
  return `sent to ${channel.name}: ${protocol} ${t.status}${t.error ? `, ${t.error}` : ''} in ${t.latency_ms} ms (${t.delivery_id})`;
}

/** Whether the Mute form shows: to an admin, never a token (no token holds admin; a mute needs a person signed in
 *  within 15 minutes, or developer mode). */
export const canMute = (me: Pick<Me, 'roles' | 'kind'> | undefined) =>
  !!me && me.roles.includes('admin') && me.kind !== 'token';
