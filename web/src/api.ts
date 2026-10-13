/** Typed client of the DeltaSleuth API v1. Every key it sends or receives is a masked key, but for the sample
 *  key a spec author types in the builder, which is sent to try a side and to compare under a saved version.
 *  The browser's credential is the HttpOnly session cookie: no token is kept here. */
// The one import, with its extension, so that Node runs the tests of this module without a bundler.
import { NOT_KEPT } from './signin.ts';

export const LOGIN_EVENT = 'ds:login';
const IDLE_MS = 60_000;

export type Items<T> = { items: T[] };
export type Page<T> = Items<T> & { next_cursor: string | null };
export type Query = Record<string, string | number | undefined>;

export interface Run {
  run_id: string;
  spec_id: number;
  spec_name: string;
  spec_version: string;
  mode: string;
  status: string;
  cycle: number;
  counters: Record<string, unknown>;
  params: { tables?: { a?: TablePlan; b?: TablePlan; only_a: string[]; only_b: string[] } };
  scan: Scan | null; // a table scan's progress; null when the run does not scan
  progress?: { keys: Record<string, number>; outcomes: Record<string, number> };
  stop_reason: string | null;
  error: string | null;
  created_by: string | null; // an actor; null before identities were recorded
  created_by_name: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  licence: RunLicence | null; // null for a run of an open build, of one before licences (010) or of an older release
}
/** The licence a run was made under and what its label says. */
export interface RunLicence {
  licence_id: string | null; // null in an unlicensed evaluation
  tier: string | null;
  state: string;
  ends: string | null;
  clock_behind?: boolean;
  label: string;
}

/** GET /v1/licence: the installation's standing, what it allows, the pairs counted
 *  in its window with their seats, and each licence installed; `state: unknown` when it cannot be read, and
 *  `open` in a build that embeds no vendor key, which needs no licence and refuses nothing. */
export interface Licence {
  installation_id: string | null;
  state: 'open' | 'evaluation' | 'valid' | 'ending' | 'grace' | 'expired' | 'unknown';
  warnings: string[];
  licence: LicenceFields | null; // the licence named; null in an evaluation and in an open build
  renewal: LicenceFields | null; // in force the day after the running licence ends
  allows: Allows | null;
  pairs: { limit: number | null; since: string | null; counted: CountedPair[] } | null;
  clock_behind_seconds: number | null;
  installed: InstalledLicence[];
}
export interface LicenceFields {
  licence_id: string;
  customer: string;
  order: string;
  tier: string;
  pairs: number | null; // null: unlimited
  starts: string;
  ends: string;
  issued: string;
  key: string;
  sha256: string;
}
export interface Allows { runs: boolean; sso: boolean; signoff: boolean; packs: boolean }
export interface CountedPair { name: string; first_at: string; seated: boolean; targets: string[] }
export interface InstalledLicence {
  licence_id: string;
  tier: string | null; // null when it grants nothing (then `problem` says why)
  starts: string | null;
  ends: string | null;
  installed_by: string;
  installed_by_name: string | null;
  installed_at: string;
  withdrawn_by: string | null;
  withdrawn_by_name: string | null;
  withdrawn_at: string | null;
  withdraw_reason: string | null;
  in_force: boolean;
  problem: string | null;
}

/** GET /v1/runs/{id} adds its retention: the class and its days, a hold, the
 *  cycles whose purge started, and the ended cycles still kept, each with the day it expires. */
export interface Retention {
  class: string;
  days: number;
  hold: { reason: string; by: string; at: string } | null;
  purged: number[];
  last_purged_at: string | null;
  cycles: { cycle: number; ended_at: string; expires_at: string }[];
}
/** GET /v1/runs/{id}'s spec approval: the state, the scopes the version needs
 *  and the auth mode it ran in (null before approvals), who approved it, when, and any revoke. */
export interface RunApproval {
  state: 'approved' | 'revoked' | 'unapproved' | 'not_needed';
  approval_id: string | null;
  needs: string[] | null;
  mode: Mode | null;
  deciders: Decider[];
  revoked_by: string | null;
  revoked_by_name: string | null;
  revoked_at: string | null;
  reason: string | null;
}
export interface Decider { actor: string; name: string | null; at: string }
/** GET /v1/runs/{id} also gives the current cycle's census and the table sides it still waits for (the
 *  worker counts them first, with evidence packs configured), and each ended cycle's census, adjudications and
 *  evidence packs and labellings. */
export type RunDetail = Run & {
  retention: Retention;
  approval: RunApproval;
  signoffs: Approval[];
  census: Census | null;
  census_pending: string[];
  cycles: CycleEvidence[];
  sample: RunSample | null; // a sampled run's current draw; null for other runs
  schedule: RunSchedule | null; // the schedule that started it; null for other runs
  schema: RunSchema; // what the schema guard holds of its tables, as lines
};

/** What the schema guard says of a run's tables: whether it holds their shapes
 *  (a run without a table side has none), the spec's mode, the run of the version it was judged against, its
 *  last look, whether it paused the run, whether a resume would accept the changes that stand, and its lines,
 *  the pause's first. The names and declared types it holds reach a screen in those lines only. */
export interface RunSchema {
  guarded: boolean;
  mode: 'finding' | 'pause' | null;
  basis: string | null;
  checked: { cycle: number; at: string; where: string; error_type: string | null; side: string | null } | null;
  paused: boolean;
  accepts: boolean;
  lines: string[];
}

/** A sampled run's current draw: its state (null once the run ended), its frame's refusal, and the draw. */
export interface RunSample {
  state: 'drawing' | 'comparing' | 'resolving' | 'paused' | null;
  error: string | null;
  draw_id: string | null;
  drawn_at: string | null;
}
/** A sign-off stratum as its cycle's end judged it; bounds are decimal strings. */
export interface Stratum {
  label: string;
  tier: 'standard' | 'high';
  target: string;
  basis: 'census' | 'sample';
  frame_keys: number;
  drawn: number;
  contrast: number;
  x_max: number;
  counters: Record<string, unknown> & { outstanding: number };
  n: number;
  x: number;
  unresolved: number;
  absent_both: number;
  api_uncomparable: number;
  retried: number;
  ub95_one_sided: string | null;
  ub95_one_sided_all_suppressed: string | null;
  ub95_one_sided_worst_case: string | null;
  verdict: 'passed' | 'failed' | 'unfinished';
  why: string[];
  done?: number; // a live draw's progress: members done, and being read again
  retrying?: number;
}
export interface Judgement {
  draw_id: string;
  status: 'passed' | 'failed' | 'incomplete';
  weighted_rate: string | null;
  strata: Stratum[];
  contrast: Record<string, unknown> & { total: number; compared: number; different: number };
}
/** A live draw's progress: the judgement its cycle would store now, and an ETA from the last 15 minutes. */
export type Progress = Judgement & { done: number; finished_recently: number; eta_seconds: number | null };
export interface Frame { keys: number; origin: { a: number; b: number; both: number }; unfit: number }
export type LedgerStatus = 'live' | 'passed' | 'failed' | 'incomplete' | 'unjudged';
/** A draw as its population's ledger lists it, in commit order. */
export interface LedgerDraw {
  draw_id: string;
  run_id: string;
  spec_id: number;
  spec_name: string;
  cycle: number;
  method: 'bottom_k' | 'rate';
  population: string[];
  salt_label: string;
  drawn_at: string;
  keys: number;
  closed_by: string | null;
  closed_by_name: string | null;
  closed_at: string | null;
  close_note: string | null;
  ended_at: string | null;
  status: LedgerStatus;
  failed_strata: string[];
  draw_seq: number;
  close_seq: number | null;
  closed?: boolean;
  blocking?: boolean; // ended, bottom_k, did not pass, not closed: a later draw is not signed off
  order?: 'before' | 'this' | 'after' | null; // in a review, against the draw it signs off
}
/** GET /v1/samples/{draw_id}: the draw, its ledger, and its progress while live (no nonce) or its precheck;
 *  below rule_approver, no nonce and no stratum's u_max at all. */
