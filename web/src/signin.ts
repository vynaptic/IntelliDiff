/** What the sign-in page says beyond the server's answers (docs/mvp/FIRSTRUN.md §6, docs/mvp/AUTH.md §9).
 *  Nothing here comes from the server: an anonymous visitor learns its own address bar and a command's name.
 *  Pure, so that Node runs its tests and api.ts may use it: it loads no other module and asks no server. */

export const TOKEN_HINT =
  'An API token comes from the command line of the machine that runs DeltaSleuth: whoever administers it runs "ds token create" there and passes the token on. It is shown once. This page cannot create one.';

/** A token sign-in the server accepted and the browser dropped: /v1/auth/me then answers 401 `not signed in`. */
export const NOT_KEPT =
  'The token was accepted, but this browser did not send the session cookie back, so you are not signed in. Allow cookies for this address, or use another browser.';

const LOOPBACK = ['localhost', '127.0.0.1', '[::1]'];

/** What to try when a sign-in is refused as `cross-origin request refused`: the page was opened at another
 *  address than auth.public_url, which the server tells nobody. `at` is window.location. A guess from the
 *  address bar: on a loopback page, the other loopback names with the same scheme and port. */
export function addressHint(
  detail: string | undefined,
  at: { protocol: string; hostname: string; port: string; origin: string },
): string | undefined {
  if (detail !== 'cross-origin request refused') return undefined;
  const said = `DeltaSleuth takes a sign-in from one address only, the one in its settings (auth.public_url). This page was opened at ${at.origin}.`;
  if (!LOOPBACK.includes(at.hostname)) return `${said} Ask whoever runs it for the address.`;
  const others = LOOPBACK.slice(0, 2).filter((name) => name !== at.hostname);
  const tries = others.map((name) => `${at.protocol}//${name}${at.port && `:${at.port}`}`).join(' or ');
  return `${said} On the machine that runs it, try ${tries} instead; if that is refused too, the settings name another address.`;
}
