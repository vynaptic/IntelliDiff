/** The chat drawer about a run or one of its keys. Answers render as plain text with citation chips.
 *  `closed` says why it takes no question: once the run's evidence is purged, the API answers 410.
 *  `ai` is /v1/health's, loaded by the screen: false shows that the AI is off, unknown leaves the chat on. */
import { useEffect, useState, type FormEvent } from 'react';
import { api, type Message } from './api';
import { CITATION } from './explain';
import { href, message } from './lib';
import { Err } from './ui';

/** Scrolls to the cited row on this screen, if it shows it. */
export function reveal(id: string): void {
  const el = document.getElementById(id);
  if (!el) return;
  el.scrollIntoView({ block: 'center' });
  el.classList.add('flash');
  setTimeout(() => el.classList.remove('flash'), 1500);
}

/** Where a citation leads. `keys` (explain.ts's `citedKeys`) gives a finding's or an observation's key, which
 *  makes its chip a link to the Key screen that outlines its row; `cycle` is the one such links show. */
type Cites = { runId: string; keys?: Record<string, string>; cycle?: number };

function Chip({ id, runId, keys, cycle }: Cites & { id: string }) {
  if (id.startsWith('K:')) return <a className="chip" href={href(['runs', runId, 'keys', id.slice(2)], { cycle })}>{id}</a>;
  if (id.startsWith('RC-')) return <a className="chip" href={href(['runs', runId], { root_cause_id: id })}>{id}</a>;
  if (keys?.[id]) return <a className="chip" href={href(['runs', runId, 'keys', keys[id]], { cycle, cite: id })}>{id}</a>;
  return <button type="button" className="chip" onClick={() => reveal(id)}>{id}</button>;
}

export function CitedText({ text, ...cites }: Cites & { text: string }) {
  const parts = [];
  let last = 0;
  for (const m of text.matchAll(CITATION)) {
    parts.push(text.slice(last, m.index), <Chip key={m.index} id={m[1]} {...cites} />);
    last = m.index + m[0].length;
  }
  parts.push(text.slice(last));
  return <>{parts}</>;
}

export function Chat({ runId, keyMasked, closed, ai }: { runId: string; keyMasked?: string; closed?: string; ai?: boolean }) {
  const scope = `ds.chat:${runId}:${keyMasked ?? ''}`;
  const [open, setOpen] = useState(false);
  const [session, setSession] = useState(() => sessionStorage.getItem(scope));
  const [messages, setMessages] = useState<Message[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  useEffect(() => {
    // its messages went with a purged run's evidence
    if (session && !closed) api.chat(session).then((s) => setMessages(s.messages), (e) => setError(message(e)));
  }, []); // the stored session, once: answers bring the whole session back

  async function ask(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const content = String(new FormData(form).get('q')).trim();
    if (!content) return;
    setBusy(true);
    setError(undefined);
    setMessages((m) => [...m, { role: 'user', content }]);
    try {
      const id = session ?? (await api.newChat(runId, keyMasked)).session_id;
      sessionStorage.setItem(scope, id);
      setSession(id);
      setMessages((await api.ask(id, content)).messages);
      form.reset();
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  }

  function restart() {
    sessionStorage.removeItem(scope);
    setSession(null);
    setMessages([]);
  }

  if (ai === false)
    return <span className="chat-toggle muted" title="No LLM profile is configured">AI is off</span>;
  if (!open)
    return (
      <button className="chat-toggle" onClick={() => setOpen(true)}>
        Ask AI about this {keyMasked ? 'key' : 'run'}
      </button>
    );
  return (
    <aside className="drawer">
      <header>
        <b>Chat about {keyMasked ? `key ${keyMasked}` : `run ${runId}`}</b>
        <span>
          <button onClick={restart}>New</button> <button onClick={() => setOpen(false)}>Close</button>
        </span>
      </header>
      <p className="muted">AI-generated, not evidence: check what it cites.</p>
      <div className="messages">
        {messages
          .filter((m) => m.role !== 'tool')
          .map((m, i) => (
            <div key={i} className={`msg ${m.role}`}>
              <CitedText text={m.content} runId={runId} />
            </div>
          ))}
      </div>
      {closed && <p className="muted" role="status">{closed}</p>}
      <Err error={error} />
      <form onSubmit={ask}>
        <textarea
          name="q"
          rows={3}
          disabled={!!closed}
          placeholder="Ask about these results. Questions are kept and sent as typed: use masked keys, no personal data."
        />
        <button disabled={busy || !!closed}>{busy ? 'Thinking…' : 'Ask'}</button>
      </form>
    </aside>
  );
}