export type SampleDetail = LedgerDraw & {
  nonce: string | null;
  frame: Frame;
  strata: Stratum[];
  selection_sha256: string;
  blocking: boolean;
  ledger: LedgerDraw[];
  progress?: Progress;
  precheck?: string[];
};

/** Over which population of a side parity was observed: a table read to its end, a
 *  complete key listing, or the key set checked against a table in one snapshot; or the count that failed. */
export type CensusSide =
  | ({ basis: 'scan'; target: string; relation: string; rows: number; skipped: number; snapshots?: number | null; first_at: string | null; last_at: string | null } & Engined)
  | { basis: 'listing'; target: string; items: number; keys?: number; unfit?: number; first_at: string; last_at: string }
  | ({ basis: 'frame'; target: string; relation: string; where: string | null; rows: number; skipped: number; keys: number; unfit: number; at: string } & Engined)
  | ({ basis: 'table'; target: string; relation: string; where: string | null; rows: number; skipped: number; missing: number; extra: number; at: string } & Engined)
  | { failed: string };
/** What the record of a side on MySQL or Oracle adds: its engine, and whether its
 *  rows were read in a snapshot, in a transaction whose snapshot cannot be observed, or in neither. A file
 *  side's (`fixed`) holds no snapshots and no digest, but its files' count and bytes,
 *  whether DeltaSleuth could write to their mount, its lines that were not rows and its keys held by several
 *  rows (null: a frame counts none). */
export interface Engined {
  engine?: string;
  consistency?: Consistency;
  files?: number;
  bytes?: number;
  mount?: 'read-only' | 'writable';
  rejected?: number;
  repeated?: number | null;
}
export type Consistency = 'snapshot' | 'unverified' | 'none' | 'fixed';
export interface Census { cycle: number; at: string | null; a: CensusSide | null; b: CensusSide | null }

/** A reviewer sheet drawn from an ended cycle and, once scored, its score. A purge
 *  keeps its hashes and counts and nulls its keys and disagreements. */
export interface Adjudication {
  adjudication_id: string;
  run_id: string;
  cycle: number;
  salt: string;
  sample_limit: number;
  selection: { candidates: number; keys: Record<string, string> | null };
  drawn_by: string;
  drawn_by_name: string | null;
  drawn_at: string;
  upload_sha256: string | null;
  sheet_sha256: string | null;
  score_sha256: string | null;
  score: { adjudicated: number; agreed: number; exit: { passed: boolean; reasons: string[] } } | null;
  passed: boolean | null;
  scored_by: string | null;
  scored_by_name: string | null;
  scored_at: string | null;
  purged_at: string | null;
}
/** A signed evidence pack: revoked only with its sign-off; its record outlives the
 *  purge of its cycle, its stored ZIP does not. */
export interface Pack {
  pack_id: string;
  run_id: string;
  cycle: number;
  signoff_id: string;
  adjudication_id: string | null;
  sums_sha256: string;
  zip_sha256: string;
  key_fingerprint: string;
  issued_by: string;
  issued_by_name: string | null;
  issued_at: string;
  status: 'issued' | 'revoked';
  revoked_by: string | null;
  revoked_by_name: string | null;
  revoked_at: string | null;
  revoke_reason: string | null;
  purged_at: string | null;
}
/** A labelling of an ended cycle as lists show it: its ids, actors, dates, sizes,
 *  truth and hashes, without its sheet, selection and score (GET /v1/labellings/{id} holds those). `summary` is
 *  the server's one line and `attested` the requested or approved sign-off attesting it; a purge keeps them. */
export interface Labelling {
  labelling_id: string;
  run_id: string;
  cycle: number;
  salt: string;
  size: number;
  per_cause: number;
  accepted: number;
  agreed: number;
  item_count: number;
  truth_source: string;
  truth_as_of: string;
  selection_sha256: string;
  drawn_by: string;
  drawn_by_name: string | null;
  drawn_at: string;
  sheet_sha256: string | null;
  labels_sha256: string | null;
  score_sha256: string | null;
  labelled_by: string | null;
  labelled_by_name: string | null;
  labelled_at: string | null;
  purged_at: string | null;
  summary: string;
  attested: string | null;
}
/** GET /v1/labellings/{id}: its selection's counts and its score too, what it says (`lines`, none until it is
 *  labelled) and may not be read as. */
export type LabellingDetail = Omit<Labelling, 'item_count'> & {
  selection: { counts: Record<string, unknown>; names_side: 'a' | 'b' | null; unlocatable: boolean; warnings: string[] };
  accuracy: Record<string, unknown> | null; // the stored score (deltasleuth-labelled/1); null until labelled
  lines: string[];
  limits: string;
};
/** What a draw asks for: sizes (the server's defaults when left out) and the truth source the labels rest on. */
export interface LabellingDraw {
  size?: number;
  per_cause?: number;
  accepted?: number;
  agreed?: number;
  truth: { source: string; as_of: string };
  acknowledge_unlocatable?: boolean;
}
/** A draw's answer: the labelling (the first one, for a draw repeated within ten minutes), its items by masked
 *  key and the server's warnings. */
export interface DrawnLabelling {
  labelling_id: string;
  salt: string;
  items: { item: number; key: string; population: string; root_cause_id: string }[];
  warnings: string[];
}
export interface CycleEvidence {
  cycle: number;
  census: Census | null;
  adjudications: Adjudication[];
  labellings: Labelling[];
  packs: Pack[];
  sample?: { draw: LedgerDraw & { frame: Frame; strata: Stratum[]; nonce: string | null }; judgement: Judgement | null } | null;
}

/** A table side of a run's column plan: columns by canonical name, and its row filter.
 *  In a pair with a side on MySQL or Oracle, also its engine, its server's major and minor version, how it is
 *  read, whether its sessions are encrypted and each column's class. A file side has no schema, and names
 *  its files' path and format, their count and bytes, and its lines that were not rows; never a file's name
 *  or a digest. */
export interface TablePlan {
  schema: string | null;
  name: string;
  keys: string[];
  columns: Record<string, string>;
  where?: string | null;
  engine?: string;
  server?: string;
  consistency?: Consistency;
  tls?: boolean | null;
  classes?: Record<string, string>;
  file?: { path: string; format: 'csv' | 'parquet' };
  file_count?: number;
  bytes?: number;
  rows?: number;
  rejected?: number;
  mount?: 'read-only' | 'writable';
}
export interface Scan {
  done: boolean;
  rows_a: number;
  rows_b: number;
  skipped_a: number; // rows whose key is NULL, empty or over-long: not compared
  skipped_b: number;
  matched: number; // identical on both sides, counted only
  candidates: number; // keys read and compared one by one
  repeated_a?: number; // a file side's keys held by more than one row
  repeated_b?: number;
  graph?: Graph; // graph parity's counts, once the scan of a graph run ends
}
/** Bulk graph parity: groups compared by their members, and the components that changed. */
export interface Graph {
  groups_a: number;
  groups_b: number;
  groups_equal: number;
  groups_different: number;
  split: number;
  merge: number;
  rearrange: number;
  oversized: number; // components too large to expand (maxComponent)
}

export interface Result {
  key_masked: string;
  outcome: string;
  first_outcome: string;
  confirmation: string;
  revision: number;
  side_a_state: string;
  side_a_reason: string | null;
  side_b_state: string;
  side_b_reason: string | null;
  inconclusive_reason: string | null;
  drift_reason: string | null;
  suppressed_count: number;
  attrs: Record<string, unknown>;
}

export interface Finding {
  finding_id: string;
  key_masked?: string;
  json_pointer: string;
  path_pattern: string;
  change_type: string;
  direction: string;
  severity: string | null;
  taxonomy_code: string | null;
  value_a: unknown;
  value_b: unknown;
  value_class: string | null;
  rule_id: string | null;
  suppressed: boolean;
  root_cause_id: string | null;
}

