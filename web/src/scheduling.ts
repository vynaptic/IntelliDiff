/** What the Schedules screens build besides the API's own texts (docs/mvp/SCHEDULES.md §10): the cron text a preset
 *  writes, the body a form sends, a schedule's spec and due-time labels in `ds schedules`' words, the zone a new
 *  schedule starts with, and the list read over its pages. Every firing's text, banner, warning and note is the
 *  API's: nothing here words one. The server still decides. */
// Type imports only, so that Node runs the tests of this module without a bundler.
import type { DueTime, Mode, Schedule, ScheduleGate, ScheduleIn, Schedules, Spec, Zones } from './api';

export const NOT_A_FORECAST = 'a schedule, not a forecast';
export const PRESETS = {
  daily: 'daily', weekdays: 'on weekdays', weekly: 'weekly', monthly: 'monthly', hours: 'every N hours', custom: 'custom cron',
};
export const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']; // cron's 0 to 6
export const HOURS = [1, 2, 3, 4, 6, 8, 12]; // the steps that divide a day evenly
export const AUTH_MODES: Record<Mode, string> = { oidc: 'SSO', tokens: 'tokens mode', none: 'developer mode' };

/** A schedule as its form holds it: every field a string or a switch, as the controls give them. */
export interface Draft {
  name: string;
  spec_id: string; // a stored version
  follow: boolean; // run the newest version of that version's name at each firing, not the version itself
  mode: 'batch' | 'soak';
  rolling: boolean;
  preset: keyof typeof PRESETS;
  time: string; // HH:MM of a daily, weekdays, weekly or monthly preset
  weekday: string; // 0 (Sunday) to 6
  day: string; // 1 to 28: a day every month has
  hours: string; // one of HOURS
  cron: string; // the custom text
  time_zone: string; // empty: the browser's zone (defaultZone)
  start_within: string;
  ends_at: string; // ISO-8601 with a UTC offset, or empty
}

/** A new schedule's form: a batch run of `spec_id` (a version, pinned) daily at 02:00. */
export const blank = (spec_id = ''): Draft => ({
  name: '', spec_id, follow: false, mode: 'batch', rolling: false, preset: 'daily', time: '02:00', weekday: '1', day: '1',
  hours: '6', cron: '', time_zone: '', start_within: '60', ends_at: '',
});

/** The form of a stored schedule, to edit it: its cron as custom text (a preset is never read back). */
export const draftOf = (s: Schedule): Draft => ({
  ...blank(String(s.spec_id ?? s.runs.spec_id ?? '')), name: s.name, follow: s.spec_id === null, mode: s.run.mode,
  rolling: !!s.run.soak?.rolling, preset: 'custom', cron: s.cron, time_zone: s.time_zone,
  start_within: String(s.start_within), ends_at: s.ends_at ?? '',
});

/** The cron text a form's preset writes: presets only write cron text, which the server reads as any other. */
export function cronOf(d: Pick<Draft, 'preset' | 'time' | 'weekday' | 'day' | 'hours' | 'cron'>): string {
  const [hour, minute] = d.time.split(':').map(Number);
  const at = `${minute} ${hour}`;
  switch (d.preset) {
    case 'daily': return `${at} * * *`;
    case 'weekdays': return `${at} * * 1-5`;
    case 'weekly': return `${at} * * ${d.weekday}`;
    case 'monthly': return `${at} ${d.day} * *`;
    case 'hours': return `0 */${d.hours} * * *`;
    default: return d.cron;
  }
}

/** What a form sends: the version pinned, or its name followed; never a key. */
export function bodyOf(d: Draft, specs: Pick<Spec, 'spec_id' | 'name'>[], time_zone: string): ScheduleIn {
  const spec_id = Number(d.spec_id);
  const spec = d.follow ? { spec_name: specs.find((s) => s.spec_id === spec_id)?.name ?? '' } : { spec_id };
  return {
    name: d.name, ...spec, run: d.mode === 'soak' ? { mode: 'soak', soak: { rolling: d.rolling } } : { mode: 'batch' },
    cron: cronOf(d), time_zone, start_within: Number(d.start_within), ends_at: d.ends_at.trim() || null,
  };
}

/** A stored schedule as a PUT takes it, running `spec_id` instead: the move to the newer version. */
export const movedTo = (s: Schedule, spec_id: number): ScheduleIn & { revision: number } => ({
  name: s.name, spec_id, run: s.run, cron: s.cron, time_zone: s.time_zone, start_within: s.start_within,
  ends_at: s.ends_at, revision: s.revision,
});

/** The zone a form starts with: `wanted` (the browser's, or a `stored` schedule's) when this build lists it, else
 *  the zone it was renamed to. Else, with a note saying so, UTC for the browser's and none for a stored one: its
 *  editor chooses, since another zone moves every time of its cron. */
export function defaultZone(z: Pick<Zones, 'zones' | 'renamed'>, wanted: string, stored = false): { zone: string; note?: string } {
  if (z.zones.includes(wanted)) return { zone: wanted };
  const renamed = Object.hasOwn(z.renamed, wanted) ? z.renamed[wanted] : undefined;
  if (renamed && z.zones.includes(renamed)) return { zone: renamed };
  if (stored) return { zone: '', note: `this build's zone data does not hold ${wanted}, the zone this schedule was saved with: choose its time zone` };
  return { zone: 'UTC', note: `your browser's time zone (${wanted}) is not in this build's zone data: UTC is selected` };
}

/** What a schedule runs, as `ds schedules` says it: its pinned version, or the name it follows and its newest. */
export function specLabel(s: Pick<Schedule, 'spec_id' | 'spec_name' | 'runs'>): string {
  const version = `${s.runs.version} (spec ${s.runs.spec_id})`;
  return s.spec_id !== null ? `pinned ${version}` : `follows ${s.spec_name}: ${version} now`;
}

/** A due time on its zone's clock and in UTC, as `ds schedules` prints it; UTC alone when the zone does not load. */
export const dueLine = (due: DueTime, time_zone: string) =>
  due.local ? `${due.local} ${time_zone} (${due.utc})` : due.utc;

/** What the four-eyes gate says now of the version a firing would run, and the refusal a firing would meet. */
export const gateLine = (g: Pick<ScheduleGate, 'state' | 'refusal'>) =>
  `Four-eyes approval: ${g.state.replace('_', ' ')}${g.refusal ? `: ${g.refusal}` : ''}`;

/** The schedules of each spec name, as the Specs screen lists them. */
export function bySpec(items: Schedule[]): Map<string, Schedule[]> {
  const found = new Map<string, Schedule[]>();
  for (const s of items) found.set(s.spec_name, [...(found.get(s.spec_name) ?? []), s]);
  return found;
}

/** The list's first `pages` pages as one answer: their schedules and their banners together. */
export async function listed(load: (cursor?: string) => Promise<Schedules>, pages: number): Promise<Schedules> {
  const found = await load();
  for (let n = 1; n < pages && found.next_cursor; n++) {
    const page = await load(found.next_cursor);
    found.items.push(...page.items);
    found.banners.push(...page.banners);
    found.next_cursor = page.next_cursor;
  }
  return found;
}
