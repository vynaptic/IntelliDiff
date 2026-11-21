/** The page's own dialogs, in place of the browser's: a question with its two answers named, or a request for one
 *  value. One is open at a time, modal, and Escape leaves it. Its first button is the one that changes nothing,
 *  so that is where the keyboard starts, unless a value is asked for. */
import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';

interface Ask {
  title: string;
  body?: ReactNode;
  action: string; // what the confirming button does, as a verb: "Revoke token"
  dismiss?: string; // the other button; Cancel unless the action itself cancels something
  danger?: boolean; // the action cannot be undone
}
interface Input { label: string; initial?: string; required?: boolean; number?: { min: number; max: number } }
type Pending = Ask & { input?: Input; resolve: (answer: string | null) => void };

interface Dialogs {
  /** Whether the person chose the action. */
  confirm: (ask: Ask) => Promise<boolean>;
  /** The value typed, empty where none is required; null when the person left the dialog. */
  prompt: (ask: Ask & { input: Input }) => Promise<string | null>;
}

const Asking = createContext<Dialogs>({ confirm: async () => false, prompt: async () => null });
export const useDialogs = () => useContext(Asking);

export function DialogHost({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<Pending>();
  const box = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    if (pending && !box.current?.open) box.current?.showModal();
  }, [pending]);

  const dialogs = useMemo<Dialogs>(() => {
    const open = (ask: Ask & { input?: Input }) => new Promise<string | null>((resolve) => setPending({ ...ask, resolve }));
    return { confirm: (ask) => open(ask).then((answer) => answer !== null), prompt: open };
  }, []);

  function answer(value: string | null) {
    pending?.resolve(value);
    setPending(undefined);
  }

  const input = pending?.input;
  return (
    <Asking.Provider value={dialogs}>
      {children}
      {pending && (
        <dialog
          ref={box}
          className="ask"
          aria-labelledby="ask-title"
          onCancel={(e) => {
            e.preventDefault();
            answer(null);
          }}
        >
          <form
            onSubmit={(e) => {
              e.preventDefault();
              answer(String(new FormData(e.currentTarget).get('answer') ?? ''));
            }}
          >
            <h2 id="ask-title">{pending.title}</h2>
            {pending.body && <p>{pending.body}</p>}
            {input && (
              <label>
                {input.label}
                <input
                  className="wide"
                  name="answer"
                  type={input.number ? 'number' : 'text'}
                  {...input.number}
                  defaultValue={input.initial}
                  required={input.required}
                  autoComplete="off"
                  autoFocus
                />
              </label>
            )}
            <p className="inline actions">
              <button type="button" onClick={() => answer(null)}>{pending.dismiss ?? 'Cancel'}</button>
              <button className={pending.danger ? 'danger' : 'primary'}>{pending.action}</button>
            </p>
          </form>
        </dialog>
      )}
    </Asking.Provider>
  );
}