export interface RootCause {
  root_cause_id: string;
  max_severity: string;
  taxonomy_code: string | null;
  path_pattern: string;
  change_type: string;
  direction: string;
  affected_keys: number;
  finding_count: number;
  sample_keys: string[];
}

export interface Observation {
  obs_id: string;
  side: string;
  pass_no: number;
  http_status: number | null;
  latency_ms: number | null;
  attempts: number;
  pages: number;
  error_class: string | null;
  observed_at: string;
}

export interface KeyDetail {
  run_id: string;
  key_masked: string;
  cycle: number;
  state: string;
  pass_no: number;
  result: Result | null;
  findings: Finding[];
  observations: Observation[];
  canonical: { a: unknown; b: unknown };
}

export interface Change { cycle: number; key_masked: string; from_outcome: string; to_outcome: string }
export interface Spec {
  spec_id: number;
  name: string;
  version: string;
  content_hash: string;
  created_at: string;
  approval: { approval_id: string; status: ApprovalStatus; scopes: string[] } | null; // its newest request
}
/** GET /v1/specs/{id}: the stored version, who stored it (null before identities were recorded) and its stored
 *  document as YAML, its inline keys masked: the upload's own text is not kept. */
export type SpecDetail = Omit<Spec, 'approval'> & {
  created_by: string | null;
  created_by_name: string | null;
  yaml: string;
  body: { key: { class?: string; source?: { keys?: string[] } } }; // the stored document, its inline keys masked
  form?: SpecForm; // when asked for: that document as the builder's form
};
/** A spec as the builder holds it: the document's text without its service sides'
 *  `target` and `request`, which `sides` carries. Sent, a side is there only with a target; answered, a and b
 *  each hold what their side has, and a side left out is a table side, which has no form. */
export interface SpecForm { text: string; sides: Partial<Record<'a' | 'b', { target?: string; request?: unknown }>> }
/** POST /v1/specs/try: what one side's endpoint answered for one key, in one attempt, nothing stored. `answer` is
 *  the parsed JSON as it came in a trial installation (`raw`), elsewhere its structure with every value a
 *  stand-in; null without a JSON body, over 1 MiB or after a 401 or 403 (`note` says why). Never a URL. */
export interface Tried {
  side: 'a' | 'b';
  target: string;
  method: string;
  status: number | null; // null: no answer came
  error_class: string | null;
  error_detail: string | null;
  attempts: number;
  pages: number;
  ms: number | null;
  bytes: number | null;
  content_type: string | null;
  answer: unknown;
  note: string | null;
  raw: boolean;
}
/** A target as GET /v1/targets lists it: its id, a database's engine (null for a service) and where it is
 *  declared. To who declares, a service of the product's list also says what it was declared with: its address,
 *  consents, auth (header names) and budget, and whether a credential is stored; never a credential. */
export interface Target {
  target_id: string;
  engine: string | null;
  declared_in: 'file' | 'product';
  base_url?: string;
  allow_private_network?: boolean;
  allow_insecure_http?: boolean;
  auth?: { kind: AuthKind; header: string | null; more: string[] };
  has_secret?: boolean;
  rate_per_s?: number;
  max_concurrency?: number;
  absent_on_404?: boolean;
}
export type AuthKind = 'none' | 'bearer' | 'header';
/** GET /v1/targets: the targets, and the role that declares one in the product (`admin`; in a trial `spec_author`). */
export interface Targets { items: Target[]; declarer: string }
/** The body of PUT /v1/targets/{id}: `secret` is the credential of the bearer or of
 *  `auth.header`, `more_secrets` those of `auth.more` by header; one left out keeps the stored one. */
export interface TargetIn {
  base_url: string;
  allow_private_network?: boolean;
  allow_insecure_http?: boolean;
  auth?: { kind: AuthKind; header?: string; more?: { header: string }[] };
  secret?: string;
  more_secrets?: Record<string, string>;
  rate_per_s?: number;
  max_concurrency?: number;
  absent_on_404?: boolean;
}
/** POST /v1/specs: what gates the version's runs under SSO, and its active approval. */
export type Saved = Omit<Spec, 'created_at' | 'approval'> & { needs: string[]; approval_id: string | null };
export interface Issue { pointer: string; message: string; code: string; severity: string }
export interface Pair { key: string; a: unknown; b: unknown }
/** POST /v1/profile: a draft spec, with each side's paths that have no counterpart on the other (`unmatched`). */
export interface Draft {
  yaml: string;
  notes: string[];
  evidence: Record<string, unknown>[];
  unmatched: Record<'a' | 'b', string[]>;
  issues: Issue[];
}
/** A chat message. A stored assistant message that cites nothing is a refusal. */
export interface Message { role: string; content: string; citations?: string[]; created_at?: string }
export type Session = { session_id: string; messages: Message[] };

export type Mode = 'none' | 'tokens' | 'oidc';
export interface Health { status: string; ai: boolean; auth: Mode }

/** GET /v1/auth/me: who is signed in, and the CSRF token of the session (`dev` in developer mode). */
export interface Me {
  actor: string;
  name: string;
  display_name: string | null;
  kind: 'user' | 'token' | 'dev';
  roles: string[];
  csrf: string | null;
  mode: Mode;
  idle_minutes: number | null;
  expires_at: string | null;
}

export interface Token {
  token_id: string;
  name: string;
  owner: string;
  owner_user_id: string | null;
  owner_user_name: string | null;
  role: string;
  state: string;
  created_by: string;
  created_by_name: string | null;
  created_at: string;
  expires_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
}
export interface NewToken { token_id: string; name: string; owner: string; owner_user_id: string | null; role: string; expires_at: string; token: string }
export interface User {
  user_id: string;
  username: string;
  display_name: string | null;
  roles: string[];
  created_at: string;
  last_login_at: string | null;
  disabled_at: string | null;
  disabled_by: string | null;
  disabled_by_name: string | null;
}
export interface Disabled {
  user_id: string;
  disabled_at: string;
  sessions_revoked: number;
  tokens: { token_id: string; name: string; role: string; state: string }[];
  tokens_revoked: string[];
}
export interface SignedIn {
  session_id: string;
  actor: string;
  name: string;
  roles: string[];
  client_addr: string | null;
  auth_time: string | null;
  created_at: string;
  last_seen_at: string;
  expires_at: string;
}
export interface AuditRow {
  id: number;
  at: string;
  actor: string;
  actor_name: string | null;
  action: string;
  object: string | null;
  detail: unknown;
  request_id: string | null;
  session_id: string | null;
  client_addr: string | null;
  chain: { seq: number; hash: string } | null; // its hash chain entry
}

