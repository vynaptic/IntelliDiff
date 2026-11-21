/** The words the screens show for the engine's codes (an outcome, a status, a severity, a change, a reason, a
 *  filter's parameter) and each code's tone. The code itself stays in the tooltip, the address and the reports.
 *  Pure, so that Node runs its tests: it loads no other module. */

// Codes whose words are not their own parts.
const WORDS: Record<string, string> = {
  KNOWN_DIFF: 'Known difference',
  NOT_FOUND_A: 'Not found in A',
  NOT_FOUND_B: 'Not found in B',
  taxonomy_code: 'Taxonomy',
  root_cause_id: 'Root cause',
  spec_name: 'Comparison name',
  session_id: 'Session',
};

const CODE = /^[A-Za-z][A-Za-z0-9]*(_[A-Za-z0-9]+)*$/; // one word or snake case, in either case

/** A code as a sentence-case phrase, a side's letter kept upper-case: `missing_in_b` is Missing in B. Text that
 *  is no code (a sentence, an id) is left as it is. */
export function label(code: string): string {
  if (Object.hasOwn(WORDS, code)) return WORDS[code];
  if (!CODE.test(code)) return code;
  const words = code.toLowerCase().split('_').map((word) => (word === 'a' || word === 'b' ? word.toUpperCase() : word));
  return words.join(' ').replace(/^./, (first) => first.toUpperCase());
}

const TONE: Record<string, string> = {
  MATCH: 'ok', completed: 'ok', running: 'ok',
  MISMATCH: 'bad', failed: 'bad', CRITICAL: 'bad', HIGH: 'bad',
  INCONCLUSIVE: 'warn', DRIFT: 'warn', NOT_FOUND_A: 'warn', NOT_FOUND_B: 'warn', MEDIUM: 'warn',
  completed_with_issues: 'warn', cancelling: 'warn', cancelled: 'warn', paused: 'warn',
  active: 'ok', revoked: 'bad', expired: 'warn', disabled: 'bad',
  approved: 'ok', rejected: 'bad', requested: 'warn',
  valid: 'ok', open: 'ok', evaluation: 'warn', ending: 'warn', grace: 'warn', unknown: 'warn',
};

/** `ok`, `bad` or `warn`; nothing for a code that carries no verdict. */
export const tone = (code: string): string => (Object.hasOwn(TONE, code) ? TONE[code] : '');
