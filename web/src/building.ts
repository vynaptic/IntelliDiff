/** What the comparison builder and the Targets screen build in the browser (docs/mvp/CONTRACTS.md §7): the starter
 *  of a new comparison, each side's request as its rows hold it, the form a validation, a save and a Send post, a
 *  pasted curl command read into those rows, the line that says what a Send met, an endpoint's form and the body
 *  it sends, and what stays in memory while a screen is left. A credential never enters a spec: the import sets
 *  each credential header aside for the target, by the loader's own rules. The server still decides. */
// The one import, with its extension, so that Node runs the tests of this module without a bundler.
import { isRaw, parseExact, stringifyExact, type AuthKind, type SpecForm, type Target, type TargetIn, type Tried } from './api.ts';

export type Side = 'a' | 'b';

/** A new comparison: the guide's smallest spec without the sides' target and request, under a scratch name, with a
 *  key pattern and a truncation rule that fit any service until they are changed. */
export const STARTER: SpecForm = {
  text: `apiVersion: deltasleuth/v1
kind: ComparisonSpec
metadata: {name: scratch-comparison, version: "0.1.0", owner: my-team}   # change: the pair's name, three numbers in quotes, your team
normalizerVersion: "1.1.0"
key:
  name: record_id                            # change: a label for the key
  type: string
  pattern: '^[^\\s/]{1,200}$'                 # change: an expression that fits your keys only
  source: {kind: list, keys: [change-me]}    # change: one key of yours, or more
sides:                                       # each side's target and request are the forms above
  a:                                         # side a is the old system, side b the new one
    role: legacy
    truncation: {itemsPath: "$", cap: 1000}  # change: where the answer's list is, and the most the service returns
  b:
    role: new
    truncation: {itemsPath: "$", cap: 1000}  # change
compare: {}
recheck: {delay: 1s, times: 1}
`,
  sides: { a: {}, b: {} },
};

export const PLACEHOLDERS = '{{ key }}, {{ keyParts.<name> }}'; // what a path, a query, a header and a body may hold
export const NOT_JSON = 'The body is not JSON.';
export const REPLACE_ID = "Replace the record's id in the path with {{ key }}.";
export const MASKED = 'Every value is a stand-in: equal values get equal stand-ins.';
export const AS_IT_CAME =
  'The answer as it came: this is a trial installation on this machine. An office installation shows stand-ins only.';
/** The server's refusal of a reopened spec's masked inline keys, said before it is met. */
export const STAND_INS =
  'The inline keys of this text are stand-ins: type the keys again, or declare another key source and give keys with the run.';

/** A side's request as its rows hold it. `stored` is the request the side was opened with: it is sent as it is,
 *  whatever the rows could not say exactly (a number, a list), until a row is edited. */
export interface Rows {
  target: string;
  method: 'GET' | 'POST';
  path: string;
  query: string; // name=value lines
  headers: string; // Name: value lines
  body: string; // JSON, POST only
  stored?: unknown;
}

/** A builder's work: the sample key, each side's rows (null: a table side, which has no form) and the rest. */
export interface Draft { key: string; a: Rows | null; b: Rows | null; text: string }

export function isJson(text: string): boolean {
  try {
    JSON.parse(text);
    return true;
  } catch {
    return false;
  }
}

const isPlain = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v) && !isRaw(v);
const shown = (v: unknown) => (isRaw(v) ? v.rawJSON : String(v));
const lines = (members: unknown, sep: string) =>
  Object.entries(isPlain(members) ? members : {})
    .flatMap(([name, value]) => [value].flat().map((one) => `${name}${sep}${shown(one)}`))
    .join('\n');

/** A form's side as rows. */
export function rowsOf(side: SpecForm['sides'][Side] = {}): Rows {
  const request = isPlain(side.request) ? side.request : {};
  return {
    target: side.target ?? '',
    method: request.method === 'POST' ? 'POST' : 'GET',
    path: typeof request.path === 'string' ? request.path : '',
    query: lines(request.query, '='),
    headers: lines(request.headers, ': '),
    body: request.body === undefined ? '' : stringifyExact(request.body, 2),
    stored: side.request,
  };
}

const blank = (rows: Rows) => !(rows.target || rows.path || rows.query.trim() || rows.headers.trim() || rows.body.trim());