/** A four-eyes approval: of a spec version's content, or a cycle's sign-off. */
export type ApprovalStatus = 'requested' | 'approved' | 'rejected' | 'revoked';
export interface Approval {
  approval_id: string;
  kind: 'spec' | 'signoff';
  spec_id: number;
  spec_name: string;
  spec_version: string;
  spec_owner: string | null;
  run_id: string | null;
  cycle: number | null;
  scopes: string[];
  attests_sha256: string | null;
  status: ApprovalStatus;
  approvers_needed: number; // 2 when an author is unattributed
  requested_by: string;
  requested_by_name: string | null;
  requested_at: string;
  request_note: string | null;
  first_by: string | null; // the first of two approvers
  first_by_name: string | null;
  first_at: string | null;
  decided_by: string | null;
  decided_by_name: string | null;
  decided_at: string | null;
  decision_note: string | null;
  revoked_by: string | null;
  revoked_by_name: string | null;
  revoked_at: string | null;
  revoke_reason: string | null;
}
/** What a sign-off accepts; a decision echoes its hash. */
export interface Attests {
  run_id: string;
  cycle: number;
  mode: string;
  auth_mode: Mode | null; // the one the run started in
  keys_source: string | null;
  scan: { done: boolean; rows_a: number; rows_b: number; skipped_a: number; skipped_b: number; matched: number; candidates: number } | null;
  spec_id: number;
  content_hash: string;
  spec_approval_id: string | null;
  created_by: string | null;
  ended_at: string;
  counters: Record<string, unknown> & {
    compared: number;
    cancelled: number;
    outstanding: number;
    indeterminate: number;
    coverage: number | null;
    absent_both: number;
    api_uncomparable: number;
    reconciliation: { identity: string; holds: boolean }[];
  };
  unexplained_critical: number;
  rules: { rule_id: string; when: string; classification: string; reason: string; ticket: string; expires: string | null; keys: number }[];
  root_causes: Pick<RootCause, 'root_cause_id' | 'taxonomy_code' | 'max_severity' | 'change_type' | 'path_pattern' | 'affected_keys'>[];
  claim: string;
  format?: number; // 1 from evidence packs on, 2 from sampling, 3 from labelled accuracy; absent: 008's format 0
  census?: Census;
  adjudication?: AttestedAdjudication | null; // the newest of the cycle a person scored
  sample?: AttestedSample | null; // format 2: a sampled cycle's draw
  labellings?: { drawn: number; labelled: number }; // format 3: the cycle's labellings, attested or not
  accuracy?: { labelling_id: string } | null; // format 3: the newest labelling a person labelled
}
/** What attests format 2 records of a sampled cycle: its draw, the ledger draws before it, its judgement. */
export interface AttestedSample {
  draw_id: string;
  method: 'bottom_k' | 'rate';
  salt_label: string;
  nonce: string;
  salt_sha256: string;
  drawn_at: string;
  draw_seq: number;
  population: string[];
  targets: Record<string, string>;
  confidence: string;
  sided: string;
  margin: string;
  resolve: { times: number; delay_s: number };
  frame: Frame;
  strata: Pick<Stratum, 'label' | 'tier' | 'target' | 'basis' | 'frame_keys' | 'drawn' | 'contrast'>[];
  selection_sha256: string;
  ledger: (Pick<LedgerDraw, 'draw_id' | 'spec_name' | 'run_id' | 'cycle' | 'draw_seq' | 'ended_at' | 'status' |
    'failed_strata' | 'closed_by' | 'close_seq' | 'close_note'>)[];
  judgement: Judgement | null;
}
export interface AttestedAdjudication {
  adjudication_id: string;
  sheet_sha256: string;
  score_sha256: string;
  adjudicated: number;
  agreed: number;
  passed: boolean;
}
export type Needed = { scope: string; pointer: string; value: unknown } | { rule: Attests['rules'][number] } | { root_cause: Attests['root_causes'][number] };
export interface Review {
  needed: Needed[];
  exclusion: { persons: { actor: string; name: string | null }[]; unattributed: boolean };
  history: Approval[]; // the earlier requests of the version or cycle, newest first
  content_hash?: string; // a spec version's, which an approval echoes
  body?: unknown; // masked
  previous?: { spec_id: number; version: string; body: unknown; approval_id: string } | null;
  ledger?: LedgerDraw[]; // a sampling version's population, or a sampled sign-off's
  lines?: string[]; // a format-3 sign-off's: what the labelling it attests says, in the server's words
  labellings?: { labelling_id: string; attested: boolean }[]; // and every labelling of its cycle, newest first
}
export type ApprovalDetail = Approval & {
  attests: Attests | null;
  review: Review;
  you: { can_decide: boolean; reason: string | null }; // for an approval step
};
export interface Decision { decision: 'approve' | 'reject'; note?: string; content_hash?: string; scopes?: string[]; attests_sha256?: string }
export interface Revoked { approval_id: string; status: string; paused: string[]; stopped: string[]; revoked: string[] }

/** GET /v1/notifications: whether notifications are on, the feed's cursor (`behind`:
 *  when the oldest unread audit row a rule routes was written), each declared channel with its counts and newest
 *  attempt, the rules as declared, the active mutes and the banners; never a URL, path, address or secret. */
export interface Notifications {
  enabled: boolean;
  label: string | null;
  cursor: { seq: number | null; head: number; behind: string | null } | null;
  channels: NotifyChannel[];
  rules: NotifyRule[];
  mutes: Mute[];
  banners: string[];
}
export interface NotifyChannel {
  name: string;
  kind: 'webhook' | 'slack' | 'teams' | 'email';
  host: string;
  proxy: boolean;
  allow_private_network: boolean;
  labels: boolean;
  links: boolean;
  recipients: number; // a count: addresses never leave the settings
  pending: number;
  sent_24h: number;
  dead_24h: number;
  suppressed_24h: number;
  last_error: string | null; // a closed code
  last_status: number | null; // HTTP or SMTP
}
export interface NotifyRule {
  name: string;
  events: string[];
  channels: string[];
  specs: string[] | null;
  modes: string[];
  when: Record<string, string | number | null> | null;
  reasons: string[] | null;
  states: string[] | null;
  statuses: string[] | null;
}
export interface Mute {
  mute_id: string;
  scope: 'channel' | 'run';
  target: string;
  until: string;
  reason: string;
  created_by: string;
  created_at: string;
}
/** A stored delivery: its rendered body is sent as it is at every attempt (an e-mail's without From and To). */
export interface Delivery {
  delivery_id: string;
  event_id: string;
  seq: number | null; // its audit row; null for a daily summary or a test
  event: string;
  severity: 'info' | 'warning' | 'alert';
  channel: string;
  rules: string[];
  dedup_key: string;
  run_id: string | null;
  body: string;
  body_sha256: string;
  render: string; // the channel's kind:labels:links it was rendered for: sent under those only
  state: 'pending' | 'sent' | 'dead' | 'suppressed';
  why: 'muted' | 'duplicate' | 'rate_limited' | null;
  mute_id: string | null;
  attempts: number;
  queued_at: string;
  due_at: string | null;
  last_error: string | null;
  last_status: number | null;
  created_at: string;
  done_at: string | null;
}
export interface Tested { delivery_id: string; state: 'sent' | 'dead'; status: number | null; error: string | null; latency_ms: number }
export interface MuteIn { scope: 'channel' | 'run'; target: string; hours: number; reason: string }

/** Cut-over readiness: named gates, each met or not on observed evidence, and a
 *  pair's place on a fixed ladder; never a probability, a forecast or sign-off evidence. Every text is the API's. */
export type ReadyState = 'no_evidence' | 'not_ready' | 'unknown' | 'parity_ready' | 'signoff_ready' | 'signed_off' | 'evidenced';
export type GateStatus = 'met' | 'not_met' | 'unknown' | 'not_applicable' | 'unavailable';
export interface Gate {
  gate: string;
  group: 'parity' | 'governance' | 'evidence';
  required: boolean;
  status: GateStatus;
  value: number | null;
  allowed: number | null;
  over: number | null;
  run_id: string | null;
  cycle: number | null;
  text: string;
}
export interface CycleRef { run_id: string; cycle: number }
export interface Score { passed: number; required: number }
/** One of a pair's last evidence cycles: its discrepant or unresolved keys against the tier's allowance, its
 *  suppressed keys beside them (all null for a sampled cycle, judged per stratum) and the per-cycle gates it failed. */
export interface TrendPoint extends CycleRef {
  ended_at: string;
  spec_id: number;
  sampled: boolean;
  value: number | null;
  allowed: number | null;
  suppressed: number | null;
  failed: string[];
}
export interface ReadyPair {
  pair: string;
  programme: string | null;
  entity: string | null;
  version: string | null; // null without an evidence cycle
  spec_id: number | null;
  state: ReadyState;
  regressed: boolean;
  score: Score;
  gates: Gate[];
  blocking: string[];
  evidence: (CycleRef & { ended_at: string; basis: string; claim: 'observed' }) | null;
  trend: TrendPoint[]; // the last 10 evidence cycles, oldest first
  since: string | null; // when the pair entered this state, as the worker recorded it; null until it has
  signoff: string | null;
  pack: string | null;
  closed: string | null;
  deviations: string[];
}
export interface ReadyProgramme {
  programme: string | null; // null: the pairs in no programme
  pairs: number;
  ready: number;
  worst: ReadyState | null;
  entities: ReadyEntity[];
}
export interface ReadyEntity { entity: string | null; states: Partial<Record<ReadyState, number>> }
/** GET /v1/readiness: the pairs worst first, with each programme's and entity type's roll-up. */
export interface Readiness {
  definition: string;
  now: string;
  as_of: string | null;
  config_sha256: string;
  worker_config_sha256: string | null;
  notes: string[];
  programmes: ReadyProgramme[];
  entities: ReadyEntity[];
  items: ReadyPair[];
  next_cursor: string | null;
  /** How many dashboard.pdf volumes (100 pairs each) the pairs of the programme and entity type make. */
  volumes: number;
}
/** A measured cycle as a pair's trend lists it; `skipped`: no evidence point; `boundary`: its version differs from
 *  the point before. */
