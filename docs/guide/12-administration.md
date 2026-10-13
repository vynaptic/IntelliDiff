# 12. Administration

This chapter is for the person who looks after a DeltaSleuth installation: who may sign in and with
what, what the record of every action holds, how long results are kept, where the keys are, and how to
back the installation up, restore it and update it. It ends with what changes when the one-machine
trial of [chapter 1](01-install-docker.md) becomes an office deployment.

You need the installation of chapter 1 and its short form `ds` (chapter 1, section 5). Run every
command from the installation's folder, `IntelliDiff`. The sections build on each other: sections 3 to
7 use the tokens that section 2 makes, and section 11 uses the runs of sections 5 and 7.

**How this chapter was checked.** Every command was run on 2026-10-05, on macOS 27.0.1 (Apple
silicon, zsh) with Docker Desktop (Docker 29.1.3, Compose 2.40.3), on an installation made as chapter
1 says. The outputs printed here are real and shortened; `...` stands for what
was left out, `<you>` for your user name, and your ids and times will differ.

Not verified here: anything in a browser (every "In the web UI" part is written from the page's
source), anything on Linux, and single sign-on, for lack of an identity provider.

Three words are used throughout. A **run** is one comparison of many keys. A **cycle** is one pass of
a run over its keys: an ordinary (batch) run has one cycle. A **spec** is the file that says what to
compare and how.

## 1. How people sign in

### What it is for

An installation lets people in by exactly one of three ways, set in its settings file. Which one it
is decides who can administer it from the browser, whose name the record of actions shows, and
whether a second person can approve work. You need to know the way before you read anything else in
this chapter.

Two terms come up in the table. An **identity provider** is the organisation's own sign-in service,
the one its people already sign in to for their other applications, for example Entra ID or Okta. A
**TLS proxy** is a web server placed in front of the app that gives it an `https` address.

| | Developer mode | API tokens | Single sign-on |
|---|---|---|---|
| The health address says | `"auth": "none"` | `"auth": "tokens"` | `"auth": "oidc"` |
| In `deploy/config/deltasleuth.yaml` | no `auth` block | an `auth` block without `oidc` lines | an `auth` block with the `oidc` lines |
| Who signs in | Nobody. Every user and program on the machine acts as administrator | Each person and each script holds a named API token | People sign in at the organisation's identity provider; scripts hold API tokens |
| Where it works | Only DeltaSleuth run from source on your own machine ([chapter 2](02-run-from-source.md)). The Docker installation refuses it | Anywhere. The installation of chapter 1 uses it | Behind a TLS proxy, with an identity provider |
| The record of actions names | `dev:loopback` | The token, not the person who pasted it | The person |
| Roles | All but the two that only a signed-in person holds | `viewer`, `operator` or `spec_author`, one for each token | Six, from the person's groups at the identity provider |
| Administration in the browser | The Admin screen | None. Everything in this chapter is done with `ds` | The Admin screen, for people with the role `admin` |
| A second person's approval, sign-off, evidence packs | No | No. A run of a spec that would need an approval goes ahead, labelled unapproved | Yes |

The column for API tokens is what was seen here. The other two columns are taken from
[AUTH.md](../mvp/AUTH.md) and [DEPLOY.md](../mvp/DEPLOY.md), and were not tried here.

### Before you start

Nothing.

### In the web UI

1. Open `http://localhost:8000`. If a page headed "Runs" appears with a bar that ends in "Sign out",
   press "Sign out". You should see a panel headed "Sign in" and the sentence "You are signed out."
2. Read the panel. One field, "API token", and a button "Sign in" mean API tokens. One button, "Sign
   in with single sign-on", means single sign-on.
3. Developer mode shows no panel. There the page "Runs" opens at once, the bar has no "Sign out"
   button, and above every page stands a banner that begins "Developer mode: no sign-in, and every
   local user and process acts as admin." Only the address with `#/signin` at its end shows the panel
   in that mode, with the sentence "Developer mode needs no sign-in." and the link "Continue".

### On the command line

1. Ask the health address. It needs no sign-in.

   ```bash
   curl -fsS http://localhost:8000/v1/health
   ```

   You should see:

   ```text
   {"status": "ok", "ai": false, "auth": "tokens"}
   ```

### How to read the result

`"auth"` is the way people sign in: `none` is developer mode, `tokens` is API tokens, `oidc` is single
sign-on. `"status": "ok"` says the app reaches its database. `"ai": false` says AI chat is off.

### Limits

The way cannot be changed from a page or with a `ds` command, only in the settings file, followed by a
restart of the app. Single sign-on needs an identity provider and an `https` address (section 13).

### If something goes wrong

**`error: serving on 0.0.0.0 needs the auth settings (docs/mvp/AUTH.md §1)`**
The `auth` block was removed from the settings file of a Docker installation, to get developer mode.
The app refuses to start without it. Put the block back. Provoked here in a short-lived container
with a copy of the settings file without its `auth` lines.

## 2. API tokens

### What it is for

An API token is a named password. A person pastes it into the sign-in page, and a script sends it
with each request. Give every person and every machine a token of their own: the record of actions
names the token, and a token can be taken away without touching the others.

### Before you start

Nothing to switch on. You need the alias `ds` (chapter 1, section 5) and the folder
`.deltasleuth/tokens`, which the installation made. A token is a secret: the commands below write it
straight into a file that only you can read, and never show it.

The folder is missing in two cases: after section 13 of this chapter, which deletes it, and in a
fresh clone of the source (section 11). Then make it first. Run where the folder exists, the two
commands change nothing.

```bash
mkdir -p .deltasleuth/tokens
```

You should see no output.

```bash
chmod 700 .deltasleuth .deltasleuth/tokens
```

You should see no output.

### In the web UI

The web pages cannot create, list or revoke a token in this installation. No token can hold the role
`admin`, and the "Admin" link is shown only to an administrator who is a person, so a browser signed
in with a token never has it ([AUTH.md](../mvp/AUTH.md) says that administration is therefore done on
the command line). The sign-in page says so too: "An API token comes from the command line of the
machine that runs DeltaSleuth: whoever administers it runs "ds token create" there and passes the
token on. It is shown once. This page cannot create one."

Not verified here: under single sign-on, and in developer mode, the bar has an "Admin" link. Its
first tab, "Tokens", holds a form "New API token" with the fields "Name", "Owner", "Accountable
person", "Role" and "Days" and the button "Create". The new token then appears once in a panel headed
"Token `<name>` (`<token id>`)", with the sentence "Copy it now: it is not shown again. Role `<role>`,
expires `<time>`." and the buttons "Copy" and "Done". The table below lists every token, with a
"Revoke" button for each active one.

### On the command line

1. Create a token for a person. `<name>` is the token's name, 3 to 64 characters of lower-case
   letters, digits, `.`, `_` and `-`, starting with a letter or a digit. `<owner>` is the person or
   team who answers for it, in free text. `<role>` is `viewer`, `operator` or `spec_author` (section
   3). The file after `>` is where the token goes; `set -C` makes the shell refuse to write over a
   file that exists.

   ```bash
   (umask 077; set -C; docker compose run --rm -T -e DS_OPERATOR="$(id -un)" app ds token create --name <name> --owner "<owner>" --role <role> > .deltasleuth/tokens/<name>)
   ```

   You should see, here for the name `ann-viewer`, the owner `<you>` and the role `viewer`, the
   token's id, role and expiry, but not the token:

   ```text
    Container deltasleuth-db-1  Running
   {"id": 9, "at": "2026-10-05T20:05:20.768099+00:00", "actor": "local:<you>@deltasleuth-app", ... "action": "token.create", "object": "T-a447dea09648b1c6", ...}
   token T-a447dea09648b1c6 (ann-viewer), viewer, expires 2027-01-03T20:05:20+00:00; it is shown only now
   ```

2. Create a token for a machine, with a shorter life. `<days>` is how many days it lives, from 1 to
   90. Without `--days` a token lives 90 days.

   ```bash
   (umask 077; set -C; docker compose run --rm -T -e DS_OPERATOR="$(id -un)" app ds token create --name <name> --owner "<owner>" --role <role> --days <days> > .deltasleuth/tokens/<name>)
   ```

   You should see, here for `nightly-check`, the owner `Payments QA`, the role `operator` and 30 days:

   ```text
    Container deltasleuth-db-1  Running
   {"id": 10, "at": "2026-10-05T20:05:22.579495+00:00", "actor": "local:<you>@deltasleuth-app", ... "action": "token.create", "object": "T-832786e9346c7230", ...}
   token T-832786e9346c7230 (nightly-check), operator, expires 2026-11-04T20:05:22+00:00; it is shown only now
   ```

   The later sections use these two tokens under these names, and `first-run`, the token of chapter 1.

3. Check that each file holds one token, without printing it.

   ```bash
   grep -c '^dst_' .deltasleuth/tokens/ann-viewer .deltasleuth/tokens/nightly-check
   ```

   You should see:

   ```text
   .deltasleuth/tokens/ann-viewer:1
   .deltasleuth/tokens/nightly-check:1
   ```

4. List the tokens, newest first. The list never shows a token itself.

   ```bash
   ds token list
   ```

   You should see the two new tokens above the one of chapter 1. `last used` shows a time in place of
   `-` once a token was used, so `first-run` shows one if you signed in with it:

   ```text
    Container deltasleuth-db-1  Running
   T-832786e9346c7230 (nightly-check)  operator  active  expires 2026-11-04T20:05:22+00:00  owner 'Payments QA'  created by local:<you>@deltasleuth-app  last used -
   T-a447dea09648b1c6 (ann-viewer)  viewer  active  expires 2027-01-03T20:05:20+00:00  owner '<you>'  created by local:<you>@deltasleuth-app  last used -
   T-ecde506ce15b2702 (first-run)  spec_author  active  expires 2027-01-03T20:04:19+00:00  owner '<you>'  created by local:<you>@deltasleuth-app  last used -
   ```

5. Ask the app who a token is. The token goes from its file through a pipe to curl, so it appears
   neither on the screen nor in a list of running programs. A script sends its token the same way.

   ```bash
   { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/<name>; printf '"\n'; } | curl -sS -K - http://localhost:8000/v1/auth/me
   ```

   You should see, here for `nightly-check`:

   ```text
   {"actor": "token:T-832786e9346c7230", "name": "nightly-check", "display_name": null, "kind": "token", "roles": ["viewer", "operator"], "csrf": null, "mode": "tokens", "idle_minutes": 30, "expires_at": "2026-11-04T20:05:22.579495+00:00"}
   ```

6. Create one more token, only to take it away in the next step. Do not practise step 7 on the three
   tokens above: the later sections need them.

   ```bash
   (umask 077; set -C; docker compose run --rm -T -e DS_OPERATOR="$(id -un)" app ds token create --name old-ci --owner "$(id -un)" --role viewer --days 7 > .deltasleuth/tokens/old-ci)
   ```

   You should see:

   ```text
    Container deltasleuth-db-1  Running
   {"id": 11, "at": "2026-10-05T20:06:38.983578+00:00", "actor": "local:<you>@deltasleuth-app", ... "action": "token.create", "object": "T-7e658ed5055fb780", ...}
   token T-7e658ed5055fb780 (old-ci), viewer, expires 2026-10-12T20:06:38+00:00; it is shown only now
   ```

7. Take a token away. `<token id>` is the id at the start of its line in the list, here the id of
   `old-ci` that step 6 printed.

   ```bash
   ds token revoke <token id>
   ```

   You should see:

   ```text
    Container deltasleuth-db-1  Running
   {"id": 12, "at": "2026-10-05T20:06:49.614257+00:00", "actor": "local:<you>@deltasleuth-app", ... "action": "token.revoke", "object": "T-7e658ed5055fb780", ...}
   revoked token T-7e658ed5055fb780 (old-ci)
   ```

8. Check that the app refuses the revoked token. The last line is the status the app answered with.

   ```bash
   { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/old-ci; printf '"\n'; } | curl -sS -K - -w '\n%{http_code}\n' http://localhost:8000/v1/auth/me
   ```

   You should see:

   ```text
   {"detail": "invalid or expired API token"}
   401
   ```

9. Delete the file of a token you revoked, here `old-ci`.

   ```bash
   rm .deltasleuth/tokens/<name>
   ```

   You should see no output.

To hand a token to somebody, give them the file over a channel you trust for passwords.

**Replace a token before it expires.** The new token keeps the name and goes into a file of another
name, so that the old file stays usable until the switch. Tried here on a token named `spare`.

1. Create the new token: the command of step 1 with `-new` added to the file's name only.

   ```bash
   (umask 077; set -C; docker compose run --rm -T -e DS_OPERATOR="$(id -un)" app ds token create --name <name> --owner "<owner>" --role <role> > .deltasleuth/tokens/<name>-new)
   ```

   You should see Docker's line, an audit line and then, here for `spare`:

   ```text
   token T-dd4ff237f4fe714b (spare), viewer, expires 2027-01-03T20:20:18+00:00; it is shown only now
   ```

2. List the tokens (step 4). You should see two lines with that name; the upper one is the newer:

   ```text
   T-dd4ff237f4fe714b (spare)  viewer  active  expires 2027-01-03T20:20:18+00:00  owner '<you>'  created by local:<you>@deltasleuth-app  last used -
   T-d6d87a8e8c0a75b7 (spare)  viewer  active  expires 2027-01-03T20:20:16+00:00  owner '<you>'  created by local:<you>@deltasleuth-app  last used -
   ...
   ```

3. Give the person or the script the new file, then revoke the older token by its id (step 7). Here
   the last line read `revoked token T-d6d87a8e8c0a75b7 (spare)`; after it the old file was answered
   401 and the new one 200 by the request of step 8.

4. Put the new file in the old one's place.

   ```bash
   mv -f .deltasleuth/tokens/<name>-new .deltasleuth/tokens/<name>
   ```

   You should see no output.