/** The lines as a mapping, each split at its first `sep`; with `many`, a name given twice holds each value. */
function members(text: string, sep: string, many: boolean): Record<string, string | string[]> | undefined {
  const found: Record<string, string | string[]> = {};
  for (const line of text.split('\n').filter((l) => l.trim())) {
    const at = line.indexOf(sep);
    const [name, value] = at < 0 ? [line.trim(), ''] : [line.slice(0, at).trim(), line.slice(at + 1).trim()];
    found[name] = many && name in found ? [found[name], value].flat() : value;
  }
  return Object.keys(found).length ? found : undefined;
}

/** The rows' request: the stored one while no row was edited, none while every row is empty. */
function requestOf(rows: Rows): unknown {
  if (rows.stored !== undefined) return rows.stored;
  if (blank({ ...rows, target: '' })) return undefined;
  let body: unknown;
  try {
    body = rows.method === 'POST' && rows.body.trim() ? parseExact(rows.body) : undefined;
  } catch {
    throw new Error(NOT_JSON);
  }
  return { method: rows.method, path: rows.path.trim(), query: members(rows.query, '=', true), headers: members(rows.headers, ':', false), body };
}

/** What a validation, a save and a Send post: the text, and each side whose rows name a target. */
export function formOf(d: Pick<Draft, Side | 'text'>): SpecForm {
  const sides: SpecForm['sides'] = {};
  for (const side of ['a', 'b'] as const) {
    const rows = d[side];
    if (rows?.target) sides[side] = { target: rows.target, request: requestOf(rows) };
  }
  return { text: d.text, sides };
}

/** A form as a builder's work, its sample key empty. */
export const draftOf = (form: SpecForm): Draft => filled({ key: '', a: null, b: null, text: form.text }, form);

/** The work with its empty sides taken from `form`, as a validation answers it: text pasted whole fills the rows
 *  it left empty, and a side `form` leaves out is a table side. Rows that hold anything stay. */
export function filled(d: Draft, form: SpecForm): Draft {
  const side = (rows: Rows | null, of: Side) => (rows && !blank(rows) ? rows : of in form.sides ? rowsOf(form.sides[of]) : null);
  return { ...d, a: side(d.a, 'a'), b: side(d.b, 'b') };
}

/** `HTTP 200, 143 ms, 1 attempt, 12,345 bytes`: what a Send met. */
export const sentLine = (t: Pick<Tried, 'status' | 'ms' | 'attempts' | 'pages' | 'bytes'>) =>
  [
    t.status === null ? 'No answer' : `HTTP ${t.status}`,
    t.ms !== null && `${Math.round(Number(shown(t.ms)))} ms`,
    `${t.attempts} attempt${t.attempts === 1 ? '' : 's'}`,
    t.pages > 1 && `${t.pages} pages`,
    t.bytes !== null && `${t.bytes.toLocaleString('en-US')} bytes`,
  ].filter(Boolean).join(', ');

// --- Import curl -------------------------------------------------------------------------------------------------

const BASH = "Copy the command for bash: $'…' and ^ quoting are not read.";
const TLS = 'DeltaSleuth always verifies TLS: remove -k.';
const ONLY = 'Only GET, and POST with a JSON body, are sent.';
const NO_URL = 'The command has no URL that can be read.';
const unread = (option: string) => `Import does not read ${option}: remove it.`;
const alone = (operator: string) => `Paste the curl command alone: remove ${operator} and what follows it.`;

// The loader's rules (deltasleuth/core/spec/rules.py): a header whose name, lower-cased without - and _, holds one
// of these fragments carries a credential (they cover the names the spec schema refuses), and so does a value
// that reads as an Authorization scheme or as a JSON Web Token. web/test/credential-headers.json holds both suites'
// cases.
const CREDENTIAL_NAME =
  /authorization|cookie|secret|password|passwd|apikey|accesstoken|authtoken|sessiontoken|idtoken|refreshtoken|bearer|credential/;
const CREDENTIAL_VALUE =
  /^\s*(?:bearer|basic|digest|negotiate|ntlm|token|aws4-hmac-sha256)\s+\S|\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\./i;