export interface ReadyPoint extends CycleRef {
  ended_at: string;
  version: string | null;
  spec_id: number;
  basis: string | null;
  complete: boolean | null;
  evidence: boolean | null;
  total: number | null;
  compared: number | null;
  different: number | null;
  unresolved: number | null;
  absent_both: number | null;
  api_uncomparable: number | null;
  suppressed: number | null;
  unexplained_critical: number | null;
  new: number | null;
  persistent: number | null;
  fixed: number | null;
  other: number | null;
  sample_status: string | null;
  graph_rows: number | null;
  fails: string[];
  skipped: boolean;
  boundary: boolean;
}
export type ReadyStratum = Pick<
  Stratum,
  'label' | 'tier' | 'basis' | 'frame_keys' | 'drawn' | 'n' | 'x' | 'unresolved' | 'absent_both' | 'api_uncomparable' | 'verdict' | 'why'
>;
/** What a sign-off of the pair's unsigned candidate would be refused for, computed now: the sign-off decides. */
export interface Precheck extends CycleRef { label: string; purged: boolean; problems: string[] }
/** GET /v1/readiness/pairs/{name}; `precheck` only when asked for (null: nothing to check). */
export type ReadyDetail = ReadyPair & {
  definition: string;
  now: string;
  config_sha256: string;
  worker_config_sha256: string | null;
  candidate: CycleRef | null;
  convergence: { streak: number; needed: number; window: number };
  notes: string[];
  points: ReadyPoint[];
  versions: (CycleRef & { spec_id: number; version: string; ended_at: string })[];
  runs: (CycleRef & { status: string; stop_reason: string | null; started_at: string | null })[];
  strata: ReadyStratum[] | null;
  labelling: (CycleRef & { labelling_id: string; labelled_at: string; attested: string | null }) | null;
  precheck?: Precheck | null;
};
/** A `readiness.changed` row as the worker recorded it: ids, states, counts and slugs only. */
export interface Transition {
  seq: number;
  at: string;
  detail: {
    pair: string;
    programme: string | null;
    entity: string | null;
    from: ReadyState | null; // null: the pair's first row
    to: ReadyState;
    regressed: boolean;
    score: Score;
    causes: string[];
    changed: { gate: string; from: GateStatus | null; to: GateStatus | null }[];
  };
}
/** A measured cycle as a pair's brief lists it: the trend's point with what its stored counters say (the rate and
 *  the coverage as the store's own digits, null for a sampled cycle, which has no rate; its count of each outcome),
 *  its root-cause groups (null once its purge started) and its marks against the point before. */
export interface BriefPoint extends ReadyPoint {
  mismatch_rate: string | null;
  coverage: string | null;
  by_outcome: Record<string, number> | null;
  cancelled: number | null;
  outstanding: number | null;
  root_causes: number | null;
  marks: Mark[];
}
export type Mark = 'version' | 'keys'; // a new spec version; another key source or key-list size
/** A brief's reported cycle, its newest complete one whatever its basis; `strata`: a sampled cycle's lines. */
export type BriefCycle = BriefPoint & { draw_id: string | null; claim: 'observed'; strata: string[] | null; notes: string[] };
/** A root-cause group of a brief: opened in the reported cycle, still open in it, or closed (not observed in it,
 *  which is not "fixed"); `since`: the marks after the last cycle holding it. Never its path or a key. */
export interface BriefGroup {
  root_cause_id: string;
  state: 'opened' | 'still_open' | 'closed';
  first: CycleRef & { ended_at: string };
  last: CycleRef & { ended_at: string };
  seen: number;
  max_severity: string;
  taxonomy_code: string | null;
  change_type: string;
  affected_keys: number;
  since: Mark[];
}
/** GET /v1/readiness/pairs/{name}/brief: the pair as listed, with its last 30 measured cycles oldest first, its
 *  reported cycle (null without a complete one: the last note says why) and its root-cause groups over the window
 *  (null without that cycle or once it is purged: the last note). Stored measures restated; every text is the API's. */
export type Brief = ReadyPair & {
  definition: string;
  brief: string;
  now: string;
  config_sha256: string;
  notes: string[];
  legend: string;
  points: BriefPoint[];
  cycle: BriefCycle | null;
  causes: {
    opened: number;
    still_open: number;
    closed: number;
    total: number;
    cycles: number;
    from: string;
    text: string;
    items: BriefGroup[]; // the first 500
  } | null;
};

/** Schedules: a stored spec started at cron times in a time zone. Every firing's text,
 *  banner and warning is the API's; a schedule holds no key and no value. */
export interface DueTime { utc: string; local: string | null } // local: the zone's wall clock; null when it does not load
export interface RunOptions { mode: 'batch' | 'soak'; soak?: { rolling: boolean } | null }
/** The body of POST and PUT /v1/schedules (a PUT adds the `revision` it was loaded at): `spec_id` pins a
 *  version, `spec_name` follows the name's newest one. */
export interface ScheduleIn {
  name: string;
  spec_id?: number;
  spec_name?: string;
  run: RunOptions;
  cron: string;
  time_zone: string;
  start_within: number;
  ends_at: string | null;
}
/** What the four-eyes gate says now of the version a firing would run: the gate judges each firing anew. */
export interface ScheduleGate {
  needs: string[];
  mode: Mode;
  state: 'approved' | 'not_needed' | 'unapproved';
  approval_id: string | null;
  refusal: string | null;
}
export interface Schedule {
  schedule_id: string;
  name: string;
  spec_name: string;
  spec_id: number | null; // null: it follows the name
  runs: { spec_id: number | null; version: string | null; approval?: ScheduleGate | null }; // what a firing would run now
  newer: { spec_id: number; version: string } | null; // the stored version past its pin
  run: RunOptions;
  cron: string;
  time_zone: string;
  start_within: number;
  ends_at: string | null;
  state: 'active' | 'paused' | 'ended' | 'deleted';
  auth_mode: Mode;
  revision: number;
  created_by: string;
  created_by_name: string | null;
  created_at: string;
  set_by: string;
  set_by_name: string | null;
  set_at: string;
  paused_by: string | null; // a person, or system:worker
  paused_by_name: string | null;
  paused_at: string | null;
  next_due: DueTime | null;
  overdue: boolean;
  clock_behind: boolean;
  last: { at: string; run_id: string | null; run_status: string | null; text: string } | null;
  held: { due: string; to: string; count: number } | null; // a standing refusal's range not yet chained
}
/** GET /v1/schedules: what readers are never told, this build's tz data, how many schedules are overdue, and a
 *  page of the schedules with its banners. */
export interface Schedules {
  not: string[];
  tzdata: string;
  overdue: number;
  banners: string[];
  items: Schedule[];
  next_cursor: string | null;
}
/** A started run, a missed time or range, or times a tz release moved: a projection of the worker's audit row. */
export interface Firing {
  seq: number;
  at: string;
  outcome: 'fire' | 'missed' | 'rezone';
  due: string | null;
  to: string | null;
  count: number | null;
  late_s: number | null;
  run_id: string | null;
  reason: string | null;
  code: string | null;
  next: string | null;
  text: string;
}
export type ScheduleDetail = Schedule & { upcoming: DueTime[]; firings: Firing[] };
export type SavedSchedule = Schedule & { warnings?: string[] };
export interface Zones { tzdata: string; zones: string[]; renamed: Record<string, string> }
export interface Preview { upcoming: DueTime[]; notes: string[]; tzdata: string }
/** GET /v1/runs/{id}'s schedule: the due time the run was started for, in the zone in force at the firing. */
export interface RunSchedule {
  schedule_id: string;
  name: string | null;
  due: string;
  time_zone: string;
  late_s: number;
  text: string;
}

