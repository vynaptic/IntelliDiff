// Every form control of a screen has a name a screen reader reads: an aria-label, an aria-labelledby, or a
// <label> around it. The screens are read as text: no browser runs here.
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { test } from 'node:test';

const SRC = new URL('../src/', import.meta.url);

/** The control's opening tag, from its `<` to the `>` outside any `{…}` expression. */
function tagAt(text: string, start: number): string {
  let depth = 0;
  for (let i = start; i < text.length; i++) {
    const c = text[i];
    if (c === '{') depth++;
    else if (c === '}') depth--;
    else if (c === '>' && depth === 0) return text.slice(start, i + 1);
  }
  return text.slice(start);
}

function unnamed(file: string, text: string): string[] {
  const found: string[] = [];
  for (const m of text.matchAll(/<(input|textarea|select)\b/g)) {
    const tag = tagAt(text, m.index);
    if (/\baria-label(ledby)?=/.test(tag) || /type="hidden"/.test(tag)) continue;
    const before = text.slice(0, m.index);
    const open = (before.match(/<label\b/g) ?? []).length - (before.match(/<\/label>/g) ?? []).length;
    if (open > 0) continue; // inside a <label>
    found.push(`${file}:${before.split('\n').length}`);
  }
  return found;
}

test('every input, textarea and select of the screens is named', () => {
  const screens = readdirSync(SRC, { encoding: 'utf8' }).filter((name) => name.endsWith('.tsx'));
  assert.ok(screens.length > 10);
  const missing = screens.flatMap((name) => unnamed(name, readFileSync(new URL(name, SRC), 'utf8')));
  assert.deepEqual(missing, []);
});

test('a control outside a label without an aria-label is found', () => {
  assert.deepEqual(unnamed('x.tsx', '<div>\n<input value={a > b ? 1 : 2} />\n</div>'), ['x.tsx:2']);
  assert.deepEqual(unnamed('x.tsx', '<label>Name <input value={v} /></label><textarea aria-label="Body" />'), []);
});