### How to read the result

A line of the list holds the token's id, its name in brackets, its role, its state (`active` or
`revoked`), when it expires, its owner, who created it and when it was last used. `created by
local:<you>@deltasleuth-app` names the `ds` command line and the user name it was told (section 4).
`last used` moves at most once a minute.

In the answer of step 5, `roles` lists the token's role and every role below it. `expires_at` is the
token's own end.

### Limits

- A token holds one of three roles and never `admin`. In this installation nobody is an
  administrator in the browser.
- A token lives 90 days at most. Not verified here: the setting `token_max_days` in the `auth` block
  of the settings file, which [AUTH.md](../mvp/AUTH.md) says moves that limit (1 to 366).
- Not verified here: a token past its expiry. By [AUTH.md](../mvp/AUTH.md) the app refuses it as it
  refuses a revoked one.
- A revoked token stays in the list, marked `revoked`. The browser sessions opened with it end too:
  here the next request of such a session was answered 401 `session expired`.
- Token names may repeat. Two tokens of one name are told apart by their ids.
- Whoever signs in with a token is recorded as that token. A token shared by two people hides who did
  what.
- Not verified here: [AUTH.md](../mvp/AUTH.md) says that revoking a token does not stop the schedules
  that were saved with it; pause or delete those yourself.

### If something goes wrong

When `ds token create` refuses with one of the first four messages below, no token was made, but the
shell has already made the file: an empty file `.deltasleuth/tokens/<name>` is left. Delete it with
the command of step 9 before you run the corrected command. Otherwise the shell answers `file
exists`. Seen here after each of the four.

**`error: set DS_OPERATOR to your user name (docs/mvp/AUTH.md §4.2)`**
The command ran without `-e DS_OPERATOR="$(id -un)"`. Add it, or use the alias.

**`error: role must be one of operator, spec_author, viewer`**
You asked for another role, here `admin`. No token can be an administrator.

**`error: name must match ^[a-z0-9][a-z0-9._-]{2,63}$`**
The name has a capital letter or a space, here `Ann Viewer`.

**`error: days must be from 1 to 90`**
Met with `--days 91` and with `--days 0`.

**`file exists: .deltasleuth/tokens/ann-viewer`** (zsh), or **`.deltasleuth/tokens/ann-viewer: cannot
overwrite existing file`** (bash)
A file of that name is there already. `set -C` made the shell refuse to write over it, and no token
was created. If it is the empty file of a refused command, or the file of a revoked token, delete it.
If it holds a token in use, choose another name, or replace the token as shown above.

**`no such file or directory: .deltasleuth/tokens/back-in`** (zsh), or **`.deltasleuth/tokens/<name>:
No such file or directory`** (bash)
The folder `.deltasleuth/tokens` is missing, and no token was created. Met here after section 13 had
deleted the folder; the bash wording was provoked with another missing folder. Make the folder with
the two commands of "Before you start", then run the command again.

**`error: token T-0000000000000000 not found`**, or **`error: token T-7e658ed5055fb780 is already
revoked`**
The id is mistyped, or the token was revoked before. `ds token list` shows both.

**`{"detail": "invalid or expired API token"}`** (status 401)
The token was revoked, has expired, or is not a token of this installation. The app's log names the
id: `invalid or expired API token T-7e658ed5055fb780 request_id=Q-040bfa4b39389e82`.

## 3. Roles

### What it is for

A role says what a signed-in person or a token may do. Each role includes the ones above it in this
table, so an `operator` may also do what a `viewer` may.

| Role | May | Who can hold it |
|---|---|---|
| `viewer` | Read runs, results, differences and reports, the specs, the schedules and the Licence screen | A token, a person |
| `operator` | Also start, pause, resume and cancel runs, compare one key again, and set up schedules | A token, a person |
| `spec_author` | Also upload and check specs | A token, a person |
| `rule_approver` | Also approve or reject, as a second person, a spec and a run's sign-off | A person signed in by single sign-on |
| `pii_reader` | Reserved. The runbook says that showing raw values is not built yet | A person signed in by single sign-on |
| `admin` | Also the Admin screen: tokens, people, sessions and the audit log | A person signed in by single sign-on, and developer mode |

Tried here, with tokens: reading runs and the licence as a `viewer`, and the three refusals below;
uploading a spec as a `spec_author` and starting a run as an `operator`, which section 5 does in its
"Before you start"; and resuming a run as an `operator` (section 8). The rest of the table is the
runbook's ([RUNBOOK.md](../mvp/RUNBOOK.md), "Roles").

### Before you start

One token of each of the three roles: `ann-viewer` (`viewer`) and `nightly-check` (`operator`) from
section 2, and `first-run` (`spec_author`) from chapter 1.

### In the web UI

No screen shows your roles or changes them. The pages hide what your role does not allow:

1. Sign in with a `viewer` token and click "Runs". You should see the heading "Runs" and the table of
   runs, and no panel headed "New run".
2. Sign out, sign in with an `operator` token and click "Runs". You should see the panel "New run",
   with the fields "Spec" and "Mode", a larger field whose label begins "Keys, one per line", and the
   button "Start".
3. Click "Specs". You should see no panel headed "Upload a spec". Signed in with a `spec_author`
   token you see it, with the buttons "Validate" and "Save".
4. Look at the bar. It reads DeltaSleuth, Runs, Readiness, Specs, Schedules, Pre-checks, Licence, then
   the token's name and id, for example `first-run (T-ecde506ce15b2702)`, and "Sign out". It has no
   "Admin" link, whatever the token's role.

### On the command line

1. Read the runs with the `viewer` token. The last line is the status the app answered with.

   ```bash
   { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/ann-viewer; printf '"\n'; } | curl -sS -K - -w '\n%{http_code}\n' http://localhost:8000/v1/runs
   ```

   You should see, before any run was made (runs of earlier chapters are listed inside `items`):

   ```text
   {"items": [], "next_cursor": null}
   200
   ```

2. Try to start a run with the same token.

   ```bash
   { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/ann-viewer; printf '"\n'; } | curl -sS -K - -w '\n%{http_code}\n' -H 'Content-Type: application/json' -d '{"spec_id": 1, "mode": "batch"}' http://localhost:8000/v1/runs
   ```

   You should see the refusal:

   ```text
   {"detail": "needs the operator role"}
   403
   ```

3. Try to upload a spec with the `operator` token.

   ```bash
   { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/nightly-check; printf '"\n'; } | curl -sS -K - -w '\n%{http_code}\n' -H 'Content-Type: application/yaml' --data-binary @mocks/customer/spec.yaml http://localhost:8000/v1/specs
   ```

   You should see:

   ```text
   {"detail": "needs the spec_author role"}
   403
   ```

4. Try to read the audit log with the `spec_author` token, the highest a token can hold.

   ```bash
   { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - -w '\n%{http_code}\n' http://localhost:8000/v1/audit
   ```

   You should see:

   ```text
   {"detail": "needs the admin role"}
   403
   ```

### How to read the result

Status 403 with `needs the <role> role` means the caller is known and its role is too low. Status 401
means the caller is not known at all. Each of the three refusals above also left a row in the audit
log, with the action `authz.denied` and the role that was needed; section 5 shows them. By
[AUTH.md](../mvp/AUTH.md) a caller who repeats the same refused request gets one such row a minute at
most, with a count.

### Limits

A role cannot be changed after a token is made: create another token. Under single sign-on the roles
come from the person's groups at each sign-in and are set in the settings file (section 13), never
on a page.

### If something goes wrong

**`{"detail": "not signed in"}`** (status 401)
The request carried no token. Send one as the commands above do.

## 4. The `ds` command, the administrator's channel

### What it is for

`ds` is DeltaSleuth's command line. It works on the database directly and stands outside the roles:
whoever can run it in the installation's folder can do everything, also what no token may. In this
installation it is the only way to administer.

### Before you start

Define the alias of chapter 1, section 5, in each new terminal. It sets `DS_OPERATOR`, the name `ds`
records for what you do, to your user name:

```text
alias ds='docker compose run --rm -e DS_OPERATOR="$(id -un)" app ds'
```

### In the web UI

Not offered: this is the command line. What it does shows on the pages only as its results.

### On the command line

1. Run a command that only reads, without the name. `docker compose run --rm app ds` is `ds` without
   the alias.

   ```bash
   docker compose run --rm app ds token list
   ```

   You should see the list of section 2, step 4, with `old-ci` above it, marked `revoked`.

2. Run a command that leaves a row in the audit log, here a read of that log, without the name.

   ```bash
   docker compose run --rm app ds audit --limit 1
   ```

   You should see the refusal:

   ```text
    Container deltasleuth-db-1  Running
   error: set DS_OPERATOR to your user name (docs/mvp/AUTH.md §4.2)
   ```

3. Read what the command line recorded when it made the tokens of section 2.

   ```bash
   ds audit --action token.create
   ```

   You should see one row for each token, each done by `local:<you>@deltasleuth-app`, newest first:

   ```text
   11  2026-10-05T20:06:38+00:00  local:<you>@deltasleuth-app (<you>)  token.create  T-7e658ed5055fb780  {"asserted": true, "expires_at": "2026-10-12T20:06:38.983578+00:00", "name": "old-ci", "os_account": "deltasleuth", "owner": "<you>", "owner_user_id": null, "role": "viewer"}  request C-855dd2288203d393
   ...
   ```

### How to read the result

A `ds` command acts as `local:<name>@deltasleuth-app`. `<name>` is `DS_OPERATOR` as given, and
`deltasleuth-app` is the fixed host name of the app's container. Its rows carry `"asserted": true`,
because the name is taken on trust, and `"os_account": "deltasleuth"`, the account inside the
container.

Commands that change something, or that read the audit log, need the name and leave a row. Run here
under a name, each leaving its row: `ds token create` and `revoke`, `ds session revoke`, `ds audit`,
`ds purge` with `hold`, `release` and `--run`, and `ds run`. Run here without the name, `ds token
create`, `ds audit` and `ds purge hold` were refused. Commands that only look need no name and leave
no row. Run here without one: `ds token list`, `ds runs`, `ds licence`, `ds users`, `ds keys status`,
`ds keys fingerprint`, `ds audit verify`, `ds purge --dry-run` and `ds migrate`.
[AUTH.md](../mvp/AUTH.md) lists every command.