/** Table pre-checks: each pair's two tables counted and added up, never a comparison
 *  of rows. Every text of a pair, the totals, the banner and each verdict's words are the API's. (`Precheck` is
 *  the sign-off precheck of a readiness pair.) */
export type PrecheckLevel = 'schema' | 'counts' | 'columns' | 'distinct';
export type PrecheckVerdict =
  | 'unpaired' | 'refused' | 'not_read' | 'rows_differ' | 'keys_null_or_repeated' | 'columns_not_compared'
  | 'values_differ' | 'incomplete' | 'schema_differs' | 'no_difference_observed'; // worst first
export type PairStatus = 'pending' | 'running' | 'done' | 'cancelled';
/** The body of POST /v1/prechecks: exactly one of the three forms, and the level (columns by default). */
export interface PrecheckIn {
  level?: PrecheckLevel;
  spec_ids?: number[]; // stored versions
  spec_names?: string[]; // each name's newest version
  all_specs?: boolean; // the newest version of every name whose sides are both tables
}
/** The body of POST /v1/prechecks/catalogue: two database targets' schemas (the target's default when left out),
 *  their tables paired by name, those the globs choose; level schema, counts (the default) or columns. */
export interface CatalogueIn {
  a: { target: string; schema?: string };
  b: { target: string; schema?: string };
  include?: string[];
  exclude?: string[];
  level?: Exclude<PrecheckLevel, 'distinct'>;
}
export interface PrecheckMade { not: string; precheck_id: string; pairs: number; skipped: number; evaluation: boolean }
export interface TablePrecheck {
  precheck_id: string;
  source: 'specs' | 'catalogue';
  level: PrecheckLevel;
  pairs: number;
  skipped: number; // of all_specs: the names whose newest version is no pair of tables
  evaluation: boolean; // made in a vendor build's unlicensed evaluation; never in an open build
  created_by: string;
  created_by_name: string | null;
  created_at: string;
  cancelled_by: string | null;
  cancelled_by_name: string | null;
  cancelled_at: string | null;
  finished_at: string | null;
  progress: Partial<Record<PairStatus, number>>;
  verdicts: Partial<Record<PrecheckVerdict, number>>;
  totals: string; // its pairs counted by verdict, worst first, in words
}
/** GET /v1/prechecks: what a pre-check is not, the banner of one no worker reads, and a page of them. */
export interface Prechecks { not: string; banners: string[]; items: TablePrecheck[]; next_cursor: string | null }
/** A pair as its pre-check lists it; `text` is its verdict's line (a cancelled pair's says so). */
export interface PrecheckPair {
  n: number;
  label: string;
  spec_id: number | null; // null: two relations paired by name
  status: PairStatus;
  verdict: PrecheckVerdict | null;
  differing: number | null;
  rows: [number | null, number | null];
  text: string | null;
}
/** GET /v1/prechecks/{id}: the pre-check, each verdict as words, and a page of its pairs worst first. */
export type PrecheckDetail = TablePrecheck & {
  not: string;
  banners: string[];
  worded: Record<PrecheckVerdict, string>;
  items: PrecheckPair[];
  next_cursor: string | null;
};
/** A column as a side's catalogue holds it. */
export interface CatalogueColumn { declared: string; cls: string | null; nullable: boolean | null }
/** A pair's result, format 1: its lines worst first, the refusals a run would meet and its schema codes. */
export interface PrecheckResult {
  format: number;
  level: PrecheckLevel; // the level read
  refused: string[];
  schema: { code: string; column: string | null; group: 'not_compared' | 'schema'; a: CatalogueColumn | null; b: CatalogueColumn | null }[];
  skipped: string[];
  lines: string[];
  names?: Record<'a' | 'b', { schema: string; name: string }>; // once both catalogues were read
}
/** GET /v1/prechecks/{id}/pairs/{n}: the pair, its result and each compared column's figures as lines. */
export type PrecheckPairDetail = PrecheckPair & {
  not: string;
  precheck_id: string;
  level: PrecheckLevel;
  evaluation: boolean;
  said: string | null; // its verdict as words
  targets: [string, string];
  relations: [string | null, string | null];
  finished_at: string | null;
  result: PrecheckResult | null;
  columns: { column: string; classes: (string | null)[]; lines: string[] }[];
};

export class ApiError extends Error {
  constructor(readonly status: number, readonly body: unknown, message: string) {
    super(message);
  }
}

let csrf: string | null = null; // the session's CSRF token, from /me; kept in memory only
let used = Date.now();

/** Counts a pointerdown, keydown or wheel on `target` as use. Requests after 60 s without one (the Run
 *  screen's polls) carry X-DS-Background: 1, so a polling tab still idles out. */
export function watchUse(target: EventTarget): void {
  for (const type of ['pointerdown', 'keydown', 'wheel'])
    target.addEventListener(type, () => (used = Date.now()), { capture: true, passive: true });
}

function keepCsrf(me: Me): Me {
  csrf = me.csrf;
  return me;
}

async function request(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if ((init.method ?? 'GET') !== 'GET' && csrf) headers.set('X-CSRF-Token', csrf);
  if (Date.now() - used > IDLE_MS) headers.set('X-DS-Background', '1');
  const res = await fetch(`/v1${path}`, { ...init, headers });
  if (res.ok) return res;
  const body = (await res.json().catch(() => null)) as { detail?: unknown } | null;
  if (res.status === 401) dispatchEvent(new Event(LOGIN_EVENT));
  const detail = body?.detail;
  throw new ApiError(res.status, body, typeof detail === 'string' ? detail : `${res.status} ${res.statusText}`);
}

const get = <T>(path: string) => request(path).then((r) => r.json() as Promise<T>);
const del = <T>(path: string) => request(path, { method: 'DELETE' }).then((r) => r.json() as Promise<T>);

function send<T>(path: string, body: string, type: string, method = 'POST'): Promise<T> {
  return request(path, { method, headers: { 'Content-Type': type }, body }).then((r) => r.json());
}
const post = <T>(path: string, body: unknown) => send<T>(path, stringifyExact(body), 'application/json');
const put = <T>(path: string, body: unknown) => send<T>(path, stringifyExact(body), 'application/json', 'PUT');
const postYaml = <T>(path: string, yaml: string) => send<T>(path, yaml, 'application/yaml');
/** A POST whose answer keeps its numbers exact, as `parseExact` reads them. */
const postExact = <T>(path: string, body: unknown) =>
  request(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: stringifyExact(body) }).then(
    async (r) => parseExact(await r.text()) as T,
  );

/** `?a=1&b=2` of the non-empty values. */
export function qs(params: Query): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== '') q.set(k, String(v));
  return q.size ? `?${q}` : '';
}

/** A number whose JSON text a double would change (a big integer, `1.10`): kept as that text. */
class Raw {
  constructor(readonly rawJSON: string) {}
}
export const isRaw = (v: unknown): v is Raw => v instanceof Raw;

// Every browser, not only those with JSON.parse source-text access (Chrome 114, Firefox 135, Safari 18.4):
// such a number becomes a string carrying this nonce, which no document holds, and the reviver turns it back.
const MARK = `~${Math.random().toString(36).slice(2)}:`;
const LEXEME = /"(?:[^"\\]|\\.)*"|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/g; // a string, or a number outside one