const credential = (name: string, value: string) =>
  CREDENTIAL_NAME.test(name.toLowerCase().replace(/[-_]/g, '')) || CREDENTIAL_VALUE.test(value);

// Headers the HTTP client writes itself, that say which page a browser sent the request from, or that make the
// request depend on what the browser's cache holds (If-None-Match and the like: a service then answers 304, no body).
const DROPPED =
  /^(?:host|content-length|transfer-encoding|connection|content-type|user-agent|accept-encoding|referer|origin|sec-.*|if-(?:none-)?match|if-(?:un)?modified-since|if-range)$/i;
// DeltaSleuth's own session and sign-in cookies (deltasleuth/auth/sessions.py `named`), whatever the port. A browser
// keeps cookies by host, not by port: a request to an application on this machine, copied from it, carries them.
const OWN_COOKIE = /^(?:__Host-)?ds_(?:session|login)(?:_\d+)?$/;
const IGNORED = /^(?:--compressed|--silent|--location|-[sL]+)$/;
const VALUED: Record<string, 'url' | 'method' | 'header' | 'data' | 'raw' | 'cookie'> = {
  '--url': 'url', '-X': 'method', '--request': 'method', '-H': 'header', '--header': 'header', '-b': 'cookie', '--cookie': 'cookie',
  '-d': 'data', '--data': 'data', '--data-binary': 'data', '--json': 'data', '--data-raw': 'raw',
};

/** A curl command as the builder takes it: a side's request rows, the address it was sent to, and the credential
 *  headers it carried, which belong to the target and never enter the rows. */
export interface Imported extends Pick<Rows, 'method' | 'path' | 'query' | 'headers' | 'body'> {
  origin: string; // scheme://host[:port]
  under: string; // the base path of the side's target that the path was read under; empty when none was
  credentials: [header: string, value: string][];
}

/** The path of a target's base URL, without the slash it may end with; empty when there is none to read. */
function basePath(base = ''): string {
  try {
    return new URL(base).pathname.replace(/\/+$/, '');
  } catch {
    return '';
  }
}

/** A header as the import keeps it: none that is dropped by name, and a Cookie as its pairs without DeltaSleuth's
 *  own, which is no header when no other pair is left. */
function keeping([header, value]: [string, string]): [string, string][] {
  if (DROPPED.test(header)) return [];
  if (!/^cookie$/i.test(header)) return [[header, value]];
  const pairs = value.split(';').map((pair) => pair.trim());
  const others = pairs.filter((pair) => pair && !OWN_COOKIE.test(pair.split('=')[0].trim())).join('; ');
  return others ? [[header, others]] : [];
}

/** A bash command line as its words: '…', "…", backslashes and line continuations. An unquoted |, <, >, ; or &&
 *  is the shell's: the command ends there, and the Error says so. */
function words(command: string): string[] {
  const found: string[] = [];
  let word: string | undefined;
  for (let i = 0; i < command.length; i++) {
    const ch = command[i];
    if (ch === '\\') {
      if (command[++i] !== '\n') word = (word ?? '') + (command[i] ?? ''); // a line continuation is no character
    } else if (ch === "'") {
      const end = command.indexOf("'", i + 1);
      if (end < 0) throw new Error(BASH);
      word = (word ?? '') + command.slice(i + 1, end);
      i = end;
    } else if (ch === '"') {
      word ??= '';
      for (i++; command[i] !== '"'; i++) {
        if (i >= command.length) throw new Error(BASH);
        if (command[i] === '\\' && '"\\$`\n'.includes(command[i + 1] ?? ' ') && command[++i] === '\n') continue;
        word += command[i];
      }
    } else if (/\s/.test(ch)) {
      if (word !== undefined) found.push(word);
      word = undefined;
    } else if (ch === '^' || (ch === '$' && command[i + 1] === "'")) {
      throw new Error(BASH);
    } else if ('|<>;'.includes(ch) || command.startsWith('&&', i)) {
      throw new Error(alone(ch === '&' ? '&&' : ch)); // the shell's, not curl's: a pipe, a redirect, a second command
    } else word = (word ?? '') + ch;
  }
  return word === undefined ? found : [...found, word];
}