Many commands print, before their result, a line that starts ` Container` (Docker's) and lines that
start `{"id":`. The second kind are copies of the audit rows the command wrote (section 5).

### Limits

- The name is not checked. Anybody who may use Docker on the machine can run `ds` under any name. The
  record that counts is the machine's own record of who ran Docker; [DEPLOY.md](../mvp/DEPLOY.md)
  shows how an office keeps one.
- Each `ds` command starts a container, about two seconds, and removes it again.
- The container cannot see your files. A file goes in and out through the folder
  `/var/lib/deltasleuth/p0`, with `docker compose cp` (section 5 uses it). Files there are not
  encrypted and are never deleted for you.
- `ds` cannot pause, resume or cancel a run. That is done on the run's page or over the API.

### If something goes wrong

**`error: set DS_OPERATOR to your user name (docs/mvp/AUTH.md §4.2)`**
The alias is not defined in this terminal, or the long form lacks `-e DS_OPERATOR="$(id -un)"`.

**`command not found: ds`** (zsh)
Not met here; the [appendix](A1-install-by-hand.md) quotes it. The alias is not defined in this
terminal: define it again.

## 5. The audit log

### What it is for

The audit log is DeltaSleuth's record of who did what: sign-ins, tokens made and revoked, specs
uploaded, runs started and ended, keys looked at, reports fetched, evidence held or deleted, keys
changed, each start of the app. Rows can only be added. An administrator reads it to answer "who did
this, and when", and checks from time to time that no row was changed afterwards.

### Before you start

Nothing to switch on: the log is always on. The outputs below show the tokens of section 2 and the
three refusals of section 3. They also show a spec and a run, so make both now, on the bundled mock
pair, a pair of practice services that chapter 1 started.

1. Upload the mock pair's spec with `first-run`, the token of chapter 1. Uploading the same spec
   again later changes nothing and gives the same id.

   ```bash
   { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - -H 'Content-Type: application/yaml' --data-binary @mocks/customer/spec.yaml http://localhost:8000/v1/specs
   ```

   You should see the spec's number, `spec_id`:

   ```text
   {"spec_id": 1, "name": "mock-customer-v1-vs-v2", "version": "1.0.0", "content_hash": "dadc627b4d0f5aa253144045abd73643d545e0d9d5fe4553975fbf37ddde2f6e", "needs": ["clear", "known_diffs"], "approval_id": null}
   ```

2. Start a run of it with `nightly-check`, the `operator` token. Write your `spec_id` in place of 1
   if it differs.

   ```bash
   { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/nightly-check; printf '"\n'; } | curl -sS -K - -H 'Content-Type: application/json' -d '{"spec_id": 1, "mode": "batch"}' http://localhost:8000/v1/runs
   ```

   You should see the run's id:

   ```text
   {"run_id": "R-35T7BF8BQNHNGGNKSHP8C4WM91", "warnings": []}
   ```

3. Wait ten seconds, then list the runs.

   ```bash
   ds runs
   ```

   You should see the run `completed`. Its second line is there because nobody can approve a spec in
   this installation (section 1).

   ```text
    Container deltasleuth-db-1  Running
   R-35T7BF8BQNHNGGNKSHP8C4WM91  mock-customer-v1-vs-v2 1.0.0  batch  completed  cycle 1  different 4 of 40
     Unapproved spec version: clear, known_diffs were not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
   ```

### In the web UI

Not offered in this installation: the log is on the "Audit" tab of the Admin screen, which a browser
signed in with a token never has (section 2). No page checks the log for changes, in any way of
signing in.

Not verified here: under single sign-on, and in developer mode, "Admin", then the tab "Audit", shows
a form with the fields `actor`, `action`, `object`, `session_id`, `since` and `until`, the button
"Filter" and the link "clear", then a table with the columns Id, At, Actor, Action, Object, Detail,
Request, Session, Client and Chain, and a button "More" for older rows.

### On the command line

1. Read the newest rows. Without options `ds audit` prints the newest 100; `--limit <number>` asks
   for another number, from 1 to 500.

   ```bash
   ds audit
   ```

   You should see Docker's line, the record of this very read as a line that starts `{"id":`, then
   the rows, newest first:

   ```text
    Container deltasleuth-db-1  Running
   {"id": 22, "at": "2026-10-05T20:08:01.061797+00:00", "actor": "local:<you>@deltasleuth-app", "actor_name": "<you>", "action": "audit.read", "object": "audit", "detail": {"filters": {}, "asserted": true, "os_account": "deltasleuth"}, "request_id": "C-6ca07e7d390ab2b0", "session_id": null, "client_addr": null, "chain": {"seq": 22, "hash": "c975da14..."}}
   21  2026-10-05T20:07:45+00:00  system:worker (worker)  readiness.changed  readiness:mock-customer-v1-vs-v2  {...}
   20  2026-10-05T20:07:45+00:00  system:worker (worker)  readiness.measured  readiness  {...}
   19  2026-10-05T20:07:45+00:00  system:worker (worker)  run.finished  R-35T7BF8BQNHNGGNKSHP8C4WM91  {"reason": null, "status": "completed"}
   18  2026-10-05T20:07:42+00:00  token:T-832786e9346c7230 (nightly-check)  create_run  R-35T7BF8BQNHNGGNKSHP8C4WM91  {...}  request Q-361d82b044f92424  from 172.30.0.1
   17  2026-10-05T20:07:42+00:00  token:T-ecde506ce15b2702 (first-run)  spec.create  1  {"content_hash": "dadc627b..."}  request Q-10765a687da0b6e2  from 172.30.0.1
   16  2026-10-05T20:07:35+00:00  local:<you>@deltasleuth-app (<you>)  audit.read  audit  {"asserted": true, "filters": {"action": "token.create"}, "os_account": "deltasleuth"}  request C-af6e22a83f98dee2
   15  2026-10-05T20:07:30+00:00  token:T-ecde506ce15b2702 (first-run)  authz.denied  GET /v1/audit  {"count": 1, "needed": "admin"}  request Q-67c51409b03109e6  from 172.30.0.1
   ...
   5  2026-10-05T20:04:12+00:00  system:server (server)  system.start  server  {"audit_head": {...}, "auth": "tokens", "current_kek": "K-2004822248ff3b04", ..., "retention": {"audit_days": 365, "standard_days": 90}, ..., "version": "0.1.0.dev0"}
   ...
   1  2026-10-05T20:04:10+00:00  system:migration (migration)  installation.created  I-371185ef64443987  {"installation_id": "I-371185ef64443987"}
   ```

   By the command's source the record of the read is written before the rows are printed, so a
   terminal shows them in this order.

2. Read the rows of one kind of action.

   ```bash
   ds audit --action token.create
   ```

   You should see one row for each token made, newest first:

   ```text
   11  2026-10-05T20:06:38+00:00  local:<you>@deltasleuth-app (<you>)  token.create  T-7e658ed5055fb780  {..., "name": "old-ci", ..., "role": "viewer"}  request C-855dd2288203d393
   10  2026-10-05T20:05:22+00:00  local:<you>@deltasleuth-app (<you>)  token.create  T-832786e9346c7230  {..., "name": "nightly-check", ..., "role": "operator"}  request C-453d59aaac390ded
   9  2026-10-05T20:05:20+00:00  local:<you>@deltasleuth-app (<you>)  token.create  T-a447dea09648b1c6  {..., "name": "ann-viewer", ..., "role": "viewer"}  request C-3d6b107a47f1d11f
   8  2026-10-05T20:04:19+00:00  local:<you>@deltasleuth-app (<you>)  token.create  T-ecde506ce15b2702  {..., "name": "first-run", ..., "role": "spec_author"}  request C-51791190c68cc404
   ```

3. Read what one caller did. `<actor>` is the word in the third column of any row, for example
   `token:T-832786e9346c7230`; for a token it is `token:` and the id that `ds token list` shows.

   ```bash
   ds audit --actor <actor>
   ```

   You should see, here for the token `nightly-check`, the run it started and the upload it was
   refused:

   ```text
   18  2026-10-05T20:07:42+00:00  token:T-832786e9346c7230 (nightly-check)  create_run  R-35T7BF8BQNHNGGNKSHP8C4WM91  {"approval": {"mode": "tokens", "needs": ["clear", "known_diffs"]}, ..., "keys": 40, ..., "mode": "batch", ..., "spec_id": 1}  request Q-361d82b044f92424  from 172.30.0.1
   14  2026-10-05T20:07:30+00:00  token:T-832786e9346c7230 (nightly-check)  authz.denied  POST /v1/specs  {"count": 1, "needed": "spec_author"}  request Q-15aa6f7984164f35  from 172.30.0.1
   ```

4. Read everything about one thing. `<object>` is the fifth column: a run's id, a token's id, a
   session's id. Take the id of the run you started in "Before you start".

   ```bash
   ds audit --object <object>
   ```

   You should see, here for the run `R-35T7BF8BQNHNGGNKSHP8C4WM91`:

   ```text
   19  2026-10-05T20:07:45+00:00  system:worker (worker)  run.finished  R-35T7BF8BQNHNGGNKSHP8C4WM91  {"reason": null, "status": "completed"}
   18  2026-10-05T20:07:42+00:00  token:T-832786e9346c7230 (nightly-check)  create_run  R-35T7BF8BQNHNGGNKSHP8C4WM91  {...}  request Q-361d82b044f92424  from 172.30.0.1
   ```

5. Read the rows of a span of time. Times are in UTC, written as `2026-10-05T20:07:40`. `--since`
   takes rows from that time on and `--until` rows before that time. Take your two times from the
   second column of step 4's output: a few seconds before the run's start, and a second or two after
   it.

   ```bash
   ds audit --since <time> --until <time>
   ```

   You should see, here from `2026-10-05T20:07:40` until `2026-10-05T20:07:44`, the two rows of that
   time:

   ```text
   18  2026-10-05T20:07:42+00:00  token:T-832786e9346c7230 (nightly-check)  create_run  R-35T7BF8BQNHNGGNKSHP8C4WM91  {...}  request Q-361d82b044f92424  from 172.30.0.1
   17  2026-10-05T20:07:42+00:00  token:T-ecde506ce15b2702 (first-run)  spec.create  1  {"content_hash": "dadc627b4d0f5aa253144045abd73643d545e0d9d5fe4553975fbf37ddde2f6e"}  request Q-10765a687da0b6e2  from 172.30.0.1
   ```

6. Get rows in full, for a program. `--json` works with every filter.

   ```bash
   ds audit --action create_run --json
   ```

   You should see each row with all its fields:

   ```text
   [
     {
       "action": "create_run",
       "actor": "token:T-832786e9346c7230",
       "actor_name": "nightly-check",
       "at": "2026-10-05T20:07:42.356569+00:00",
       "chain": {
         "hash": "6ed2ced2044ab30c3649d7d21e34132b2b926940ddf9051840e76de817beb37b",
         "seq": 18
       },
       "client_addr": "172.30.0.1",
       "detail": {
         ...
       },
       "id": 18,
       "object": "R-35T7BF8BQNHNGGNKSHP8C4WM91",
       "request_id": "Q-361d82b044f92424",
       "session_id": null
     }
   ]
   ```

7. Check that no row was changed, removed or slipped in. Each row is tied to the row before it by a
   fingerprint, so a change to an old row breaks every later tie. This command checks every tie. It
   needs no `DS_OPERATOR` and writes nothing.

   ```bash
   ds audit verify
   ```

   You should see the number of rows checked and the newest row's fingerprint, the head:

   ```text
    Container deltasleuth-db-1  Running
   chain: 27 row(s) verified
   head: row 27 a63948111f0fc3ce2cda97e9fe18e40c59cac4b47f901a76ce1fa31934a5a41e
   ```

8. See the second copy of the log. Each audit row is also printed as one line that starts `{"id":`.
   The app prints its rows into its own log; count them there.

   ```bash
   docker compose logs --no-log-prefix app 2> /dev/null | grep -c '^{"id":'
   ```

   You should see a number, here:

   ```text
   13
   ```

   A `ds` command prints its rows on its standard error, the stream for messages, so that its
   standard output stays its result. With the messages thrown away only the rows are left. This needs
   the long form with `-T`:

   ```bash
   docker compose run --rm -T -e DS_OPERATOR="$(id -un)" app ds audit --limit 3 2> /dev/null
   ```

   You should see rows only, here the three newest, and neither Docker's line nor a line that starts
   `{"id":`:

   ```text
   27  2026-10-05T20:08:17+00:00  local:<you>@deltasleuth-app (<you>)  audit.read  audit  {"asserted": true, "filters": {"action": "create_run"}, "os_account": "deltasleuth"}  request C-5de03c8c790c209e
   26  2026-10-05T20:08:15+00:00  local:<you>@deltasleuth-app (<you>)  audit.read  audit  {"asserted": true, "filters": {"since": "2026-10-05T20:07:40+00:00", "until": "2026-10-05T20:07:44+00:00"}, "os_account": "deltasleuth"}  request C-7469e406da70a9ec
   25  2026-10-05T20:08:13+00:00  local:<you>@deltasleuth-app (<you>)  audit.read  audit  {"asserted": true, "filters": {"object": "R-35T7BF8BQNHNGGNKSHP8C4WM91"}, "os_account": "deltasleuth"}  request C-092d93ab5d6aca08
   ```

9. Compare the log with a copy that was collected outside it. An office sends these lines, as they
   are written, to a system that collects logs; DeltaSleuth's messages call it the SIEM. The check
   wants a copy without a gap, and on this trial only the app's lines are kept (step 11 says why). So
   save one stretch of the app's lines that has no gap. The `awk` part keeps the longest run of lines
   whose row numbers follow one another.

   ```bash
   docker compose logs --no-log-prefix app 2> /dev/null | grep '^{"id":' | awk -F'[ ,]' '{ if ($2 == prev + 1) { buf = buf "\n" $0; n++ } else { buf = $0; n = 1 } prev = $2; if (n > best) { best = n; keep = buf } } END { print keep }' > ~/app-lines.jsonl
   ```

   You should see no output.

10. Let the container read the file, then copy it into the folder that `ds` can see.

    ```bash
    chmod a+r ~/app-lines.jsonl
    ```

    You should see no output.

    ```bash
    docker compose cp ~/app-lines.jsonl app:/var/lib/deltasleuth/p0/siem.jsonl
    ```

    You should see a line that ends in `Copied`. Where the output does not go to a terminal, the
    same line ending in `Copying` comes first.

    ```text
     deltasleuth-app-1 copy <folder>/app-lines.jsonl to deltasleuth-app-1:/var/lib/deltasleuth/p0/siem.jsonl Copied
    ```

11. Check the log against the file, both ways.

    ```bash
    docker compose run --rm app ds audit verify --siem /var/lib/deltasleuth/p0/siem.jsonl
    ```

    You should see one more line than in step 7, with the number of lines that match and the rows
    they span. Here the stretch was the five rows of the app's first start:

    ```text
     Container deltasleuth-db-1  Running
    chain: 28 row(s) verified
    siem: 5 line(s) match: rows 3 to 7
    head: row 28 029a05e0845d7a38c82bb9a8d2117983a6f6e5d097245591b205b5e5aa53de7d
    ```

    The check covers the span from the file's first row to its last, and every row of the log
    inside that span must have its line in the file. That is why step 9 cut the file down. With all
    of the app's lines in the file the same command stopped at the first row a `ds` command wrote:

    ```text
    error: row 8 (token.create T-ecde506ce15b2702) has no SIEM line: it was not written by DeltaSleuth, or the SIEM lost it
    ```

    Row 8 was written by `ds token create`. Its copy went to the standard error of a short-lived
    container, and Docker removed that container with its lines. On this trial the message is no sign
    of tampering: the app's log alone is not a full copy.

12. Remove both files again: nothing deletes them for you.

    ```bash
    docker compose run --rm --no-deps app rm /var/lib/deltasleuth/p0/siem.jsonl
    ```

    You should see no output.

    ```bash
    rm ~/app-lines.jsonl
    ```

    You should see no output.

### How to read the result

A row reads, from left to right: its number; the time, in UTC; who did it, with the name in brackets;
the action; the thing it was done to; details, as JSON; then, where they exist, `request` and an id,
`session` and a browser session's id, and `from` and the caller's address.

| Who | Is |
|---|---|
| `token:T-...` | An API token, sent by a script or signed in at the browser |
| `local:<name>@deltasleuth-app` | A `ds` command, under the name it was told |
| `system:server`, `system:worker`, `system:migration` | DeltaSleuth itself: the app's start, its background work, a change of the database's tables or keys |
| `user:U-...` | A person signed in by single sign-on (not in this installation) |

Actions met in this chapter: `system.start` (the app started; its details name the way of signing in,
the retention days and the keys), `token.create` and `token.revoke`, `auth.login` (a browser signed
in), `session.revoke`, `authz.denied` (a role was too low), `spec.create`, `create_run`,
`run.finished`, `key.view` (somebody opened one key), `audit.read` (somebody read this log),
`retention.hold`, `retention.release`, `retention.expire` and `retention.purged` (section 7),
`keys.created` and `keys.rewrap` (section 8). [AUTH.md](../mvp/AUTH.md) lists every action.

A request id that starts `Q-` belongs to a request to the app, one that starts `C-` to a `ds`
command. The details never hold a raw key, a value or a secret. The address `172.30.0.1` is Docker's
own: on this trial every caller arrives through it, so the column tells callers apart only behind a
proxy that is set up as [DEPLOY.md](../mvp/DEPLOY.md) says.

The `head` line of `ds audit verify` is worth writing down each day, outside the machine.
[DEPLOY.md](../mvp/DEPLOY.md) files it in a daily ticket, as a fixed point that later checks start
from.

### Limits

- Filters match whole values exactly. There is no search for part of a word.
- `--limit` goes from 1 to 500. For older rows, narrow the time with `--until`.
- Every read with `ds audit` adds a row, `audit.read`, with the filters used.
- Not recorded: a request without a valid token or session (it is in the app's log only), and plain
  listings of runs, specs and the like.
- The log is never shortened: no retention applies to it.
- The check of step 7 finds changes made behind DeltaSleuth's back. Somebody who owns the database
  could rewrite the log and its ties together. Only a copy collected outside, line by line as it is
  written, shows that, and it must hold the lines of every `ds` container too. Docker's default log
  setting drops those with the container. [DEPLOY.md](../mvp/DEPLOY.md), "The SIEM copy and host
  auditing", names a setting that keeps them. Not verified here: that setting, and a check against a
  full copy. Step 11 checked a stretch of five rows only.

### If something goes wrong

**`Invalid value for '--limit': 501 is not in the range 1<=x<=500.`** (in a box, under a `Usage:`
line)
Ask for 500 rows at most.

**`error: --since 'yesterday' is not an ISO 8601 time, e.g. 2026-09-26T08:00:00Z`**
Write the time as the message shows.

**`[]`** from a command with `--json`, or no rows at all from a filter
No row matches. A filter matches the whole value, so check the id for a missing letter. For `--action
create_run`, a run must have been started ("Before you start").

**`error: row 8 (token.create T-ecde506ce15b2702) has no SIEM line: it was not written by
DeltaSleuth, or the SIEM lost it`**
The file given to `--siem` lacks the line of a row inside the span it covers. On a trial that is a
row a `ds` command wrote (step 11); make the file with the command of step 9. On an office
installation with a full collection it would mean a row was added behind DeltaSleuth's back, or the
collector lost a line.

**`error: No such file or directory: /var/lib/deltasleuth/p0/nope.jsonl`**
The path after `--siem` is a path inside the container. Copy the file in first (step 10).

## 6. Sessions

### What it is for

A session is one browser's sign-in. It starts when somebody signs in and ends at "Sign out", after 30
minutes without use, or after 8 hours. An administrator ends a session early when a token was passed
on by mistake or a machine was left signed in. The two times are what the app reported for a session
here (`"idle_minutes": 30`, and an end 8 hours after the sign-in); neither was waited for.

### Before you start

At least one live session. Whoever signed in at the browser (chapter 1, section 4) has one. The steps
below were run without a browser, on two sessions opened with the request that the "Sign in" button
sends. Open them the same way if you want your output to match.

`-c` makes curl keep the session's cookie, the value a browser sends back with every request, in a
file. Whoever holds that file acts as the token for as long as the session lives. So the commands
make it readable by you alone, inside the closed folder `.deltasleuth`, and the last step of this
section deletes it.

1. Sign in as `ann-viewer`.

   ```bash
   (umask 077; { printf '{"token": "'; tr -d '\n' < .deltasleuth/tokens/ann-viewer; printf '"}'; } | curl -sS -c .deltasleuth/ann-viewer.cookies -H 'Origin: http://localhost:8000' -H 'Content-Type: application/json' --data-binary @- -o /dev/null -w '%{http_code}\n' http://localhost:8000/v1/auth/token-login)
   ```

   You should see the status of the sign-in:

   ```text
   200
   ```

2. Sign in as `nightly-check`.

   ```bash
   (umask 077; { printf '{"token": "'; tr -d '\n' < .deltasleuth/tokens/nightly-check; printf '"}'; } | curl -sS -c .deltasleuth/nightly-check.cookies -H 'Origin: http://localhost:8000' -H 'Content-Type: application/json' --data-binary @- -o /dev/null -w '%{http_code}\n' http://localhost:8000/v1/auth/token-login)
   ```

   You should see:

   ```text
   200
   ```

### In the web UI

Not offered in this installation: sessions are on the "Sessions" tab of the Admin screen, which a
browser signed in with a token never has (section 2). The only control is your own "Sign out" button,
at the end of the bar: it ends your session and shows the panel "Sign in" with the sentence "You are
signed out."

Not verified here: under single sign-on, and in developer mode, "Admin", then "Sessions", lists the
live sessions under the sentence "Live sessions, newest first. A revoked session ends at its next
request.", with the columns Session, Who, Roles, Client, Started, Authenticated, Last seen and Ends
and a "Revoke" button in each row, which asks "End session `<id>` of `<name>`?".

### On the command line

1. List the sign-ins. No `ds` command lists sessions, so read them from the audit log: each sign-in
   is a row, and its fifth column is the session's id.

   ```bash
   ds audit --action auth.login
   ```

   You should see one row for each sign-in:

   ```text
   30  2026-10-05T20:08:58+00:00  token:T-832786e9346c7230 (nightly-check)  auth.login  W-1398f81db91b05d1  {"method": "token", "reauth": false, "roles": ["viewer", "operator"]}  request Q-b470844c52598b4f  session W-1398f81db91b05d1  from 172.30.0.1
   29  2026-10-05T20:08:58+00:00  token:T-a447dea09648b1c6 (ann-viewer)  auth.login  W-eab7ce69ef4681b9  {"method": "token", "reauth": false, "roles": ["viewer"]}  request Q-d997940ec1e1252e  session W-eab7ce69ef4681b9  from 172.30.0.1
   ```

2. Check that the session of `ann-viewer` is live. `-b` sends the cookie from its file. The command
   prints only the status, because the answer itself holds a value of the session.

   ```bash
   curl -sS -b .deltasleuth/ann-viewer.cookies -o /dev/null -w '%{http_code}\n' http://localhost:8000/v1/auth/me
   ```

   You should see:

   ```text
   200
   ```

3. End one session. `<session id>` is the id that starts `W-`, here the one in the row of
   `ann-viewer`.

   ```bash
   ds session revoke --session <session id>
   ```

   You should see:

   ```text
    Container deltasleuth-db-1  Running
   {"id": 32, "at": "2026-10-05T20:09:23.733490+00:00", "actor": "local:<you>@deltasleuth-app", ... "action": "session.revoke", "object": "W-eab7ce69ef4681b9", "detail": {"count": 1, ...}, ...}
   revoked 1 session(s)
   ```

4. Ask again with the same cookie, this time with the answer shown.

   ```bash
   curl -sS -b .deltasleuth/ann-viewer.cookies -w '\n%{http_code}\n' http://localhost:8000/v1/auth/me
   ```

   You should see that the session is over:

   ```text
   {"detail": "session expired"}
   401
   ```

5. See what one session did: filter the log by its id.

   ```bash
   ds audit --session <session id>
   ```

   You should see every row recorded under that session, here only its sign-in:

   ```text
   29  2026-10-05T20:08:58+00:00  token:T-a447dea09648b1c6 (ann-viewer)  auth.login  W-eab7ce69ef4681b9  {"method": "token", "reauth": false, "roles": ["viewer"]}  request Q-d997940ec1e1252e  session W-eab7ce69ef4681b9  from 172.30.0.1
   ```

6. End every session. Everybody signs in again; tokens stay valid.

   ```bash
   ds session revoke --all
   ```

   You should see how many were live:

   ```text
    Container deltasleuth-db-1  Running
   {"id": 34, "at": "2026-10-05T20:09:27.490865+00:00", "actor": "local:<you>@deltasleuth-app", ... "action": "session.revoke", "object": "all", "detail": {"count": 1, ...}, ...}
   revoked 1 session(s)
   ```

7. Delete the two cookie files of "Before you start".

   ```bash
   rm .deltasleuth/ann-viewer.cookies .deltasleuth/nightly-check.cookies
   ```

   You should see no output.

### How to read the result

A revoked session ends at its next request, as step 4 shows. After step 3 the other session still
answered 200, and the token `ann-viewer` itself still worked when a script sent it (section 2, step
5). After step 6 the second session was answered 401 too. By the page's source a browser then shows
the panel "Sign in" at its next click.

The filter of step 5 shows what was recorded under one session. A session that had opened one key of
a run showed two rows in a later check: its `auth.login` and a `key.view`, both ending in
`session` and the same id.

### Limits

- The list of step 1 is a list of sign-ins, not of live sessions. A session in it may have ended
  since: by `ds session revoke` (a row `session.revoke`), and by [AUTH.md](../mvp/AUTH.md) also by
  "Sign out" (a row `auth.logout`) or by time, which leaves no row.
- Ending a session does not take the token away. To lock somebody out, revoke the token (section 2):
  that ends its sessions too.
- `ds session revoke --user` and `ds users` are for people who signed in by single sign-on. Here `ds
  users` printed nothing.
- The 30 minutes and the 8 hours are the settings `session_idle_minutes` and `session_max_hours` of
  the `auth` block; the [appendix](A1-install-by-hand.md), section 5.1, changes the first.

### If something goes wrong

**`error: name one of --session W-…, --user U-… or --all`**
`ds session revoke` needs one of the three.

**`error: session W-eab7ce69ef4681b9 not found`**
No live session has that id: it is mistyped, or the session ended already.

**`revoked 0 session(s)`** from `ds session revoke --all`
No session was live. Nothing is wrong.

**`{"detail": "cross-origin request refused"}`** (status 403) from a sign-in of "Before you start"
The `Origin` in the command is not the installation's own address. Met here with
`http://127.0.0.1:8000` in place of `http://localhost:8000`. Write the address of chapter 1, exactly.

## 7. Retention: how long results are kept, holding and erasing

### What it is for

DeltaSleuth deletes what a run collected a set number of days after the run's cycle ended, so that
copies of your data do not pile up. An administrator sets the number of days, can hold one run's data
for as long as an investigation or a legal case needs it, and can erase one run at once when asked
to.

What goes when a cycle's time is up: the keys it compared, what each side answered, the results, the
differences found and their causes, and the chat about them. The stored answers themselves, which
runs share, go 24 hours after the last run that needed them. What is never deleted: the run's line
with its counts, the specs, and the audit log.

### Before you start

The days are two lines of `deploy/config/deltasleuth.yaml`, as installed:

```text
retention:                                       # days raw data is kept after each cycle ends, by the spec's class
  standard_days: 90                              # 7..3650
  audit_days: 365                                # 7..3650, at least standard_days
```

A spec chooses its class with the line `retention: standard` or `retention: audit`; without the line
it is `standard`. Leave the two numbers as they are for now.

This section erases a run at its end, so make a second, fresh run of the bundled mock pair for it and
keep the run of section 5. `first-run` is the token of chapter 1.

1. Upload the mock pair's spec. After section 5 this changes nothing and gives the same id.

   ```bash
   { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - -H 'Content-Type: application/yaml' --data-binary @mocks/customer/spec.yaml http://localhost:8000/v1/specs
   ```

   You should see the spec's number, `spec_id`:

   ```text
   {"spec_id": 1, "name": "mock-customer-v1-vs-v2", "version": "1.0.0", "content_hash": "dadc627b4d0f5aa253144045abd73643d545e0d9d5fe4553975fbf37ddde2f6e", "needs": ["clear", "known_diffs"], "approval_id": null}
   ```

2. Start a run of it. Write your `spec_id` in place of 1 if it differs.

   ```bash
   { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - -H 'Content-Type: application/json' -d '{"spec_id": 1, "mode": "batch"}' http://localhost:8000/v1/runs
   ```

   You should see the run's id. It is written `<run id>` below.

   ```text
   {"run_id": "R-35T7BPBMM7JCHXD1YFE2H9KT6Y", "warnings": []}
   ```

3. Wait ten seconds, then list the runs.

   ```bash
   ds runs
   ```

   You should see the run `completed`, above the run of section 5:

   ```text
    Container deltasleuth-db-1  Running
   R-35T7BPBMM7JCHXD1YFE2H9KT6Y  mock-customer-v1-vs-v2 1.0.0  batch  completed  cycle 1  different 4 of 40
     Unapproved spec version: clear, known_diffs were not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
   R-35T7BF8BQNHNGGNKSHP8C4WM91  mock-customer-v1-vs-v2 1.0.0  batch  completed  cycle 1  different 4 of 40
     Unapproved spec version: clear, known_diffs were not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
   ```

### In the web UI

The pages show until when a run's data is kept, and that it is held or gone. They cannot hold,
release or erase: [DEPLOY.md](../mvp/DEPLOY.md) says these stay with whoever administers the machine.

1. Click "Runs", then the run's id in the column "Run". You should see a page headed "Run `<run
   id>`". Under the heading stand grey lines: the first names the spec and who started the run, the
   second reads "Evidence retained until 2027-01-03 (retention standard)", with your own date.
2. After step 2 below, load the page again. The line reads "Evidence held until released (retention
   standard): held by local:`<you>`@deltasleuth-app since 2026-10-05, legal hold LH-7".
3. After step 5 below, load the page again. That line is gone, and in place of the tables "Root
   causes", "Results" and "Findings" stands "Cycle 1 was purged: its keys, results, findings and
   evidence are deleted."

### On the command line

1. See until when the run's data is kept. `python3` only cuts the answer down to its `retention`
   part.

   ```bash
   { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - http://localhost:8000/v1/runs/<run id> | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["retention"], indent=1))'
   ```

   You should see the class, its days and the day cycle 1 expires:

   ```text
   {
    "class": "standard",
    "days": 90,
    "hold": null,
    "purged": [],
    "last_purged_at": null,
    "cycles": [
     {
      "cycle": 1,
      "ended_at": "2026-10-05T20:09:45.221041+00:00",
      "expires_at": "2027-01-03T20:09:45.221041+00:00"
     }
    ]
   }
   ```

2. Hold the run's data. The reason is kept for good in the audit log: write a case number, never a
   person's name or a customer's key.

   ```bash
   ds purge hold <run id> --reason "legal hold LH-7"
   ```

   You should see:

   ```text
    Container deltasleuth-db-1  Running
   {"id": 40, "at": "2026-10-05T20:10:02.784261+00:00", "actor": "local:<you>@deltasleuth-app", ... "action": "retention.hold", "object": "R-35T7BPBMM7JCHXD1YFE2H9KT6Y", "detail": {"reason": "legal hold LH-7", ...}, ...}
   held run R-35T7BPBMM7JCHXD1YFE2H9KT6Y: its evidence stays until 'ds purge release R-35T7BPBMM7JCHXD1YFE2H9KT6Y'
   ```

   The command of step 1 then shows the hold in place of `"hold": null`:

   ```text
    "hold": {
     "reason": "legal hold LH-7",
     "by": "local:<you>@deltasleuth-app",
     "at": "2026-10-05T20:10:02.784261+00:00"
    },
   ```

3. Ask what the next clean-up would delete. This command writes nothing.

   ```bash
   ds purge --dry-run
   ```

   You should see, while nothing is past its days:

   ```text
    Container deltasleuth-db-1  Running
   nothing to purge
   ```

4. Release the hold.

   ```bash
   ds purge release <run id>
   ```

   You should see:

   ```text
    Container deltasleuth-db-1  Running
   {"id": 41, "at": "2026-10-05T20:10:10.364940+00:00", "actor": "local:<you>@deltasleuth-app", ... "action": "retention.release", "object": "R-35T7BPBMM7JCHXD1YFE2H9KT6Y", ...}
   released run R-35T7BPBMM7JCHXD1YFE2H9KT6Y: its cycles past their retention go at the next pass
   ```

5. Erase the run's data now, whatever its days. This cannot be undone.

   ```bash
   ds purge --run <run id> --reason "erasure request E-12"
   ```

   You should see three audit rows, then the count:

   ```text
    Container deltasleuth-db-1  Running
   {"id": 42, ... "action": "retention.expire", "object": "R-35T7BPBMM7JCHXD1YFE2H9KT6Y", "detail": {"cycle": 1, "reason": "erasure: erasure request E-12", ...}, ...}
   {"id": 43, ... "action": "retention.purged", "object": "R-35T7BPBMM7JCHXD1YFE2H9KT6Y", "detail": {"keys": 40, "cycle": 1, "packs": [], "results": 40, ... "findings": 10, ... "blobs_queued": 80, "observations": 86, "chat_messages": 0, "data_key_destroyed": true}, ...}
   {"id": 44, ... "action": "retention.finished", "object": "purge", ...}
   purged 1 cycle(s) of 1 run(s): 40 key(s); 80 blob(s) queued; 0 deleted (0 B), 0 kept (shared)
   ```

6. Try to read the run's results.

   ```bash
   { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - -w '\n%{http_code}\n' "http://localhost:8000/v1/runs/<run id>/results?limit=2"
   ```

   You should see that they are gone, with the day and the reason:

   ```text
   {"detail": "cycle 1 of run R-35T7BPBMM7JCHXD1YFE2H9KT6Y was purged on 2026-10-05 (erasure): its keys, results, findings and evidence are deleted"}
   410
   ```

7. Run the clean-up now instead of waiting for it.

   ```bash
   ds purge
   ```

   You should see, when nothing is past its days:

   ```text
    Container deltasleuth-db-1  Running
   purged 0 cycle(s) of 0 run(s): 0 key(s); 0 blob(s) queued; 0 deleted (0 B), 0 kept (shared)
   ```

**To change the days.** Open `deploy/config/deltasleuth.yaml` in a text editor, change the number
after `standard_days:` (here to 30), then:

1. Check the file before it takes effect.

   ```bash
   docker compose run --rm app ds licence
   ```

   You should see four lines that start `installation`, `licence none`, `state open` and `allows`
   (section 9), or one line that starts `error:` and names the mistake.

2. Restart the app so that it reads the file.

   ```bash
   docker compose restart app
   ```

   You should see:

   ```text
    Container deltasleuth-app-1  Restarting
    Container deltasleuth-app-1  Started
   ```

After that the command of step 1 above answered `"days": 30` and `"expires_at":
"2026-11-04T20:07:45.847728+00:00"` for the run of section 5, which was made under 90 days. The number
counts for every run, old and new: a smaller number shortens what is already stored. Before the
restart the same request still answered `"days": 90`. Here the file was then put back to 90 and the
app restarted again, after which the run's date was 2027-01-03 again.

### How to read the result

- `expires_at` is the cycle's end plus the days of its class. On or after that day the clean-up
  deletes the cycle's data.
- `hold` names the reason, who placed it and when. A held run loses nothing until it is released.
- `purged` lists the cycles whose data is gone. The run itself stays in `ds runs`, with its counts.
- In the count of step 5, `80 blob(s) queued; 0 deleted` says that 80 stored answers lost a run that
  needed them. A clean-up deletes them 24 hours later, unless another run still needs them.
- `"data_key_destroyed": true` says the key that locked this run's raw keys is gone too (section 8).

Not verified here: the clean-up that runs by itself. [RETENTION.md](../mvp/RETENTION.md) says the
app's worker (section 10) runs one every hour, and starts doing so only seven days after the
installation's database was first set up, to leave time to place holds; `ds purge` does not wait. Not
verified here either: a cycle that expires by its date, since the shortest retention is seven days.

### Limits

- A hold or an erasure is for a whole run, or with `--cycle <number>` for one ended cycle of it.
  There is none for one key.
- Not deleted by any of this: files you put into, or wrote to, the folder
  `/var/lib/deltasleuth/p0`; reports you downloaded; logs; backups, which keep the data until they
  are thrown away themselves (section 11).
- `ds runs` lists an erased run like any other, as `completed`. Only the run's page, its API answer
  and the audit log say that its data is gone.
- `ds purge --dry-run` does not list a hold on a run that is not yet past its days: in step 3 it
  printed only `nothing to purge` while the run was held. To find the holds, read `ds audit --action
  retention.hold` and `ds audit --action retention.release`.
- Not verified here: [RETENTION.md](../mvp/RETENTION.md) says a run that stays paused or waiting
  longer than its days is cancelled by the clean-up, so that its data expires too.

### If something goes wrong

**`error: run R-35T7BPBMM7JCHXD1YFE2H9KT6Y is held`**
An erasure of a held run is refused. Release the hold first, if the erasure is really wanted.

**`error: run R-35T7BPBMM7JCHXD1YFE2H9KT6Y is already held`**, or **`error: run
R-35T7BPBMM7JCHXD1YFE2H9KT6Y is not held`**
A second hold, or a release of a run without one.

**`error: the reason holds a key-shaped value: write it without customer keys`**
The reason held a word that looks like one of the spec's keys, here `customer C00012019 asked`. The
reason is kept for good, so no key may be in it.

**`error: --run needs --reason (an erasure is audited)`**
Add `--reason` and a short text.

**`error: cycle 2 of run R-35T7BPBMM7JCHXD1YFE2H9KT6Y has not ended`**, or **`error: cycle 1 of run
R-35T7BPBMM7JCHXD1YFE2H9KT6Y was purged on 2026-10-05`**
`--cycle` named a cycle the run does not have yet, or one that is erased already. Erasing a whole run
a second time is not refused: it answers `purged 0 cycle(s) of 0 run(s): ...`.

**`error: run R-00000000000000000000000000 not found`**
The run id is mistyped.

**`error: put the options after 'hold'`**
`--reason` stood before the word `hold`. Write `ds purge hold <run id> --reason "..."`.

**`error: invalid settings /etc/deltasleuth/deltasleuth.yaml: retention.standard_days: Input should
be greater than or equal to 7`**, or **`error: invalid settings /etc/deltasleuth/deltasleuth.yaml:
retention: Value error, audit_days must be at least standard_days`**
A number in the `retention` lines is too small (here 5), or `standard_days` is above `audit_days`
(here 400). Correct the file before you restart the app.

**`error: cycle 1 of run R-35T7BPBMM7JCHXD1YFE2H9KT6Y was purged on 2026-10-05 (erasure): its keys,
results, findings and evidence are deleted`** from `ds report`
The run's data is gone. Nothing brings it back but a restore of a backup made before (section 11).

## 8. Encryption at rest and the keys

### What it is for

DeltaSleuth stores copies of what your systems answered. It encrypts every raw value it stores, so
that a copy of the database or of the stored answers is unreadable without two key files that live
outside both. Whoever looks after the installation keeps a copy of the two files in a safe place,
checks that the copy is right, and changes the key from time to time.

The two files, in `deploy/secrets/`:

- `kek`, the **keyring**. Each line is a key-encryption key, KEK for short: a key that locks the
  other keys DeltaSleuth makes, one for each run and a few of its own. The last line is the current
  one. Encrypted under those keys are the raw keys of every run, the stored answers of both sides and
  the chat's text.
- `tokeniser`, the **tokeniser key**. DeltaSleuth shows a protected value, such as a customer number,
  as a token like `tok:identifier:v1:E3UHLBWA...`. The same value gives the same token everywhere in
  one installation, and the tokeniser key is what makes it so. Another installation, with another
  tokeniser key, gives the same value another token.

Not encrypted: results, differences, counts and the audit log. They hold tokens in place of
protected values, and in clear only the values that a spec declares as not sensitive.

### Before you start

Nothing to switch on. The settings file names the two files, as installed:

```text
encryption:                                      # required with auth (docs/mvp/RETENTION.md §1): vault both
  kek_ref: file:/run/secrets/kek                 # the keyring: one KEK per line, the last is current
  tokeniser_key_ref: file:/run/secrets/tokeniser # every token derives from it; never inside a volume
```

Decide where the copy of the two files will live: a password manager, or a medium kept apart from the
machine and from its backups. The steps below use the folder `~/deltasleuth-keys` as a first stop on
the way there.

### In the web UI

Not offered: no page shows, checks or changes a key. A page shows only what a missing key does ("If
something goes wrong", below).

### On the command line

Steps 2, 3, 4 and 9 make or check the copy of the key files. The output of step 4 comes from another
installation, whose key had another id than the one in steps 1 and 8.

1. See the keyring's keys and how many keys each one locks.

   ```bash
   ds keys status
   ```

   You should see one line for each KEK, by its id:

   ```text
    Container deltasleuth-db-1  Running
   K-2004822248ff3b04: 3 data key(s) (current)
   ```

2. Make a folder for the copy, closed to every other account.

   ```bash
   mkdir -p -m 700 ~/deltasleuth-keys
   ```

   You should see no output.

3. Copy the two files. Nothing is shown.

   ```bash
   (umask 077; cp deploy/secrets/kek deploy/secrets/tokeniser ~/deltasleuth-keys/)
   ```

   You should see no output.

4. Check that the copy of the keyring holds the keys the app uses. The command reads the copy and
   prints the ids of its lines, never the keys. `--no-deps` starts only this short-lived container
   and leaves the database alone. `-v ...:ro` shows the copy to the container, read-only. `-u "$(id
   -u)"` runs the command under your own user number, because the copy is readable by you alone.

   ```bash
   docker compose run --rm --no-deps -u "$(id -u)" -v ~/deltasleuth-keys/kek:/tmp/kek:ro app ds keys fingerprint /tmp/kek
   ```

   You should see the id that step 1 printed for you, marked current. Where step 1 had printed
   `K-542f87148a03511b`:

   ```text
   K-542f87148a03511b (current)
   ```

   Move the folder's two files to their safe place now.

5. Change the key, once a year or when your organisation's rules say so. First let yourself write the
   keyring.

   ```bash
   chmod u+w deploy/secrets/kek
   ```

   You should see no output.

6. Add a new key as the keyring's last line. It goes from the command into the file and is never
   shown. Mind the two `>`: one alone would wipe the old key.

   ```bash
   openssl rand -hex 32 >> deploy/secrets/kek
   ```

   You should see no output.

7. Close the file again.

   ```bash
   chmod 444 deploy/secrets/kek
   ```

   You should see no output.

8. Check the keyring as the app will read it.

   ```bash
   docker compose run --rm --no-deps app ds keys fingerprint /run/secrets/kek
   ```

   You should see the old id, then the new one, marked current:

   ```text
   K-2004822248ff3b04
   K-55a599a9f1dfa028 (current)
   ```

9. Copy the new keyring to your safe place before you go on, over the old copy, and check the copy
   with the command of step 4.

   ```bash
   (umask 077; cp -f deploy/secrets/kek ~/deltasleuth-keys/kek)
   ```

   You should see no output. The command of step 4 then printed the keyring's two ids, the second
   marked current, as step 8 had. That was checked with the copy made after the restart of step 10;
   the order here is the runbook's, so that the app never runs on a key of which there is no copy.

10. Restart the app. At its start it locks every key again under the new KEK.

    ```bash
    docker compose restart app
    ```

    You should see:

    ```text
     Container deltasleuth-app-1  Restarting
     Container deltasleuth-app-1  Started
    ```

11. Look at the keys again.

    ```bash
    ds keys status
    ```

    You should see nothing left under the old KEK:

    ```text
     Container deltasleuth-db-1  Running
    K-2004822248ff3b04: 0 data key(s)
    K-55a599a9f1dfa028: 4 data key(s) (current)
    ```

12. Check that every stored answer still opens. This reads each one back.

    ```bash
    docker compose run --rm app ds keys status --verify
    ```

    You should see no answer missing, corrupted or unreadable:

    ```text
     Container deltasleuth-db-1  Running
    K-2004822248ff3b04: 0 data key(s)
    K-55a599a9f1dfa028: 4 data key(s) (current)
    blobs: 80 checked: 80 readable, 0 missing, 0 corrupted, 0 unreadable (a KEK is missing)
    ```

### How to read the result

- `K-...` is a KEK's id, made from the key; the key itself is never printed.
- `data key(s)` counts the keys a KEK locks: one for each run that still has its data, and a few of
  DeltaSleuth's own. After step 10 the count under the new KEK is one higher than before, because the
  restart also made a new key for the answers stored from then on.
- `(current)` marks the last line of the keyring, the KEK that locks new keys.
- `blobs` are the stored answers. `readable` should equal `checked`. `missing` counts answers that
  are no longer there; write that number down with each backup (section 11).

The restart of step 10 left two rows in the audit log: `keys.created`, for the new key for stored
answers, and `keys.rewrap` with `{"from": "K-2004822248ff3b04", "rows": 3, "to":
"K-55a599a9f1dfa028"}`. Between step 8 and the restart, `ds keys status` still showed the three keys
under the old KEK and `0 data key(s)` under the new one.

Keep the old line in the keyring. Backups made before the change open only with it. Not verified
here: [RETENTION.md](../mvp/RETENTION.md) says the old line may be deleted, in the file and in every
copy, once every backup that needs it has been thrown away and `ds keys status` shows `0 data key(s)`
under it.

### Limits

- A lost key cannot be made again. Without the keyring line that locks a run's key, that run's raw
  keys and stored answers cannot be read, by anybody. Not verified here: a lost tokeniser key. By
  [DEPLOY.md](../mvp/DEPLOY.md) a new one gives every value a new token, so no new run can be
  compared with an older one; never replace it.
- The copy of the keys must not sit beside the database's backups. Whoever holds a backup and the
  keys can read everything. A backup of the whole machine includes `deploy/secrets`: treat such a
  backup as unencrypted.
- Only a restart of the app moves the keys to a new KEK. A `ds` command never does.
- Encryption does not replace disk encryption: the database's own working files and the folder
  `/var/lib/deltasleuth/p0` are not encrypted by DeltaSleuth ([DEPLOY.md](../mvp/DEPLOY.md), "Volumes,
  logs and P0 files").
- Whoever may use Docker on the machine can read both files.

### If something goes wrong

**`warning: the keyring lacks K-d1bf8eba3d49c395, which wraps 5 data key(s): restore the line from
the vault and restart`**, and from `ds keys status` also **`error: the keyring lacks 1 KEK(s) in
use`** and **`missing K-d1bf8eba3d49c395: 5 data key(s); restore the line from the vault and
restart`**
The keyring has lost a line that is in use. Provoked here, on an installation whose current KEK was
`K-d1bf8eba3d49c395`, by leaving only the first line in `deploy/secrets/kek` and restarting the app.
What happened then:

- The app started, and the health address still answered `ok`. The warning was in the app's log.
- Lists and results, which hold no protected value in clear, still answered, and so did a report
  download.
- Opening one key of a run answered 503 `{"detail": "evidence unreadable: the keyring lacks
  K-d1bf8eba3d49c395 (docs/mvp/RETENTION.md §6.1)"}`.
- A new run was accepted and then stopped at once: `ds runs` showed it `paused`, and its row in the
  audit log said `{"reason": "run_paused_key_missing"}`.

The fix: put the keyring back from your copy, close it, and restart the app.

```bash
chmod u+w deploy/secrets/kek
```

```bash
cp ~/deltasleuth-keys/kek deploy/secrets/kek
```

```bash
chmod 444 deploy/secrets/kek
```

```bash
docker compose restart app
```

You should see no output from the first three, and from the restart the two lines of step 10. After
that the key's view answered 200 again and `ds keys status` showed no `missing` line. The paused run
stayed paused until it was resumed: on its page with the button "resume" (written from the page's
source), or, as here, over the API with a token that holds `operator`. It then ran to `completed`.

```bash
{ printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/nightly-check; printf '"\n'; } | curl -sS -K - -X POST -w '\n%{http_code}\n' http://localhost:8000/v1/runs/<run id>/resume
```

You should see, here with that installation's run id:

```text
{"run_id": "R-35T76C7DM6RW1DFYQ5A0VCTYTN", "status": "running"}
200
```

If the line is in no copy, what it locked is lost. By [RETENTION.md](../mvp/RETENTION.md) such runs
can still be erased (section 7), because an erasure needs no key; not tried here with a key missing.

**`error: keyring /run/secrets/kek line 1: expected 64 hex`**
Not provoked here; the [appendix](A1-install-by-hand.md) met it with a keyring that did not hold a
key. Put the file back from your copy.

## 9. The Licence screen and `ds licence`

### What it is for

DeltaSleuth has code for licences, but this build needs none and limits nothing. The screen and the
command exist so that you can see that.

### Before you start

Nothing.

### In the web UI

1. Click "Licence" in the bar. You should see the heading "Licence" with the word `open` beside it,
   a line "Installation" with an id that starts `I-`, the sentence "This build needs no licence and
   limits nothing.", the line "Allows: runs yes, sso yes, signoff yes, packs yes", and in grey "Any
   number of pairs, four-eyes sign-off, evidence packs and runs under SSO work without a licence. ds
   licence install refuses every file: this build embeds no vendor key to verify one with."

### On the command line

1. Ask the same on the command line.

   ```bash
   ds licence
   ```

   You should see:

   ```text
    Container deltasleuth-db-1  Running
   installation I-371185ef64443987
   licence none: this build needs no licence
   state open
   allows: runs yes, sso yes, signoff yes, packs yes
   ```

### How to read the result

`state open` means no licence is asked for. The `installation` id names this database; it stays the
same through a backup and a restore (section 11).

### Limits

Being allowed is not being able: sign-off and evidence packs still need single sign-on (section 1).

### If something goes wrong

**`error: this build embeds no vendor key: it needs no licence and can verify none
(docs/mvp/LICENCE.md §19)`**
`ds licence install` was given a file. There is nothing to install.

## 10. Health, logs and the worker

### What it is for

Day to day you need to know three things: whether the app is up, what it has to say when something is
wrong, and whether its background work is being done. The background work is done by the **worker**,
a part of the app's own container: it compares the keys of every run, starts scheduled runs, sends
notifications and runs the hourly clean-up of section 7.

### Before you start

Nothing.

### In the web UI

Not offered: no page shows the app's health, its log or the worker. By the pages' source a stopped
worker shows only as banners about its late work on the screens "Schedules" and "Pre-checks" (not
verified here). A run that stays `queued` is the plainest sign.

### On the command line

1. Ask the health address.

   ```bash
   curl -fsS http://localhost:8000/v1/health
   ```

   You should see:

   ```text
   {"status": "ok", "ai": false, "auth": "tokens"}
   ```

2. List the containers.

   ```bash
   docker compose ps
   ```

   You should see the app and the database marked `(healthy)`:

   ```text
   NAME                  IMAGE                COMMAND                  SERVICE   CREATED         STATUS                    PORTS
   deltasleuth-app-1     deltasleuth:source   "ds serve --host 0.0…"   app       9 minutes ago   Up 26 seconds (healthy)   127.0.0.1:8000->8000/tcp
   deltasleuth-db-1      postgres:16-alpine   "docker-entrypoint.s…"   db        9 minutes ago   Up 9 minutes (healthy)    5432/tcp
   deltasleuth-mocks-1   deltasleuth:source   "sh -c 'python -m mo…"   mocks     9 minutes ago   Up 9 minutes              8000/tcp
   ```

3. Read Docker's own word on the app, and how often Docker had to start it again.

   ```bash
   docker inspect --format '{{.State.Health.Status}} {{.RestartCount}}' "$(docker compose ps -aq app)"
   ```

   You should see:

   ```text
   healthy 0
   ```

4. Read the app's newest log lines. Add `-f` to follow the log, and leave with Ctrl-C.

   ```bash
   docker compose logs --tail 5 app
   ```

   You should see lines like these, here soon after a restart:

   ```text
   app-1  | INFO:     Application startup complete.
   app-1  | INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
   app-1  | INFO:     127.0.0.1:60324 - "GET /v1/health HTTP/1.1" 200 OK
   app-1  | INFO:     172.30.0.1:63466 - "GET /v1/health HTTP/1.1" 200 OK
   app-1  | INFO:     172.30.0.1:61508 - "GET /v1/health HTTP/1.1" 200 OK
   ```

5. Restart the app, after a change to a settings file or to a secret.

   ```bash
   docker compose restart app
   ```

   You should see:

   ```text
    Container deltasleuth-app-1  Restarting
    Container deltasleuth-app-1  Started
   ```

   Here the health address answered again two seconds later. By the worker's own help text, keys it
   had in hand are handed back and done after the restart; not tried here with a run in progress.

### How to read the result

The app's log holds three kinds of lines. Lines that start `INFO:` are requests, one for each: the
caller's address, the request and the answer's status; the request to `/v1/health` from `127.0.0.1`
every 30 seconds is the image's own health check. Lines that start `{"id":` are copies of audit rows
(section 5). Anything else is a message of the app: a line that starts `warning:` or `error:`, or a
note such as `invalid or expired API token T-7e658ed5055fb780 request_id=Q-040bfa4b39389e82`.

`(healthy)` and `"status": "ok"` say that the app answers and reaches its database. They say nothing
about the worker.

To see that the worker works, start a run and watch it end: the runs of sections 5 and 7 went from
start to `completed` within ten seconds. One worker serves a database. A `ds run` started while the
app is up leaves the work to the app's worker and says so:

```text
another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
```

### Limits

- Nothing reports a stopped worker by itself, and the health address does not cover it.
- Not verified here: running the worker as a container of its own (`ds worker` beside `ds serve
  --no-worker`), which [DEPLOY.md](../mvp/DEPLOY.md) describes.
- The app reads its settings files and secrets only when it starts. A change to
  `deploy/docker-compose.site.yml` needs `docker compose --profile mocks up -d` instead of a restart
  ([appendix](A1-install-by-hand.md), section 5.2).
- Docker keeps the app's log with its default setting; an office sets a size limit and a collector
  ([DEPLOY.md](../mvp/DEPLOY.md), "Volumes, logs and P0 files").

### If something goes wrong

**`{"detail": "the database is not answering, or the pool gave no connection in 3 s"}`** (status 503
from the health address)
The app runs and its database does not answer. Provoked here with `docker compose stop db`; `docker
compose ps -a` then showed `deltasleuth-db-1` as `Exited (0)` while the app still read `(healthy)`
for a while. The app's log repeated this line over and over:

```text
error connecting in 'pool-2': failed to resolve host 'db': [Errno -2] Name or service not known
```

Other lines about the worker stood between them and vary from one time to the next. Here they were
`worker slot: AdminShutdown`, `sweep failed: AdminShutdown` and `worker stopped (AdminShutdown):
slots restart once the lock is retaken`. Start the database:

```bash
docker compose start db
```

You should see:

```text
 Container deltasleuth-db-1  Starting
 Container deltasleuth-db-1  Started
```

One second later the health address answered `ok` again, without a restart of the app. The worker
came back by itself as well: a run started after that went to `completed` within ten seconds.

**The app never turns `(healthy)`**
It stops as soon as it starts. The reason is the last line that starts `error:` in `docker compose
logs --tail 20 app`; the [appendix](A1-install-by-hand.md), "If something goes wrong", lists those
lines.

## 11. Backup and restore

### What it is for

A backup lets you get the installation back after the machine is lost, or go back after a mistake. It
has three parts that belong together and one that is kept apart: a dump of the database, a copy of the
stored answers, a copy of your settings, and, somewhere else, the two key files of section 8. Without
the keys a backup shows no raw value, which is why the keys never go into it.

### Before you start

- The copy of the keys of section 8 is in its safe place and was checked. A backup is worth nothing
  without it.
- A place for the backups outside the installation's folder. The steps use `~/deltasleuth-backups`.
- For a restore on a machine that has no installation: git, Docker and OpenSSL (chapter 1, section
  1).

### In the web UI

Not offered: taking, checking and restoring a backup are commands on the machine.

### On the command line

**Take a backup.** The app keeps running.

1. Read every stored answer back and write down the number before `missing`. It belongs with this
   backup.

   ```bash
   docker compose run --rm app ds keys status --verify
   ```

   You should see, here with three runs made so far; after sections 5 and 7 alone the count of data
   keys is 4:

   ```text
    Container deltasleuth-db-1  Running
   K-2004822248ff3b04: 0 data key(s)
   K-55a599a9f1dfa028: 5 data key(s) (current)
   blobs: 80 checked: 80 readable, 0 missing, 0 corrupted, 0 unreadable (a KEK is missing)
   ```

2. Make the folder, closed to every other account.

   ```bash
   mkdir -p -m 700 ~/deltasleuth-backups
   ```

   You should see no output.

3. Dump the database into a file named after the day. Always take the dump first.

   ```bash
   (umask 077; docker compose exec -T db pg_dump -U deltasleuth deltasleuth > ~/deltasleuth-backups/ds-$(date +%F).sql)
   ```

   You should see no output.

4. Copy the stored answers, within 24 hours of the dump. `--exclude=./spill` leaves out a folder of
   temporary files that comparisons of files may write there, unencrypted.

   ```bash
   (umask 077; docker compose run --rm --no-deps -T app tar -C /var/lib/deltasleuth/evidence --exclude=./spill -cf - . > ~/deltasleuth-backups/evidence-$(date +%F).tar)
   ```

   You should see no output.

5. Copy your settings: the three settings files, the site file and `.env`. They hold no secret.

   ```bash
   (umask 077; mkdir -p ~/deltasleuth-backups/settings-$(date +%F) && cp -R deploy/config ~/deltasleuth-backups/settings-$(date +%F)/config && cp deploy/docker-compose.site.yml .env ~/deltasleuth-backups/settings-$(date +%F)/)
   ```

   You should see no output.

6. Look at what you have.

   ```bash
   ls -l ~/deltasleuth-backups
   ```

   You should see the two files and the folder; sizes are those seen here, with three small runs:

   ```text
   total 984
   -rw-------@ 1 <you>  staff  336710 Oct  5 20:15 ds-2026-10-05.sql
   -rw-------@ 1 <you>  staff  163840 Oct  5 20:15 evidence-2026-10-05.tar
   drwx------@ 5 <you>  staff     160 Oct  5 20:15 settings-2026-10-05
   ```

**Restore into an empty installation.** This is the case of a new machine, or of a machine whose
Docker data is gone. `<date>` is the day in the backup's file names, for example `2026-10-05`.

You can rehearse it on the trial machine by removing the installation's containers and data first.
That deletes the database and the stored answers for good, so do it only when the backup above is
complete and the key files are where they belong. Check which installation the folder acts on:

```bash
docker compose config | grep '^name:'
```

You should see:

```text
name: deltasleuth
```

Then remove its containers and its data:

```bash
docker compose down -v
```

You should see the three containers, the four volumes and the network `Removed`, ending with:

```text
 Volume deltasleuth_pgdata  Removed
 Network deltasleuth_default  Removed
```

After that there are two ways. Stay in the folder, which still holds your settings and the four
secrets, and leave out steps 1 and 6 to 11. Or leave the folder, clone the source into a folder of
another name and do every step, with the key files from their safe place. Both ways were tried here;
the outputs printed below are those of the first way.

1. Clone the source and go into the folder (chapter 1, section 2).

2. Put the three settings files back.

   ```bash
   cp ~/deltasleuth-backups/settings-<date>/config/deltasleuth.yaml ~/deltasleuth-backups/settings-<date>/config/targets.yaml ~/deltasleuth-backups/settings-<date>/config/llm_profiles.yaml deploy/config/
   ```

   You should see no output.

3. Put the site file back.

   ```bash
   cp ~/deltasleuth-backups/settings-<date>/docker-compose.site.yml deploy/
   ```

   You should see no output.

4. Put `.env` back.

   ```bash
   cp ~/deltasleuth-backups/settings-<date>/.env .
   ```

   You should see no output.

5. Let the containers read the five files; the backup kept them readable by you alone.

   ```bash
   chmod 644 deploy/config/deltasleuth.yaml deploy/config/targets.yaml deploy/config/llm_profiles.yaml deploy/docker-compose.site.yml .env
   ```

   You should see no output.

6. Make the folder for the secrets.

   ```bash
   mkdir -p deploy/secrets
   ```

   You should see no output.

7. Close it to every other account.

   ```bash
   chmod 700 deploy/secrets
   ```

   You should see no output.

8. Make a new database password. The new, empty database takes it.

   ```bash
   (umask 077; openssl rand -hex 24 > deploy/secrets/db_password)
   ```

   You should see no output.

9. Make the database's address from it.

   ```bash
   (umask 077; { printf 'postgresql://deltasleuth:'; tr -d '\n' < deploy/secrets/db_password; printf '@db:5432/deltasleuth'; } > deploy/secrets/db_dsn)
   ```

   You should see no output.

10. Put the two key files back from their safe place, here by way of `~/deltasleuth-keys`. These two
    must be the old ones.

    ```bash
    cp ~/deltasleuth-keys/kek ~/deltasleuth-keys/tokeniser deploy/secrets/
    ```

    You should see no output.

11. Let the containers read the four secrets.

    ```bash
    chmod 444 deploy/secrets/db_password deploy/secrets/db_dsn deploy/secrets/kek deploy/secrets/tokeniser
    ```

    You should see no output.

12. Check which installation this folder acts on.

    ```bash
    docker compose config | grep '^name:'
    ```

    You should see:

    ```text
    name: deltasleuth
    ```

13. Build the image.

    ```bash
    docker compose build
    ```

    You should see many build lines, ending with:

    ```text
     deltasleuth:source  Built
    ```

14. Start the database alone and wait until it is ready. The app must stay down until step 21.

    ```bash
    docker compose up -d --wait db
    ```

    You should see:

    ```text
     Network deltasleuth_default  Created
     Volume deltasleuth_pgdata  Created
     Container deltasleuth-db-1  Started
     Container deltasleuth-db-1  Healthy
    ```

15. Load the dump. It is loaded whole or not at all.

    ```bash
    docker compose exec -T db psql -U deltasleuth -d deltasleuth -q -v ON_ERROR_STOP=1 -1 < ~/deltasleuth-backups/ds-<date>.sql
    ```

    You should see a few lines like these and no line that starts `ERROR`:

    ```text
     set_config 
    ------------
     
    (1 row)

     setval 
    --------
         56
    (1 row)

     setval 
    --------
          2
    (1 row)
    ```

16. Put the stored answers back.

    ```bash
    docker compose run --rm --no-deps -T app tar -C /var/lib/deltasleuth/evidence -xf - < ~/deltasleuth-backups/evidence-<date>.tar
    ```

    You should see Docker create the other three volumes, in any order, and nothing else:

    ```text
     Volume deltasleuth_p0  Created
     Volume deltasleuth_evidence  Created
     Volume deltasleuth_workspace  Created
    ```

17. Check that the keys fit and every stored answer opens. `missing` must not be higher than the
    number you wrote down with the backup.

    ```bash
    docker compose run --rm app ds keys status --verify
    ```

    You should see the lines of the backup's step 1:

    ```text
     Container deltasleuth-db-1  Running
    K-2004822248ff3b04: 0 data key(s)
    K-55a599a9f1dfa028: 5 data key(s) (current)
    blobs: 80 checked: 80 readable, 0 missing, 0 corrupted, 0 unreadable (a KEK is missing)
    ```

18. Delete what has passed its days since the backup was taken. Define the alias `ds` first (chapter
    1, section 5).

    ```bash
    ds purge
    ```

    You should see, when nothing has:

    ```text
     Container deltasleuth-db-1  Running
    purged 0 cycle(s) of 0 run(s): 0 key(s); 0 blob(s) queued; 0 deleted (0 B), 0 kept (shared)
    ```

19. Erase again every run that was erased after the backup was taken: the restore has brought its
    data back. `<reason>` is the reason given the first time. The restored audit log no longer holds
    those erasures, so the list comes from the copy of the log outside the machine, or from your own
    notes. Leave this step out when no run was erased since the backup.

    ```bash
    ds purge --run <run id> --reason "<reason>"
    ```

    You should see three audit rows and the count, as in section 7, step 5. Here the step was done
    in the restore over the installation, further down, for a run that had been erased after the
    backup with the reason `erasure request E-13`:

    ```text
    purged 1 cycle(s) of 1 run(s): 40 key(s); 80 blob(s) queued; 0 deleted (0 B), 0 kept (shared)
    ```

20. Pause every schedule before the worker starts, so that no run starts by surprise.

    ```bash
    ds schedules pause --all
    ```

    You should see, when there is none:

    ```text
     Container deltasleuth-db-1  Running
    no active schedules
    ```

21. Start the app and the mocks.

    ```bash
    docker compose --profile mocks up -d
    ```

    You should see:

    ```text
     Container deltasleuth-db-1  Running
     Container deltasleuth-mocks-1  Created
     Container deltasleuth-app-1  Created
     Container deltasleuth-mocks-1  Started
     Container deltasleuth-db-1  Healthy
     Container deltasleuth-app-1  Started
    ```

22. Wait ten seconds, then check that the runs are back.

    ```bash
    ds runs
    ```

    You should see the runs the backup held. Here there were three (second lines left out): the runs
    of sections 5 and 7, and above them one that was made while section 10 tried a stopped database.
    If you made only the first two, you see those two.

    ```text
     Container deltasleuth-db-1  Running
    R-35T7C7R8J4QZSZJ0BR4VQD9ZF4  mock-customer-v1-vs-v2 1.0.0  batch  completed  cycle 1  different 4 of 40
    R-35T7BPBMM7JCHXD1YFE2H9KT6Y  mock-customer-v1-vs-v2 1.0.0  batch  completed  cycle 1  different 4 of 40
    R-35T7BF8BQNHNGGNKSHP8C4WM91  mock-customer-v1-vs-v2 1.0.0  batch  completed  cycle 1  different 4 of 40
    ```

23. Check the audit log.

    ```bash
    ds audit verify
    ```

    You should see every row of the backup and one more, the app's start:

    ```text
     Container deltasleuth-db-1  Running
    chain: 57 row(s) verified
    head: row 57 bf18dc6efcd0cb944613fed0589f8a31c09e63781fc0e924ccaacc3ea2d6b493
    ```

24. Print the masked key of one result of a run, for the next step. A masked key is the token that
    stands for a key (section 8). `<run id>` is a run of step 22 whose data was not erased: after
    sections 5 and 7 that is the run of section 5, the lower of your two lines. The token files that
    were made before the backup work again, because the database that came back knows their tokens.

    ```bash
    { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - "http://localhost:8000/v1/runs/<run id>/results?limit=1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["items"][0]["key_masked"])'
    ```

    You should see one masked key. Yours differs: the tokeniser key of each installation gives its
    own, so the one printed here opens nothing on your installation.

    ```text
    tok:identifier:v1:E3UHLBWAM7NAE6NI3TAYUNCMQ3
    ```

25. Check that the run reads again through the app. `<masked key>` is the line step 24 printed.

    ```bash
    { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - -o /dev/null -w '%{http_code}\n' "http://localhost:8000/v1/runs/<run id>/keys/<masked key>"
    ```

    You should see:

    ```text
    200
    ```

    This request opens one key with what both sides answered, which needs the database, the stored
    answers and the keys together. Here its whole answer was the same, byte for byte, before the
    backup and after each restore.

In a fresh clone the folder `.deltasleuth/tokens` does not exist, so steps 24 and 25 have no token
file. Copy the token files in from the old folder or from where you kept them, or make the folder
and a new token (section 2, "Before you start" and step 1) and write its name in place of
`first-run`.

**Restore over the installation you have.** This is the case of going back after a mistake, in the
same folder. Everything done since the backup is lost.

1. Stop the app.

   ```bash
   docker compose stop app
   ```

   You should see:

   ```text
    Container deltasleuth-app-1  Stopping
    Container deltasleuth-app-1  Stopped
   ```

2. Empty the database. This deletes every run, token and audit row in it.

   ```bash
   docker compose exec -T db psql -U deltasleuth -d deltasleuth -c 'DROP SCHEMA ds CASCADE'
   ```

   You should see a notice that names every table and type the database held, about sixty lines
   that read `drop cascades to ...`, in an order that differs from one database to the next. The
   last line is:

   ```text
   DROP SCHEMA
   ```

3. Do steps 15 to 20 of the restore above: load the dump, put the stored answers back, check the
   keys, delete what expired, erase again what was erased since, pause the schedules. Step 16 prints
   nothing here, because the volumes exist.

4. Start the app.

   ```bash
   docker compose start app
   ```

   You should see:

   ```text
    Container deltasleuth-db-1  Healthy
    Container deltasleuth-app-1  Started
   ```

5. Check the result with steps 22 to 25 of the restore above.

### How to read the result

After either restore the installation is as it was when the dump was taken. Here the restore over
the installation was done twice and the restore into the emptied installation once, each from the
same backup. Before the first of them a fourth run was started, the third run erased and the token
`ann-viewer` revoked, all after the backup.

- The installation id of `ds licence` is the same as before, here `I-371185ef64443987`.
- Runs made after the backup are gone. Here the fourth run was no longer listed, and the app
  answered its id with 404 `run R-35T7CB1KQGYH5B70F30NPS9QG9 not found`.
- A run that was erased before the backup is still erased: the results of the run of section 7
  answered 410 as in that section.
- **Tokens revoked after the backup are active again.** Here `ann-viewer` was listed `active` after
  the restore and the app answered it with 200 again. Run `ds token list`, revoke again what was
  revoked (section 2), and run `ds session revoke --all`. Not verified here: that a session ended
  after the backup is live again in the same way.
- **An erasure made after the backup is undone as well.** Here the results of the third run, erased
  after the backup, answered 200 again after the restore, and 410 once step 19 had erased the run
  again. The restored audit log held no row of that erasure.
- A token created after the backup is unknown to the restored database: here the app answered the
  file of such a token with 401.

### Limits

- Not in the backup: the folder `/var/lib/deltasleuth/p0` (files you copied in or wrote there), the
  token files in `.deltasleuth/tokens`, and the secrets. After the restore into an empty installation
  the folder `p0` was empty.
- A backup keeps data that the clean-up has deleted since. Throw backups away on a schedule;
  [DEPLOY.md](../mvp/DEPLOY.md) names 35 days as the longest to keep one.
- A backup made before a key change needs the keyring line of that time (section 8).
- Not verified here: a restore on another machine or on Linux, and a restore of a larger
  installation. Both restores into an empty installation were made on the same machine: one into a
  fresh clone and a new Docker project of the same name, the other in the old folder after its
  containers and data were removed.

### If something goes wrong

**`ERROR:  schema "ds" already exists`**
The dump was loaded into a database that already holds an installation, here with the app running.
Nothing was changed: the load is all or nothing, and `ds audit verify` showed the same rows as before.
To go back to the backup, follow "Restore over the installation you have". Never leave out `-v
ON_ERROR_STOP=1 -1`: [DEPLOY.md](../mvp/DEPLOY.md) describes what a load without them does to a
database.

**`KeyError: 'items'`** at the end of a few lines from `python3`, in step 24
The run you named has no results to list: its data was erased (the request itself answers 410, as in
section 7, step 6). Take a run whose data is still there.

**`{"detail": "key tok:identifier:v1:7AE3A4VM4TYUEZVP4YPOGJCZXA not found"}`** (status 404 in step
25)
The masked key is not one of this run on this installation. Met here with a masked key taken from
another installation. Use the line that step 24 printed for you.

**`{"detail": "invalid or expired API token"}`** (status 401 in step 24 or 25)
The token in the file was made after the backup, so the restored database does not hold it. Delete
that file and create a new token (section 2).

**`no such file or directory: .deltasleuth/tokens/<name>`** when you create a token after a restore
The folder is missing in a fresh clone. Section 2, "Before you start", makes it.

**`warning: the token in .deltasleuth/tokens/first-run (T-8880afc3c2288166) is not an active token of
this database (revoked, expired, or another database's) and will be refused: remove the file and run
this script again for a new one`** from `sh deploy/up.sh` after a restore
The file `first-run` holds a token made after the backup. Do as the line says.

**`error: the keyring lacks 1 KEK(s) in use`** from step 17
The keyring you put back is older than the backup. Met here, in section 8, with a keyring that held
only the older of two lines. Put back the copy made after the last key change (section 8, step 9).

**A `missing` number in step 17 higher than the one written down**
Not met here. [DEPLOY.md](../mvp/DEPLOY.md) says the copy of the stored answers is then incomplete:
put it back again (step 16) from a good copy before you start the app.

## 12. Updating to newer source

### What it is for

An update replaces the software and keeps your settings, secrets and data. When the newer software
needs new tables in the database, the app makes them as it starts. Older software does not undo such
a change, so a backup comes first: it is the only way back.

### Before you start

Take a backup (section 11). It is the only way back.

### In the web UI

Not offered: no page starts an update or shows the version in use.

### On the command line

1. Fetch the newer source.

   ```bash
   git pull
   ```

   You should see, when there is none:

   ```text
   Already up to date.
   ```

   When there is newer source, it prints a line that starts `Updating`, the word `Fast-forward` and
   the list of the files that changed.

2. Run the installation script again. It builds the source, and starts the app and the mocks again
   when the build changed.

   ```bash
   sh deploy/up.sh
   ```

   You should see `kept` for each of your files, then the closing block of chapter 1:

   ```text
   == 1/6 Settings, site file and .env
   kept     deploy/config/deltasleuth.yaml
   ...
   == 3/6 Build the image deltasleuth:source
   ...
    deltasleuth:source  Built
   == 4/6 Workspace and database
   ...
   schema up to date
   == 5/6 Start
   ...
   == 6/6 First token
   kept     .deltasleuth/tokens/first-run
   ...
   DeltaSleuth is running: Compose project "deltasleuth", an unsigned build of this checkout, for a trial.
   ```

   When the source changed, step 5 also prints ` Container deltasleuth-app-1  Recreated` and the same
   for the mocks. `schema up to date` says the database needed no new tables; otherwise the line
   reads `migrations applied:` with their numbers.

3. Check the audit log, and that a run still reads (section 11, steps 24 and 25).

   ```bash
   ds audit verify
   ```

   You should see the rows verified and the head:

   ```text
    Container deltasleuth-db-1  Running
   chain: 63 row(s) verified
   head: row 63 6074fd73ccbef585eeb4b98ff6a0984240babf2948526de791651d0b95c3233a
   ```

4. Each build that replaced another leaves the old one on the machine, without a name. Remove those
   of this installation.

   ```bash
   docker image prune --force --filter label=com.docker.compose.project=deltasleuth
   ```

   You should see, when the update built nothing new and there is nothing to remove, only this line:

   ```text
   Total reclaimed space: 0B
   ```

   After an update that built newer source it lists the build it deleted first:

   ```text
   Deleted Images:
   deleted: sha256:fc164495bd833d8193792b218d2db25837651fedb96d77d6bcce6704d849ec31

   Total reclaimed space: 0B
   ```

### How to read the result

Your files are untouched: the script creates only what is missing. The runs, the tokens and the audit
log are the ones from before.

Here the first `git pull` did bring a newer commit, one that changes documents, tests and a release
tool and nothing the app runs. It printed `Updating` with the two commits and `Fast-forward` with
twelve files. The script then built the source again and printed `Recreated` for the app and the
mocks and `schema up to date`. After it `ds runs` listed the same three runs, a key of a run opened
with status 200, `ds audit verify` counted 60 rows, and step 4 deleted one build. A second `git pull`
and a second run of the script, seven seconds long, gave the outputs printed in the steps above.

The version the app runs is in the newest `system.start` row of the audit log, which `ds audit
--action system.start --limit 1` prints: here `"version": "0.1.0.dev0"`, the version written in the
source at both commits.

Not verified here: an update that needs new tables.

### Limits

- There is no way back but the backup: older software does not undo new tables. Not verified here:
  going back, which by [DEPLOY.md](../mvp/DEPLOY.md) means the older source and a restore as in
  section 11.
- New optional settings appear only in the example files, never in your copies.
- The [appendix](A1-install-by-hand.md), section 6, does the same by hand.

### If something goes wrong

**`error: cannot pull with rebase: You have unstaged changes.`** from `git pull`
Not met here; the [appendix](A1-install-by-hand.md) met it after a source file was changed by hand.
Your settings, secrets and tokens never cause it.

**`error: the app did not become healthy; ...`** from the script
The app stopped at its start. The line above it is the reason; the [appendix](A1-install-by-hand.md),
"If something goes wrong", lists those lines. Not met here.

## 13. From this trial to an office deployment

### What it is for

The installation of chapter 1 is a trial on one machine: reachable from that machine only, over plain
`http`, with tokens that anybody holding the file can use. An office deployment lets colleagues in
from their own machines, each under their own name, over an encrypted connection.
[DEPLOY.md](../mvp/DEPLOY.md) is the document for it. This section says what changes and what must
not survive the trial.

The table uses the two terms of section 1: an identity provider is the organisation's own sign-in
service, and a TLS proxy is a web server in front of the app that gives it an `https` address.

| | The trial | The office deployment |
|---|---|---|
| Address | `http://localhost:8000`, this machine only | `https://<host name>`, through a TLS proxy such as nginx on the machine; the app's own port stays closed to the network |
| Sign-in | API tokens | Single sign-on at your identity provider; tokens only for scripts |
| Administrator in the browser | Nobody | People in the group you map to `admin`: the Admin screen |
| A second person's approval | Not possible | Possible, and asked for where a spec needs it |
| Software | Built from the source on the machine | A signed release, once one exists ([RELEASE.md](../mvp/RELEASE.md)) |
| Practice services | The mocks run | Stopped, and their targets removed |
| Keys | Two files on the machine | The same two files, with a copy in the office's vault |
| The second copy of the audit log | Stays in Docker's log | Collected outside the machine as it is written |

### Before you start

You need things this guide does not provide: an identity provider (Entra ID, ADFS, Okta, Ping or
Keycloak) in which you can register an application, a host name with a certificate, and a TLS proxy.
A backup of the trial (section 11), if anything of it is to be kept.

### In the web UI

Not verified here: with single sign-on the panel "Sign in" shows one button, "Sign in with single
sign-on", which leads to the identity provider and back. The bar then also shows "Approvals", and
"Admin" for administrators.

### On the command line

What must not survive the trial is every token and every session it made. A token keeps working after
single sign-on is switched on, and a new address ends no session.

Do steps 2 to 5 only on the day the installation stops being a trial. After them nobody can sign in
and the practice services are down, while single sign-on, which is to take their place, cannot be set
up from this guide alone. If you did them and want to go on trying things, "To get back into the
trial", after step 6, undoes their effect.

1. List the tokens.

   ```bash
   ds token list
   ```

   You should see every token, here four:

   ```text
   T-7e658ed5055fb780 (old-ci)  viewer  revoked  ...
   T-832786e9346c7230 (nightly-check)  operator  active  ...
   T-a447dea09648b1c6 (ann-viewer)  viewer  active  ...
   T-ecde506ce15b2702 (first-run)  spec_author  active  ...
   ```

2. Revoke each token that is `active`, one command for each.

   ```bash
   ds token revoke <token id>
   ```

   You should see, for each, an audit line and then:

   ```text
   revoked token T-ecde506ce15b2702 (first-run)
   ```

3. End every session.

   ```bash
   ds session revoke --all
   ```

   You should see how many were live, here none:

   ```text
   revoked 0 session(s)
   ```

4. Delete the token files.

   ```bash
   rm -r .deltasleuth/tokens
   ```

   You should see no output.

5. Stop the mocks.

   ```bash
   docker compose stop mocks
   ```

   You should see:

   ```text
    Container deltasleuth-mocks-1  Stopping
    Container deltasleuth-mocks-1  Stopped
   ```

   From now on start the installation without `--profile mocks`: `docker compose up -d` left the
   mocks stopped here.

6. Check the sign-in settings against the identity provider, once they are written.

   ```bash
   docker compose run --rm --no-deps app ds auth check
   ```

   On the trial, which has no such settings, you should see:

   ```text
   error: ds auth check needs the auth.oidc settings: these settings give tokens mode
   ```

**To get back into the trial** after steps 2 to 5, run the installation script once more. It makes
the folder `.deltasleuth/tokens` again, puts a new token named `first-run` into it, and starts the
mocks.

```bash
sh deploy/up.sh
```

You should see `kept` for your files, then:

```text
== 5/6 Start
...
 Container deltasleuth-mocks-1  Started
...
== 6/6 First token
...
token T-af64c75512677667 (first-run), spec_author, expires 2027-01-03T20:32:58+00:00; it is shown only now
created  .deltasleuth/tokens/first-run

DeltaSleuth is running: Compose project "deltasleuth", an unsigned build of this checkout, for a trial.
```

The app then answered the new file with status 200. The same was also done by hand here: the two
folder commands of section 2, "Before you start", a new token with the command of section 2, step 1,
and `docker compose --profile mocks up -d` for the mocks; a run of the mock pair then completed. The
tokens `ann-viewer` and `nightly-check` stay revoked: make them again (section 2) if you go back to
earlier sections.

**The settings of single sign-on. Not verified here:** what follows is taken from
[AUTH.md](../mvp/AUTH.md) and [DEPLOY.md](../mvp/DEPLOY.md), sections 3 and 5.

At the identity provider you register a web application with the redirect address `<public
address>/v1/auth/callback` and the sign-out address `<public address>/`, and you get a client id and
a client secret. In `deploy/config/deltasleuth.yaml`, under `auth:`, you then set:

- `public_url`, the `https` address people type, exactly;
- `trusted_proxies`, the address the proxy reaches the app from; the example file holds the line,
  switched off;
- the `oidc` lines, also in the example file, switched off: `issuer` (the identity provider's
  address), `client_id`, `client_secret_ref` (the name of the file that holds the client secret,
  never the secret), `subject_claim`, `username_claim` and `group_claims` (which fields of the
  sign-in name the person and their groups), and `roles`, which maps each of the six roles of section
  3 to group ids of the identity provider.

The client secret goes into a file `deploy/secrets/oidc_client_secret` and is named in the site file.
DEPLOY.md, section 3, has the lines; write the secret into the file with a command that does not show
it. Map ids that only the identity provider's administrators can assign, never display names, and map
at least one group to `rule_approver`, or nothing can be approved. `ds auth check` (step 6) then
checks the identity provider's address, the client secret, its signing keys and the clock, and prints
the two addresses to register.

After that, by DEPLOY.md, "From a one-machine trial": delete the four mock targets (`linker-a`,
`linker-b`, `customer-a`, `customer-b`) from `deploy/config/targets.yaml`; put the copy of `kek` and
`tokeniser` into the office's vault, or start on a new database with new keys and keep nothing of the
trial; install the signed release in place of the source build; start with `docker compose up -d`;
and go through DEPLOY.md's acceptance checklist.

### How to read the result

After steps 2 to 4 nothing of the trial can sign in: here the app answered the revoked `first-run`
token with 401 `{"detail": "invalid or expired API token"}`, and `ds token list` showed all four
tokens `revoked`. `ds` still works, because it does not sign in (section 4).

### Limits

- Single sign-on cannot be tried with what the source brings: it has no identity provider.
- DeltaSleuth cannot be served under a path such as `https://<host name>/deltasleuth/`; it needs the
  host name to itself.
- The script `deploy/up.sh` sets up a trial only. Run on an installation whose settings name an
  `https` address or single sign-on, it refuses ([appendix](A1-install-by-hand.md), "If something
  goes wrong").

### If something goes wrong

**`error: ds auth check needs the auth.oidc settings: these settings give tokens mode`**
The `oidc` lines of the settings file are still switched off.

**`no such file or directory: .deltasleuth/tokens/<name>`** when you create a token after step 4
Step 4 deleted the folder. Section 2, "Before you start", makes it again; so does the script under
"To get back into the trial".

**`cross-origin request refused`**, or **`unexpected Host header`**
[DEPLOY.md](../mvp/DEPLOY.md) says the app answers so when it is reached under another address than
`public_url`, for example through a proxy while `public_url` still says `http://localhost:8000`. The
first was met here on the trial, with a sign-in sent as from `http://127.0.0.1:8000` (section 6); the
second was not met.

## Reference

- [AUTH.md](../mvp/AUTH.md): the three ways to sign in, roles, tokens, sessions, every `ds` command
  with what it records, and every audit action.
- [RETENTION.md](../mvp/RETENTION.md): what is encrypted and how, the keyring, what is deleted when,
  holds and erasures.
- [LICENCE.md](../mvp/LICENCE.md): why this build needs no licence (its section 19).
- [DEPLOY.md](../mvp/DEPLOY.md): the office deployment: single sign-on, the TLS proxy, the audit copy
  outside the machine, volumes and logs, backups and restore, upgrades, "From a one-machine trial".
- [RUNBOOK.md](../mvp/RUNBOOK.md): the roles in a table, and "Retention, purge and keys" with the
  key change.
- [PACK.md](../mvp/PACK.md): how the audit log's rows are tied together and what `ds audit verify`
  reports.
- [RELEASE.md](../mvp/RELEASE.md): signed releases.
- [Chapter 1](01-install-docker.md) and its [appendix](A1-install-by-hand.md): installing, the alias
  `ds`, the first token, changing a setting, and the messages of a start that fails.