/** JSON.parse keeping numbers exact: evidence is shown, and samples are sent, as they came. */
export function parseExact(text: string): unknown {
  JSON.parse(text); // JSON only: its numbers are then exactly the lexemes outside strings
  const marked = text.replace(LEXEME, (t) => (t[0] === '"' || String(Number(t)) === t ? t : `"${MARK}${t}"`));
  return JSON.parse(marked, (_k, v) => (typeof v === 'string' && v.startsWith(MARK) ? new Raw(v.slice(MARK.length)) : v));
}

/** JSON.stringify writing a `Raw` as its text; `space` indents it for reading. */
export function stringifyExact(value: unknown, space?: number): string {
  const text = JSON.stringify(value, (_k, v) => (v instanceof Raw ? MARK + v.rawJSON : v), space);
  return text.replace(new RegExp(`"${MARK}([^"]*)"`, 'g'), '$1');
}

const run = (id: string) => `/runs/${encodeURIComponent(id)}`;
const user = (id: string) => `/auth/users/${encodeURIComponent(id)}`;
const approval = (id: string) => `/approvals/${encodeURIComponent(id)}`;
const adjudication = (id: string) => `/adjudications/${encodeURIComponent(id)}`;
const labelling = (id: string) => `/labellings/${encodeURIComponent(id)}`;
const pack = (id: string) => `/packs/${encodeURIComponent(id)}`;
const cycleOf = (runId: string, cycle: number) => `${run(runId)}/cycles/${cycle}`;
const channelOf = (name: string) => `/notifications/channels/${encodeURIComponent(name)}`;
const ready = (name: string) => `/readiness/pairs/${encodeURIComponent(name)}`;
const schedule = (id: string) => `/schedules/${encodeURIComponent(id)}`;
const precheck = (id: string) => `/prechecks/${encodeURIComponent(id)}`;