/** One bash curl command as a side's rows, or an Error whose message says what to change. `base`: the base URL
 *  of the side's target, where the caller may read it: the path is then the part under its path. */
export function fromCurl(command: string, base?: string): Imported {
  const [name, ...args] = words(command);
  if (name !== 'curl') throw new Error(BASH);
  let url: string | undefined;
  let method: string | undefined;
  const data: string[] = [];
  const sent: [string, string][] = [];
  for (let i = 0; i < args.length; i++) {
    const joined = /^-([XHdb])(.+)$/.exec(args[i]); // -XPOST
    const arg = joined ? `-${joined[1]}` : args[i];
    if (!arg.startsWith('-')) {
      if (url !== undefined) throw new Error(unread('a second URL'));
      url = arg;
      continue;
    }
    if (IGNORED.test(arg)) continue;
    if (arg === '-k' || arg === '--insecure') throw new Error(TLS);
    const kind = VALUED[arg];
    if (!kind) throw new Error(unread(arg));
    const value = joined ? joined[2] : (args[++i] ?? '');
    if (kind === 'url') url = value;
    else if (kind === 'method') method = value;
    else if (kind === 'cookie' && !value.includes('=')) throw new Error(unread('a cookie file')); // as curl reads it
    else if (kind === 'cookie') sent.push(['Cookie', value]);
    else if (kind === 'header') {
      const at = value.indexOf(':');
      if (at > 0) sent.push([value.slice(0, at).trim(), value.slice(at + 1).trim()]);
    } else if (kind === 'data' && value.startsWith('@')) throw new Error(unread(`${arg} @file`));
    else data.push(value);
  }
  let address: URL;
  try {
    address = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(url ?? '') ? url! : `http://${url ?? ''}`);
  } catch {
    throw new Error(NO_URL);
  }
  if (!/^https?:$/.test(address.protocol)) throw new Error(NO_URL);
  if (address.username || address.password) throw new Error(unread('a user name in the URL'));
  const body = data.join('&'); // as curl joins them
  method = (method ?? (data.length ? 'POST' : 'GET')).toUpperCase();
  if (method === 'POST' ? body && !isJson(body) : body || method !== 'GET') throw new Error(ONLY);
  let path = address.pathname;
  let under = basePath(base);
  if (under && (path === under || path.startsWith(`${under}/`))) path = path.slice(under.length) || '/';
  else under = '';
  const kept = sent.flatMap(keeping);
  return {
    origin: address.origin,
    under,
    method: method as Rows['method'],
    path,
    query: [...address.searchParams].map(([k, v]) => `${k}=${v}`).join('\n'),
    headers: kept.filter((h) => !credential(...h)).map(([header, value]) => `${header}: ${value}`).join('\n'),
    body,
    credentials: kept.filter((h) => credential(...h)),
  };
}

/** `The command carried a credential (Cookie, X-Auth-Token). It belongs to the target, not to a spec.` */
export const carried = (found: Pick<Imported, 'credentials'>) =>
  `The command carried a credential (${found.credentials.map(([header]) => header).join(', ')}). It belongs to the target, not to a spec.`;

// --- Endpoints ---------------------------------------------------------------------------------------------------

export const AUTH: Record<AuthKind, string> = { none: 'None', bearer: 'Bearer token', header: 'Header' };
export const MORE_HEADERS = 3; // further credential headers a target takes

/** An endpoint as its form holds it. A credential is typed or handed over, sent once and never read back: an
 *  empty one keeps the credential stored for its header. */
export interface Endpoint {
  id: string;
  base_url: string;
  private_network: boolean;
  insecure_http: boolean;
  kind: AuthKind;
  header: string;
  secret: string;
  more: { header: string; secret: string }[];
  rate_per_s: string; // empty: the server's
  max_concurrency: string;
  absent_on_404: boolean;
}

export const blankEndpoint = (): Endpoint => ({
  id: '', base_url: '', private_network: false, insecure_http: false, kind: 'none', header: '', secret: '', more: [],
  rate_per_s: '', max_concurrency: '', absent_on_404: false,
});

/** A target of the product's list as the form that changes it: what it was declared with, no credential. */
export const endpointOf = (t: Target): Endpoint => ({
  ...blankEndpoint(),
  id: t.target_id,
  base_url: t.base_url ?? '',
  private_network: !!t.allow_private_network,
  insecure_http: !!t.allow_insecure_http,
  kind: t.auth?.kind ?? 'none',
  header: t.auth?.header ?? '',
  more: (t.auth?.more ?? []).map((header) => ({ header, secret: '' })),
  rate_per_s: String(t.rate_per_s ?? ''),
  max_concurrency: String(t.max_concurrency ?? ''),
  absent_on_404: !!t.absent_on_404,
});

/** What the form sends: no credential that was left empty, and no budget that was. */
export function endpointBody(e: Endpoint): TargetIn {
  const more = e.kind === 'header' ? e.more.map((m) => ({ ...m, header: m.header.trim() })) : [];
  const further = more.filter((m) => m.secret).map((m) => [m.header, m.secret]);
  const names = more.length ? more.map((m) => ({ header: m.header })) : undefined;
  return {
    base_url: e.base_url.trim(),
    allow_private_network: e.private_network,
    allow_insecure_http: e.insecure_http,
    auth: e.kind === 'header' ? { kind: e.kind, header: e.header.trim(), more: names } : { kind: e.kind },
    secret: (e.kind !== 'none' && e.secret) || undefined,
    more_secrets: further.length ? Object.fromEntries(further) : undefined,
    rate_per_s: e.rate_per_s ? Number(e.rate_per_s) : undefined,
    max_concurrency: e.max_concurrency ? Number(e.max_concurrency) : undefined,
    absent_on_404: e.absent_on_404,
  };
}

/** What an imported command hands the Targets screen: where it was sent, the base path its path was read under, and
 *  the credential headers it carried. */
export type Carried = Pick<Imported, 'origin' | 'under' | 'credentials'>;

// A name of the machine itself: in a browser's command, and as a target names it from the server or its container.
const THIS_MACHINE = /^(?:localhost|.+\.localhost|127(?:\.\d+){3}|\[::1\]|host\.docker\.internal)$/;

/** `target` when it is of the product's list and a command sent to `origin` reached its service: the same scheme and
 *  port, and the same host or two names of this machine (a command's localhost is a container's
 *  host.docker.internal). The credential the command carried is this target's; of no other is that known. */
export function reached(origin: string, target?: Target): Target | undefined {
  try {
    const [sent, at] = [new URL(origin), new URL(target?.base_url ?? '')];
    const host = sent.hostname === at.hostname || (THIS_MACHINE.test(sent.hostname) && THIS_MACHINE.test(at.hostname));
    return target?.declared_in === 'product' && host && sent.protocol === at.protocol && sent.port === at.port ? target : undefined;
  } catch {
    return undefined; // no address to read: a target of the targets file shows none
  }
}

/** The endpoint an imported command names, for the Targets form, with every credential header it carried: a lone
 *  `Authorization: Bearer …` is a bearer token; else the first is the header and the others the further ones.
 *  Laid over `target` (the one the command `reached`), whose address, consents and budget stay; without one, a new
 *  endpoint at the address the side's path is under: the origin and the base path the import took off. Its
 *  consents are left to the person. */
export function handing(found: Carried, target?: Target): Endpoint {
  const [[header, secret], ...others] = found.credentials;
  const token = !others.length && /^authorization$/i.test(header) && /^bearer\s+(\S+)$/i.exec(secret);
  const more = others.map(([name, value]) => ({ header: name, secret: value }));
  const first: Pick<Endpoint, 'kind' | 'header' | 'secret'> = token ? { kind: 'bearer', header: '', secret: token[1] } : { kind: 'header', header, secret };
  return { ...(target ? endpointOf(target) : { ...blankEndpoint(), base_url: found.origin + found.under }), ...first, more };
}

/** What stays while a screen is left, in memory only (never the browser's storage, never an address): each
 *  builder's unsaved work by spec, so a visit to Targets loses none of it, and what a builder hands the Targets
 *  screen: what a command carried, the target its side names and where to go back to. `forget` empties it when the
 *  person signed in changes. */
export const memory: { drafts: Map<string, Draft>; handed?: { found: Carried; target?: Target; back: string } } = { drafts: new Map() };
export function forget(): void {
  memory.drafts.clear();
  memory.handed = undefined;
}