export const api = {
  /** Liveness, whether an LLM profile is configured, and the auth mode (no credential needed). */
  health: () => get<Health>('/health'),
  me: () => get<Me>('/auth/me').then(keepCsrf),
  /** Tokens mode: a session acting as the token. /me then shows that the browser kept the session cookie,
   *  and gives the CSRF token; a browser that dropped it is told so. */
  tokenLogin: (token: string) =>
    post<Me>('/auth/token-login', { token }).then(() =>
      get<Me>('/auth/me').then(keepCsrf, (e: unknown) => {
        if (e instanceof ApiError && e.status === 401 && e.message === 'not signed in') throw new ApiError(401, e.body, NOT_KEPT);
        throw e;
      }),
    ),
  /** Ends the session; where the browser then signs out of the identity provider, if anywhere. */
  logout: () =>
    post<{ logout_url: string | null }>('/auth/logout', {}).then((r) => {
      csrf = null;
      return r;
    }),
  tokens: () => get<Items<Token>>('/auth/tokens'),
  createToken: (body: { name: string; owner: string; owner_user_id?: string; role: string; days?: number }) =>
    post<NewToken>('/auth/tokens', body),
  revokeToken: (id: string) => del<{ token_id: string }>(`/auth/tokens/${encodeURIComponent(id)}`),
  users: () => get<Items<User>>('/auth/users'),
  disableUser: (id: string, revoke_tokens: boolean) => post<Disabled>(`${user(id)}/disable`, { revoke_tokens }),
  enableUser: (id: string) => post<{ user_id: string }>(`${user(id)}/enable`, {}),
  sessions: () => get<Items<SignedIn>>('/auth/sessions'),
  revokeSession: (id: string) => del<{ count: number }>(`/auth/sessions/${encodeURIComponent(id)}`),
  audit: (q: Query) => get<Page<AuditRow>>(`/audit${qs(q)}`),
  runs: (cursor?: string) => get<Page<Run>>(`/runs${qs({ cursor })}`),
  run: (id: string) => get<RunDetail>(run(id)),
  createRun: (body: { spec_id: number; mode: string; keys?: string[]; soak?: { rolling: boolean } }) =>
    post<{ run_id: string }>('/runs', body),
  control: (id: string, action: string) => post<unknown>(`${run(id)}/${action}`, {}),
  results: (id: string, q: Query) => get<Page<Result>>(`${run(id)}/results${qs(q)}`),
  findings: (id: string, q: Query) => get<Page<Finding>>(`${run(id)}/findings${qs(q)}`),
  rootCauses: (id: string, cycle?: string) => get<Items<RootCause>>(`${run(id)}/root-causes${qs({ cycle })}`),
  changes: (id: string) => get<Items<Change>>(`${run(id)}/changes`),
  key: (id: string, key: string, cycle?: string) =>
    request(`${run(id)}/keys/${encodeURIComponent(key)}${qs({ cycle })}`).then(
      async (r) => parseExact(await r.text()) as KeyDetail,
    ),
  report: (id: string, name: string, cycle?: string) =>
    request(`${run(id)}/reports/${name}${qs({ cycle })}`).then((r) => r.blob()),
  /** Compare a key of a run now, or without a run the builder's sample key under a stored spec: the key view of
   *  the one-shot run that holds the result. */
  compare: (spec_id: number, run_id: string | undefined, key: string) =>
    request('/compare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ spec_id, run_id, key }),
    }).then(async (r) => parseExact(await r.text()) as KeyDetail),
  specs: () => get<Items<Spec>>('/specs'),
  /** A stored spec; `form`: also as the builder's form. Its numbers stay exact: a request not edited is sent back
   *  as it was stored. */
  spec: (id: string, form = false) =>
    request(`/specs/${encodeURIComponent(id)}${qs({ form: form ? 1 : undefined })}`).then(
      async (r) => parseExact(await r.text()) as SpecDetail,
    ),
  validateSpec: (yaml: string) => postYaml<{ issues: Issue[] }>('/specs/validate', yaml),
  saveSpec: (yaml: string) => postYaml<Saved>('/specs', yaml),
  /** A form's issues, and the joined document as a form again: text pasted whole fills the forms it left empty. */
  validateForm: (form: SpecForm) => postExact<{ issues: Issue[]; form?: SpecForm }>('/specs/validate', form),
  saveForm: (form: SpecForm) => post<Saved>('/specs', form),
  /** Send one side's request once for one key (the raw sample key): what came back; nothing is stored. */
  trySide: (form: SpecForm, side: 'a' | 'b', key: string) => postExact<Tried>('/specs/try', { form, side, key }),
  approvals: (q: Query) => get<Page<Approval>>(`/approvals${qs(q)}`),
  approval: (id: string) => get<ApprovalDetail>(approval(id)),
  requestApproval: (specId: number, note?: string) =>
    post<Pick<Approval, 'approval_id' | 'status' | 'scopes' | 'approvers_needed'>>(`/specs/${specId}/approvals`, { note }),
  requestSignoff: (runId: string, cycle: number, note?: string) =>
    post<Pick<Approval, 'approval_id' | 'status' | 'attests_sha256' | 'approvers_needed'>>(
      `${run(runId)}/cycles/${cycle}/signoff`,
      { note },
    ),
  decide: (id: string, body: Decision) => post<Approval>(`${approval(id)}/decision`, body),
  revoke: (id: string, reason: string) => post<Revoked>(`${approval(id)}/revoke`, { reason }),
  /** Draw a reviewer sheet of an ended cycle: its id, salt and masked keys. */
  draw: (runId: string, cycle: number, limit?: number) =>
    post<{ adjudication_id: string; salt: string; keys: string[] }>(`${cycleOf(runId, cycle)}/adjudications`, { limit }),
  adjudications: (runId: string, cycle: number) => get<Items<Adjudication>>(`${cycleOf(runId, cycle)}/adjudications`),
  sheet: (id: string) => request(`${adjudication(id)}/sheet.csv`).then((r) => r.blob()),
  /** Score a reviewed sheet: its bytes as the file holds them, so that the server judges their encoding. */
  score: (id: string, sheet: Blob) =>
    request(`${adjudication(id)}/score`, { method: 'POST', headers: { 'Content-Type': 'text/csv' }, body: sheet }).then(
      (r) => r.json() as Promise<Adjudication>,
    ),
  /** Draw a labelling of an ended cycle against a named truth source. */
  drawLabelling: (runId: string, cycle: number, body: LabellingDraw) =>
    post<DrawnLabelling>(`${cycleOf(runId, cycle)}/labellings`, body),
  labellings: (runId: string, cycle: number) => get<Items<Labelling>>(`${cycleOf(runId, cycle)}/labellings`),
  labelling: (id: string) => get<LabellingDetail>(labelling(id)),
  labellingSheet: (id: string) => request(`${labelling(id)}/sheet.csv`).then((r) => r.blob()),
  /** Label it with its filled sheet, once: the file's bytes as they are, so that the server judges their encoding. */
  label: (id: string, sheet: Blob) =>
    request(`${labelling(id)}/labels`, { method: 'POST', headers: { 'Content-Type': 'text/csv' }, body: sheet }).then(
      (r) => r.json() as Promise<LabellingDetail>,
    ),
  issuePack: (runId: string, cycle: number) =>
    post<{ pack_id: string; sums_sha256: string; key_fingerprint: string }>(`${cycleOf(runId, cycle)}/packs`, {}),
  packs: (q: Query) => get<Page<Pack>>(`/packs${qs(q)}`),
  pack: (id: string) => get<Pack & { manifest: Record<string, unknown> }>(pack(id)),
  downloadPack: (id: string) => request(`${pack(id)}/download`).then((r) => r.blob()),
  /** revoked.txt, its signature and the signing key, zipped: what a pack's step 8 checks. */
  revocations: () => request('/packs/revocations').then((r) => r.blob()),
  /** A spec name's ledger (every spec's draws of its population), or every draw, in commit order. */
  samples: (q: Query) => get<Page<LedgerDraw>>(`/samples${qs(q)}`),
  sample: (id: string) => get<SampleDetail>(`/samples/${encodeURIComponent(id)}`),
  /** Close the name's ended draws that did not pass, with a note of what was fixed: the draws closed. */
  closeDraws: (spec_name: string, note: string) => post<{ closed: string[] }>('/samples/close', { spec_name, note }),
  targets: () => get<Targets>('/targets'),
  /** Declare a service in the product, or change it: it holds for every run started afterwards. */
  declareTarget: (id: string, body: TargetIn) => put<Target>(`/targets/${encodeURIComponent(id)}`, body),
  /** Remove a target of the product's list, with its credentials. */
  removeTarget: (id: string) => request(`/targets/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  licence: () => get<Licence>('/licence'),
  notifications: () => get<Notifications>('/notifications'),
  deliveries: (q: Query) => get<Page<Delivery>>(`/notifications/deliveries${qs(q)}`),
  /** One attempt now, outside the queue: stored as sent or dead, and audited. */
  testChannel: (name: string) => post<Tested>(`${channelOf(name)}/test`, {}),
  /** The channel's deliveries that died since `since` (72 hours by default), tests aside, pending again. */
  retryChannel: (name: string, since?: string) => post<{ count: number }>(`${channelOf(name)}/retry`, { since }),
  mute: (body: MuteIn) => post<{ mute_id: string; until: string }>('/notifications/mutes', body),
  unmute: (id: string) => post<{ mute_id: string }>(`/notifications/mutes/${encodeURIComponent(id)}/end`, {}),
  readiness: (q: Query) => get<Readiness>(`/readiness${qs(q)}`),
  /** A pair's detail; `precheck`: also what a sign-off of its candidate would be refused for, computed now. */
  readinessPair: (name: string, precheck = false) => get<ReadyDetail>(`${ready(name)}${qs({ precheck: precheck ? 1 : undefined })}`),
  /** A pair's trend, oldest first; the pages go back in time. */
  readinessCycles: (name: string, q: Query) => get<Page<ReadyPoint>>(`${ready(name)}/cycles${qs(q)}`),
  /** A pair's cut-over brief: its gates, its newest complete cycle's stored figures, its last measured cycles
   *  with their marks and its root-cause groups. */
  readinessBrief: (name: string) => get<Brief>(`${ready(name)}/brief`),
  transitions: (q: Query) => get<Page<Transition>>(`/readiness/transitions${qs(q)}`),
  /** dashboard.pdf (the `volume` asked for, the first without one), dashboard.csv or trends.csv of the pairs the
   *  query takes, or brief.pdf, brief.csv or causes.csv of its `pair`; the download is audited. */
  readinessReport: (name: string, q: Query) => request(`/readiness/reports/${name}${qs(q)}`).then((r) => r.blob()),
  schedules: (q: Query = {}) => get<Schedules>(`/schedules${qs(q)}`),
  /** The time zones a schedule takes in this build, and each renamed zone's primary. */
  scheduleZones: () => get<Zones>('/schedules/zones'),
  /** A schedule by its id or live name, with the gate's state, its next five times and its newest firings. */
  schedule: (id: string) => get<ScheduleDetail>(schedule(id)),
  scheduleFirings: (id: string, q: Query) => get<Page<Firing>>(`${schedule(id)}/firings${qs(q)}`),
  /** A cron's next ten times in a zone and its notes (the zone's name, each clock change); writes nothing. */
  previewSchedule: (cron: string, time_zone: string) => post<Preview>('/schedules/preview', { cron, time_zone }),
  createSchedule: (body: ScheduleIn) => post<SavedSchedule>('/schedules', body),
  /** Replace its definition as loaded at `revision`: a time due and not started is recorded as missed first. */
  saveSchedule: (id: string, body: ScheduleIn & { revision: number }) => put<SavedSchedule>(schedule(id), body),
  pauseSchedule: (id: string) => post<SavedSchedule>(`${schedule(id)}/pause`, {}),
  resumeSchedule: (id: string) => post<SavedSchedule>(`${schedule(id)}/resume`, {}),
  deleteSchedule: (id: string) => del<{ schedule_id: string; deleted_at: string }>(schedule(id)),
  prechecks: (q: Query = {}) => get<Prechecks>(`/prechecks${qs(q)}`),
  /** A pre-check with its totals and a page of its pairs, worst first; `verdict`: only that verdict's. */
  precheck: (id: string, q: Query = {}) => get<PrecheckDetail>(`${precheck(id)}${qs(q)}`),
  precheckPair: (id: string, n: number) => get<PrecheckPairDetail>(`${precheck(id)}/pairs/${n}`),
  /** Store a pre-check of stored specs, its pairs pending for the worker. */
  createPrecheck: (body: PrecheckIn) => post<PrecheckMade>('/prechecks', body),
  /** List two schemas, pair their tables by name and store the pre-check, its pairs pending (spec authors). */
  createCatalogue: (body: CatalogueIn) => post<PrecheckMade>('/prechecks/catalogue', body),
  /** A starter spec of a pair paired by name, its issues heading it as comments; never stored (spec authors). */
  precheckDraft: (id: string, n: number) => request(`${precheck(id)}/pairs/${n}/draft.yaml`).then((r) => r.blob()),
  cancelPrecheck: (id: string) =>
    post<{ not: string; precheck_id: string; cancelled_at: string }>(`${precheck(id)}/cancel`, {}),
  /** precheck.csv, precheck-columns.csv or precheck.pdf; the download is audited. */
  precheckReport: (id: string, name: string) => request(`${precheck(id)}/reports/${name}`).then((r) => r.blob()),
  profile: (body: { pairs: Pair[]; spec_name: string; target_a: string; target_b: string; key_name: string }) =>
    post<Draft>('/profile', body),
  newChat: (run_id: string, key?: string) => post<{ session_id: string }>('/chat/sessions', { run_id, key }),
  ask: (session: string, content: string) =>
    post<Session>(`/chat/sessions/${encodeURIComponent(session)}/messages`, { content }),
  chat: (session: string) => get<Session>(`/chat/sessions/${encodeURIComponent(session)}`),
  /** A session of its own asked a fixed question: explain that root cause, or without one summarise the run. */
  explain: (run_id: string, root_cause_id?: string) => post<Session>('/chat/sessions/explain', { run_id, root_cause_id }),
};
