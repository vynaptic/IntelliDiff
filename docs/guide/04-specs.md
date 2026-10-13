# 4. Writing a comparison spec

A comparison spec, or spec, is one text file that describes one pair of sources: how to ask each side
for a record, what identifies a record, which parts of the two answers to compare and how strictly,
which differences are already known, and which values must stay hidden.
[Chapter 3](03-first-comparison.md) ran a spec that came with the product. In this chapter you write
one.

The chapter has six sections. Section 1 declares the services a spec may call. Section 2 builds a spec
for the bundled customer mock pair, one part at a time, and runs it after every change. Sections 3 and
4 check a spec and store it. Sections 5 and 6 let DeltaSleuth propose a first draft of a spec, and
measure how the two services behave. A closing part says what the practice leaves in the installation
and how to take the practice target of section 1 out again.

**What to do first, and what can wait.** For a first spec, do "Before you begin" and section 2, the
long one. Sections 3, 4 and 6 work on the finished file of section 2, `my-specs/customer.yaml` as it is
after step 10, so do them after it. Sections 1 and 5 stand alone: each needs only "Before you begin".
Sections 1, 5 and 6 can wait until you need them, because section 2 uses targets that the installation
declares already. Two commands make you wait: the run of section 1, step 14, takes about 24 seconds,
and a run of the unreviewed draft of section 5, which no step asks for, takes about five minutes.

**How this chapter was checked.** Every command was run on 2026-10-05, on macOS
(Apple silicon) with Docker Desktop, from top to bottom in an installation that chapter 1's script had
just made. The machine already ran other installations, so it was a second one (project `dsg04`, port
18320), and the outputs are printed here with the default names. The steps of the closing part, the
two paragraphs headed "A later change" in section 1 and several messages under "If something goes
wrong" were run after that pass, in the same installation. Run ids, spec ids, times and the values
that start with `tok:` will differ on your machine; counts, outcomes, messages and content hashes will
not. A step that could not be run is marked "Not verified here:". Not verified here: every step under
"In the web UI". Those steps were written from the pages' source (`web/src/Specs.tsx`,
`web/src/Runs.tsx`). Linux was not tried.

Section 5's steps 1 and 8 are printed as they run since 2026-10-06, when the app's container began to
see the `mocks` folder and the mock's module to write the form's file. Both were run again then, in an
installation built from that source, writing under `/tmp`. Not verified here: step 9 with the new file.

The steps of the "Targets" screen (section 1) and of the page "New comparison" (section 2) date from
2026-10-08. They were clicked in a browser on an installation that ran from a source checkout without
sign-in ([chapter 2](02-run-from-source.md)), against the customer mocks on `127.0.0.1`. That
installation is no trial, so "Send" showed stand-ins there. The answer as it came was seen in a browser
on an installation with token sign-in and two services, and for the customer mocks in the
answers of the API. On such an installation the credential of a pasted command was also handed to a
target that had a base path ("Change" and its id, the address, boxes and budget as they were), and the
refusal of DeltaSleuth's own cookie was read on the "Targets" screen. Not verified here: the same steps
in chapter 1's Docker installation, where the mock's address is `http://mocks:18091`, and a command
sent to `localhost` handed to a target at `host.docker.internal`.

Two things are left out of every output below. Each `ds` command first prints one or two lines of
Docker's own that name the container `deltasleuth-db-1`. In a terminal they are `[+] Creating 1/1` and
` ✔ Container deltasleuth-db-1  Running`; elsewhere it is the one line
` Container deltasleuth-db-1  Running`. Most `ds` commands then print lines that start with `{"id":`,
copies of DeltaSleuth's audit records. Both are normal.

Docker's own lines are printed here in their plain form, as chapter 1 prints them. In a terminal Docker
draws the same words as a short table that it redraws in place: a count such as `[+] Running 3/3`, then
one line for each container, with a tick once it is done and the seconds it took.

## Before you begin

You need the installation of [chapter 1](01-install-docker.md), running with the mocks, and a terminal
in its folder, `IntelliDiff`.

In a second installation on the same machine (chapter 1, "A second installation on the same machine"),
type your own port in place of 8000 wherever a command says `localhost:8000`: the `curl` commands of
sections 1, 3 and 4 do. Nothing else changes there, because the folder's `.env` names the project for
every `docker compose` command, and so for `ds`.

1. Define the short form `ds` in this terminal, as in chapter 1, section 5.
   ```bash
   alias ds='docker compose run --rm -e DS_OPERATOR="$(id -un)" app ds'
   ```
   You should see no output.
2. Make a working folder for the files of this chapter.
   ```bash
   mkdir -p my-specs
   ```
   You should see no output. Git lists the folder as untracked (`?? my-specs/`) and never changes it.
3. Copy the key list of the customer mock pair into it: 40 customer numbers, one on each line.
   ```bash
   cp mocks/customer/keys.txt my-specs/customer-keys.txt
   ```
   You should see no output.

`ds` runs inside a container, and the container sees of your folder only `mocks` and `examples`,
read-only, not `my-specs`. It has one folder for handing files in and out, `/var/lib/deltasleuth/p0`.
This command copies your working folder there:

```bash
docker compose cp my-specs app:/var/lib/deltasleuth/p0/
```

You should see:

```text
 deltasleuth-app-1 copy my-specs to deltasleuth-app-1:/var/lib/deltasleuth/p0/ Copied
```

**Run that command again after every change to a file in `my-specs`.** `ds` reads the copy, not your
file: a change you did not copy in is a change `ds` does not see, and it says nothing about it.

## 1. Declare the two targets

### What it is for

A target is one thing DeltaSleuth is allowed to read: here, a REST service. A target has an id, an
address, a credential if the service wants one, and a limit on how fast DeltaSleuth may call it. A spec
names its two sides by target id only, never by address, so the declared targets are also the complete
list of what this installation may call.

A service is declared in one of two places. The first is the browser, on the "Targets" screen, with no
file and no restart: "In the web UI" below, and the place to start for two services of your own. The
second is the targets file, which the people who run the installation keep, not the people who write
specs: "On the command line". A database or a folder of files is declared in the targets file only.

### Before you start

Nothing has to be switched on. Know three facts:

- The targets file is `deploy/config/targets.yaml` in the installation's folder. Chapter 1's script
  copied it from `deploy/config/targets.example.yaml`, and git ignores it.
- The app container sees it, read-only, as `/etc/deltasleuth/targets.yaml`.
- The app reads it only when it starts. A change of the targets file alone takes effect after a restart
  of the app ("A later change of the targets file alone", below). The example of this section also
  changes what Docker shows to the app, so it makes the app's container anew, which is a start as well.

The example of this section adds a target that sends a token. For that you need OpenSSL, which chapter
1 already asked for.

### In the web UI

The "Targets" screen lists every target, for every role. Whoever may declare one also gets a form on
it. In an installation of chapter 1 that is whoever signed in with the token `first-run`, whose role is
`spec_author`. Under single sign-on it is an admin who signed in within the last 15 minutes. With token
sign-in behind https nobody may, and the targets file is the only way.

The steps declare one more target, `profile-web`: the customer mock again, under another id.

1. Click "Targets" in the bar at the top. The page is headed "Targets". Its table starts with the
   columns "Id", "Declared in" and "Kind", and each target of the targets file reads `targets file` in
   the second. Under the table is the panel "Declare an endpoint".
2. Type `profile-web` in "Id" and `http://mocks:18091` in "Base URL". Tick "Private or local network":
   inside Docker the name `mocks` stands for a private address. Leave "Authentication" at "None".
3. Press "Save endpoint". You should see "Saved endpoint profile-web." under the panel, and a new row
   in the table: `profile-web`, `product`, `service`, then the address, `None`, `yes`, `no`, `no`, `20`
   and `4`, and the buttons "Change" and "Remove". The app can call the target at once, without a
   restart.

Section 2 sends a request to a target from the browser ("Send"), which is the way to test one.

For a service of your own, the fields are the lines of a targets file ("How to read the result"):

| Field | What to enter |
|---|---|
| "Id" | Lower-case letters and digits, in words joined by `-`, such as `orders-legacy`. Specs name this id. |
| "Base URL" | The address every request starts with, without a query, such as `https://orders.example.com/api`. For an app on this machine, write the host as `host.docker.internal` and keep the port: inside Docker `localhost` is DeltaSleuth's own container, and the page says so if you type it. |
| "Private or local network" | Tick it for a private address, a name that stands for one, or an app on this machine. |
| "Credential over plain http" | Tick it when the service wants a credential and its address starts with `http://`. |
| "A 404 means no such record" | Tick it when the service answers 404 for a record it does not have. A side of a spec may then say `notFound: {status: [404]}`, and a key one side lacks reads `NOT_FOUND_A` or `NOT_FOUND_B`. Without both, a 404 proves nothing and the key reads `INCONCLUSIVE`. |
| "Authentication" | "None"; "Bearer token", sent as `Authorization: Bearer <value>`; or "Header", with the header's name in "Header name", such as `Cookie` or `X-Api-Key`. |
| "Credential" | The token, or the header's value: for `Cookie`, everything after `Cookie: `, without DeltaSleuth's own cookie (`ds_session_8000` in an installation of chapter 1), which a browser sends to every app on the same host. The box shows dots. The value is written once to a file on the server and shown nowhere again: if the service sends it back in an answer, "Send" shows `[the target's credential]` in its place. |
| "Add another credential header" | For a service that wants several, such as a session cookie and an anti-forgery header: up to three more, each with its name and its value. |
| "Requests a second", "At once" | The most calls in a second, and in flight at one time. Empty means 20 and 4. |

To change a target, press "Change" in its row. The panel is then headed "Change profile-web", and an
empty "Credential" box reads "stored: leave empty to keep it". A credential must be typed again when
the address or the kind of authentication changes. "Remove" asks "Remove profile-web? Stored specs and
schedules that name profile-web stop running." and then says "Removed endpoint profile-web.". A target
of the targets file has neither button: it is changed in the file.

### On the command line

1. Read the entries of the two mock pairs, the last 24 lines of the file.
   ```bash
   tail -n 24 deploy/config/targets.yaml
   ```
   You should see:
   ```text
     # The mock pairs (compose profile "mocks"), for a first run; mocks/*/spec.yaml name these ids.
     linker-a:
       base_url: http://mocks:18081
       allow_private_network: true
       auth: {kind: none}
       capabilities: &mock_absence
         authoritative_absence: {status: [404], shapes: [not_found_body]}
         absence_shapes:
           not_found_body: 'has(body.error) && body.error == "NOT_FOUND"'
     linker-b:
       base_url: http://mocks:18082
       allow_private_network: true
       auth: {kind: none}
       capabilities: *mock_absence
     customer-a:
       base_url: http://mocks:18091
       allow_private_network: true
       auth: {kind: none}
       capabilities: *mock_absence
     customer-b:
       base_url: http://mocks:18092
       allow_private_network: true
       auth: {kind: none}
       capabilities: *mock_absence
   ```
   `customer-a` and `customer-b` are the two ids this chapter's spec names. "How to read the result"
   explains each line.
2. Ask the running app which targets it has loaded. The API token goes from its file through a pipe to
   curl, so it appears neither on the screen nor in a list of running programs.
   ```bash
   { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - http://localhost:8000/v1/targets | grep -o '"target_id": "[a-z0-9-]*"'
   ```
   You should see twelve ids: the four mocks and the eight placeholders of the example file.
   ```text
   "target_id": "billing-mysql"
   "target_id": "crm-legacy"
   "target_id": "crm-links"
   "target_id": "customer-a"
   "target_id": "customer-b"
   "target_id": "ledger-oracle"
   "target_id": "linker-a"
   "target_id": "linker-b"
   "target_id": "orders-v1"
   "target_id": "orders-v2"
   "target_id": "platform-links"
   "target_id": "platform-users"
   ```

The next steps declare one more target, `profile-v1`. It is the customer mock again, under a third id,
declared the way a real service is: with a token that is read from a file, and a limit of two calls a
second. The mock does not check the token, so any value works for practice.

3. Create the token file. This command makes a random practice value and shows nothing of it.
   `umask 077` creates the file readable by you alone.
   ```bash
   (umask 077; openssl rand -hex 24 > deploy/secrets/profile_v1_token)
   ```
   You should see no output.

   For a real service, use the next command instead. It takes the token that the service's owners
   gave you from a paste. The token is never part of a command, so it reaches neither the screen nor
   the shell's history. Never type a token on a command line.
   ```bash
   (umask 077; stty -echo -icanon; head -n 1 > deploy/secrets/profile_v1_token; stty echo icanon)
   ```
   You should see no output and no prompt: the command waits. Paste the token, which stays invisible,
   and press Return. The prompt comes back. `stty -echo -icanon` tells the terminal not to show what
   arrives and not to limit its length, `head -n 1` writes the one line into the file, and
   `stty echo icanon` sets the terminal back. Checked here with stand-in values, not with a real
   token: a program typed values of 48 to 6,000 characters and Return into this command under a
   pseudo-terminal, in zsh and in bash. Each time nothing was shown, the file held exactly the value
   and a line end, and its mode was 600. Not verified here: a paste from the clipboard in a terminal
   window.
4. Check that the file holds one line. The command counts lines and shows no content.
   ```bash
   wc -l < deploy/secrets/profile_v1_token
   ```
   You should see:
   ```text
          1
   ```
5. Let the containers read the file, as chapter 1 does for its own secrets.
   ```bash
   chmod 444 deploy/secrets/profile_v1_token
   ```
   You should see no output.
6. Tell Docker Compose to show the file to the app. Open `deploy/docker-compose.site.yml` in a text
   editor. Under `app:`, find the line that starts with `    secrets: []` and write the name between
   the brackets. Keep the four spaces before `secrets`, and whatever follows on that line.
   ```text
       secrets: [profile_v1_token]
   ```
   Then add these two lines at the end of the file. The first starts in the first column.
   ```text
   secrets:
     profile_v1_token: {file: ./secrets/profile_v1_token}
   ```
7. Add the target at the end of the targets file. This is one command: paste all twelve lines.

```bash
cat >> deploy/config/targets.yaml <<'EOF'

  # A service that wants a token: the customer mock again, under a third id.
  profile-v1:
    base_url: http://mocks:18091
    allow_private_network: true                # "mocks" resolves to a private address
    allow_insecure_http: true                  # a token over plain http: for a practice service only
    auth: {kind: bearer, secret_ref: file:/run/secrets/profile_v1_token}
    rate_per_s: 2                              # at most two calls a second
    max_concurrency: 1                         # one call at a time
    capabilities: *mock_absence
EOF
```

You should see no output.

8. Check the targets file before the app reads it. This command opens what a start of the app opens,
   and prints a mistake as one line that starts with `error:`.
   ```bash
   docker compose run --rm app ds licence
   ```
   You should see:
   ```text
   installation I-d394ad661e567240
   licence none: this build needs no licence
   state open
   allows: runs yes, sso yes, signoff yes, packs yes
   ```
9. Apply both changes with one command. Docker Compose makes the app's container anew. The new
   container holds the token file as `/run/secrets/profile_v1_token`, and it reads the targets file
   as it starts. `docker compose restart app` would not apply a change of the site file.
   ```bash
   docker compose --profile mocks up -d
   ```
   You should see these lines, among lines that Docker prints as each step begins (`Recreate`,
   `Waiting`, `Starting`):
   ```text
    Container deltasleuth-db-1  Running
    Container deltasleuth-mocks-1  Running
    Container deltasleuth-app-1  Recreated
    Container deltasleuth-db-1  Healthy
    Container deltasleuth-app-1  Started
   ```
   In a terminal the table ends with `Running` for the mocks, `Healthy` for the database and `Started`
   for the app; `Recreated` stands on the app's line for a moment before that.
10. Wait five seconds, then check that the app answers again. Here it answered after two seconds.
    ```bash
    curl -fsS http://localhost:8000/v1/health
    ```
    You should see:
    ```text
    {"status": "ok", "ai": false, "auth": "tokens"}
    ```
11. Ask the app for its targets again, with the command of step 2. You should see thirteen ids now.
    The last one is new:
    ```text
    "target_id": "profile-v1"
    ```
12. Use the new target. This command copies the bundled spec of the customer pair, gives the copy a
    name of its own, and points its side a at `profile-v1`.
    ```bash
    sed -e 's/name: mock-customer-v1-vs-v2/name: guide-profile-v1/' -e 's/target: customer-a/target: profile-v1/' mocks/customer/spec.yaml > my-specs/profile-v1.yaml
    ```
    You should see no output.
13. Copy the working folder in.
    ```bash
    docker compose cp my-specs app:/var/lib/deltasleuth/p0/
    ```
    You should see the `Copied` line of "Before you begin".
14. Run the 40 keys of that spec.
    ```bash
    ds run /var/lib/deltasleuth/p0/my-specs/profile-v1.yaml
    ```
    You should see, after about 24 seconds:
    ```text
    run R-35T7DWMMF1M5ZMNS9VTTZ7QFRW started
    warning: spec 1 (guide-profile-v1 1.0.0) needs a four-eyes approval of clear, known_diffs; tokens mode has no approvers, so run R-35T7DWMMF1M5ZMNS9VTTZ7QFRW is labelled unapproved
    another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
    completed
      total 40, compared 40, match 36, different 4, indeterminate 0, cancelled 0, outstanding 0
      KNOWN_DIFF 2, MATCH 35, MISMATCH 2, NOT_FOUND_B 1
    ```
15. For comparison, take the bundled spec unchanged, with side a on `customer-a`.
    ```bash
    cp mocks/customer/spec.yaml my-specs/mock-customer.yaml
    ```
    You should see no output.
16. Copy the working folder in.
    ```bash
    docker compose cp my-specs app:/var/lib/deltasleuth/p0/
    ```
    You should see the `Copied` line.
17. Run it.
    ```bash
    ds run /var/lib/deltasleuth/p0/my-specs/mock-customer.yaml
    ```
    You should see, after about 6 seconds:
    ```text
    run R-35T7DYHY9FTWGHX8RNA1JF2H6T started
    warning: spec 2 (mock-customer-v1-vs-v2 1.0.0) needs a four-eyes approval of clear, known_diffs; tokens mode has no approvers, so run R-35T7DYHY9FTWGHX8RNA1JF2H6T is labelled unapproved
    another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
    completed
      total 40, compared 40, match 36, different 4, indeterminate 0, cancelled 0, outstanding 0
      KNOWN_DIFF 2, MATCH 35, MISMATCH 2, NOT_FOUND_B 1
    ```

**A later change of the targets file alone.** A change that touches only `deploy/config/targets.yaml`,
such as another limit or one more target without a secret, needs no new container. Check the file with
the command of step 8, then restart the app:

```bash
docker compose restart app
```

You should see:

```text
 Container deltasleuth-app-1  Restarting
 Container deltasleuth-app-1  Started
```

Then wait and ask for health as in step 10. Tried here with one more practice id added to the file:
the app listed thirteen ids before the restart and fourteen after it. The command of step 9 does
nothing in this case: run after a change of the targets file alone, it printed `Running` for all three
containers and made none anew.

**A later change of a token.** To replace a token, remove its file with
`rm -f deploy/secrets/profile_v1_token`, create it again (steps 3 to 5), and restart the app with the
same command. Until the restart the running app cannot open the file at all, and its runs fail; "If
something goes wrong" has the message.

### How to read the result

**The lines of a service target.**

| Line | What it says |
|---|---|
| `profile-v1:` | The target's id: lower-case letters and digits, in words joined by `-`. Specs name this id. |
| `base_url` | The address every request of this target starts with. A spec adds only a path to it. |
| `auth` | How DeltaSleuth identifies itself. `{kind: none}` sends nothing. `{kind: bearer, secret_ref: file:<path>}` sends the file's one line as `Authorization: Bearer <value>`. `{kind: header, header: <name>, secret_ref: file:<path>}` sends it in the header you name. |
| `rate_per_s` | The most calls DeltaSleuth makes to this target in a second. Without the line it is 20 (read from the source). |
| `max_concurrency` | The most calls in flight at one time. Without the line it is 4 (read from the source). |
| `allow_private_network` | Permission to call an address that is not public: a private, loopback or link-local one. `mocks` is such an address inside Docker. |
| `allow_insecure_http` | Permission to send a credential over plain `http`. Leave it out for a real service and use `https`. |
| `capabilities` | What the service is known to do, seen from outside. Here: an answer with status 404 whose body is `{"error": "NOT_FOUND"}` means "no such record". Section 6 measures these. |

In the example file, `&mock_absence` gives the `capabilities` block of `linker-a` a name, and
`*mock_absence` uses the same block again. That is plain YAML, not a DeltaSleuth feature.

**How the token travels.** The token is never in the targets file, never in a spec and never in an
environment variable: the targets file names a file, and DeltaSleuth reads the file when it calls the
service. This was checked here with a small receiver in place of the service, which the
product does not ship. It answered with the SHA-256 of what it was sent: a `bearer` target sent
`Authorization: Bearer` followed by exactly the file's value, and a `header` target sent the value in
the header it named.

**The limit at work.** The run of step 14 took 24 seconds. The run of step 17, the same spec with side
a on `customer-a`, which has no `rate_per_s` line, took 6 seconds. The difference is the budget: 40
first reads and 3 second reads of `profile-v1`, at two a second. The counts are the same in both runs,
because both read the same mock.

**The warning of steps 14 and 17.** The bundled spec shows some values unmasked and declares known
differences (section 2 explains both). The warning says that under single sign-on a second person
would have to approve such a spec. This installation signs people in with tokens and has nobody who
can approve, so the run goes ahead and is labelled unapproved. Every run of such a spec prints this
line.

**The rules against calling the wrong address.** A server that fetches addresses on request can be
tricked into fetching one its operators never meant it to reach. DeltaSleuth guards against that, and
you meet the guard in these places:

- A spec cannot hold an address. It names a target id, and an id that the targets file does not hold
  is refused (`unknown_target`, section 3).
- A spec's request path cannot leave the target: it starts with one `/`, has no `?` or `#`, and a key
  is put into it only if it matches the spec's `key.pattern`.
- A target at a private address needs `allow_private_network: true`. An address written as numbers is
  refused when the file is read. A name is checked every time it is resolved.
- A credential needs `https`, or `allow_insecure_http: true`.
- A credential cannot be written anywhere but in a file: not in `base_url`, not in a spec's headers.
- By the source (`deltasleuth/connectors/http.py`), DeltaSleuth follows no redirect and uses no proxy
  setting of its environment. Not verified here by a run.

"If something goes wrong" quotes the message of each.

### Limits

- The "Targets" screen declares services only, and of what a service is known to do only "A 404 means
  no such record". A database, a folder of files and any other `capabilities` line are declared in the
  targets file. For an id declared in both places the targets file wins.
- A target of the targets file shows its id and capabilities on the screen, never its address or the
  name of its secret file. A target declared in the browser shows its address and the names of its
  credential headers to whoever may declare, and never a credential.
- A credential typed into the browser is stored in clear in a file of the installation's workspace
  volume, readable by the app alone. Backups of that volume carry it.
- A target that unfinished runs read cannot be removed or moved to another address; its credential and
  its two limits can be changed.
- `ds target check` is for database and file targets only, and says so (below). A service target is
  tested with "Send" (section 2).
- `ds spec validate --targets` reads the targets file alone, so it answers `unknown_target` for a
  target declared in the browser. Validate such a spec in the browser; `ds run` and `ds compare` know
  both lists.
- The check of step 8 does not open the secret files of targets. A target whose secret file is
  missing passes the check and fails at its first call.
- `ds compare` and `ds spec validate --targets` read the targets file when you run them. The app, which
  processes every `ds run` and everything started in the web UI, knows the file as it was at its last
  start. Between an edit and the restart the two disagree.
- Database and file targets have other members. They are the subject of the chapters on tables and on
  engines and files.
- A token file that is replaced while the app runs reaches the app only at its next restart ("A later
  change of a token").
- [DEPLOY.md](../mvp/DEPLOY.md), under "First run, without touching real endpoints", advises giving a
  changed service a new id instead of reusing one, because finished runs keep what their targets
  declared. Not verified here.

### If something goes wrong

Each message below was provoked here. Most were provoked without touching
the installation's file, and you can check a change the same way: copy the targets file into
`my-specs`, change the copy, copy the folder in, and validate any spec against the copy. For example,
this made a copy without the `allow_insecure_http` line:

```bash
sed '/allow_insecure_http/d' deploy/config/targets.yaml > my-specs/targets-try.yaml
```

You should see no output.

```bash
docker compose cp my-specs app:/var/lib/deltasleuth/p0/
```

You should see the `Copied` line of "Before you begin".

```bash
ds spec validate /var/lib/deltasleuth/p0/my-specs/mock-customer.yaml --targets /var/lib/deltasleuth/p0/my-specs/targets-try.yaml
```

You should see the first message below. `mock-customer.yaml` is the bundled spec of step 15.

**`error: /targets/profile-v1/base_url [insecure_http] credentials require https unless allow_insecure_http is true`**
The answer to the three commands above. A target with `auth` of kind `bearer` or `header` has an `http`
address. Use `https`. For a practice service only, add `allow_insecure_http: true`.

**`error: /targets/profile-v1/rate_per_sec [schema.additionalProperties] unexpected member 'rate_per_sec'`**
A member's name is misspelt, here `rate_per_sec` for `rate_per_s`. Unknown names are refused, never
ignored.

**`error: /targets/customer-a/base_url [private_network] loopback, private and link-local hosts need allow_private_network: true`**
`base_url` holds a private address written as numbers, such as `http://172.30.0.3:18091`, and the
target has no `allow_private_network: true`. Add the line if the address is the right one.

**`error: target host resolves to a loopback, private, link-local or reserved address; set allow_private_network on the target to permit it`**
The same, for an address written as a name: the file was accepted, and `ds compare` refused at the
first call. Provoked with `base_url: http://mocks:18091` and no `allow_private_network` line.

**`error: /targets/customer-a/base_url [invalid_base_url] base_url must not carry userinfo; credentials come from secret_ref`**, or **`[invalid_base_url] base_url must not contain whitespace, a query (?) or a fragment (#)`**
`base_url` holds a user name and password (`http://user:pw@...`), or something after a `?`. Write the
address alone and put the credential in a secret file.

**`error: /targets/customer-a/auth/secret_ref [schema.pattern] does not match the required pattern ^file:[^\x00]+$: Credential by reference only: file:<path>. Never an inline value.`**
`secret_ref` holds the token itself. Write the token to a file (step 3) and name the file.

**`error: /targets/customer-a/auth/secret_ref [schema.not] member 'secret_ref' is not allowed here`**
`auth` says `kind: none` and also names a secret. Remove one of the two.

**`error: /targets/Customer_A [schema.propertyNames] member name 'Customer_A' is not allowed`**
A target id has a capital letter or an underscore. Use lower-case words joined by `-`.

**`error: cannot read secret file /run/secrets/no_such_token: No such file or directory`**
The target names a secret file that the container does not hold. Either step 6 is missing, or step 9
was not run after it. The check of step 8 passed all the same; `ds compare` printed this line.

**`failed (cannot read secret file /run/secrets/profile_v1_token: No such file or directory)`**, then **`error: the run is failed`**, from `ds run`, while the file is in `deploy/secrets` and `ds compare` works
The token file was removed and created again while the app ran. Docker still shows the app the old
file, which is gone. `ds compare` starts a container of its own and sees the new one. Restart the app
("A later change of a token", above). Provoked here by replacing the practice token.

**`error: secret file /run/secrets/profile_v1_token is empty or has a BOM or surrounding whitespace`**
The token file holds a space or a blank line beside the token. Provoked here by pasting a value with a
space after it. By its words the message also stands for a file that holds nothing, which is what a
paste interrupted with Ctrl-C leaves; the terminal itself was back to normal after the interruption,
in zsh and in bash. Remove the file with `rm -f deploy/secrets/profile_v1_token` and create it again
(steps 3 to 5).

**`permission denied: deploy/secrets/profile_v1_token`** (zsh) or **`bash: deploy/secrets/profile_v1_token: Permission denied`** (bash)
A command of step 3 ran while the file exists and is read-only. The file is unchanged. To replace it,
remove it first with `rm -f deploy/secrets/profile_v1_token`. Without `-f`, `rm` asks `override
r--r--r-- <you>/staff for deploy/secrets/profile_v1_token?` (macOS); left unanswered here, it kept the
file.

**`curl: (52) Empty reply from server`** from the health command of step 10
The app has not finished starting. Wait a few seconds and ask again.

**`target 'profile-v1' is not in the registry`** (code `unknown_target`), on the Specs screen or from the API, while `ds spec validate` with `--targets` says the same spec is valid
The app has not started again since the target was added. In this section's example, run step 9.
After a change of the targets file alone, restart the app ("A later change of the targets file alone",
above).

**`failed (SpecError)`**, then **`error: the run is failed`**, from `ds run`
The same cause, met by a run: `ds run` read the new targets file, and the app that processed the run
did not know the target. Neither the terminal, nor the run's record, nor the app's log
(`run R-... failed: SpecError`) names the target. Apply the change as the entry above says, then start
the run again.

**`error: target customer-a is not a database target (dsn_ref or engine)`**
`ds target check` was given a service target. It has no check for one.

**"invalid target" on the "Targets" screen, with a table of issues**
Nothing was saved, and the form keeps what you typed. The rows are the messages of this list, with the
field's name as the targets file writes it: `allow_private_network` is "Private or local network", and
`allow_insecure_http` is "Credential over plain http". Tick the box the message names and save again.

**`localhost is DeltaSleuth's own container here, not this machine: write the host as host.docker.internal, keep the port, and set allow_private_network`**
"Base URL" names `localhost` or `127.0.0.1` in an installation that runs in Docker. Write
`http://host.docker.internal:<port>` and tick "Private or local network". Not verified here: the
installation these steps were clicked on did not run in Docker.

**`secret is refused: it holds DeltaSleuth's own cookie ds_session_8000, which signs in here and which a browser sends to every port of this host: leave that cookie out`**
The value in "Credential" holds the cookie that keeps you signed in to DeltaSleuth. A browser keeps
cookies by host, not by port, so a request to an app at another port of `localhost`, copied from the
browser, carries it beside the app's own cookie. Nothing was saved. Remove that cookie from the value
and save again; "Import curl…" (section 2) leaves it out by itself.

**`secret is required: none is stored for this auth, or the base URL or the auth changed`**
The target needs a credential and the box was empty, or the address or the kind of authentication
changed: a stored credential never moves to another address. Type the credential and save again.

**`target customer-a is declared in the targets file: change it there`**
The id belongs to a target of the targets file. Choose another id, or change the file ("On the command
line").

**`target profile-web is read by 1 unfinished run(s): stop them first`**
A run that is not finished reads the target: a running, paused or soak run. Let it end or cancel it,
then remove the target or change its address.

**"Endpoints are declared by the spec_author role, or in the targets file."** under the table
You are signed in with a lower role, so the screen shows the list and no form.

## 2. The parts of a spec, step by step

### What it is for

This section builds one spec from the smallest file that runs to one you could use, and runs it after
every change, so that you see what each part does to the result. It is for whoever has to describe a
new pair of services to DeltaSleuth. The pair is the customer mock: `customer-a` is version 1 of a
customer service, `customer-b` is version 2, and both hold the same 40 customers with a few planted
differences.

### Before you start

Do "Before you begin" at the top of this chapter. Nothing else has to be switched on. The steps use
the targets `customer-a` and `customer-b` of the example targets file; section 1 is not needed.

A spec is written in YAML, where the spaces at the start of a line carry meaning. Use spaces, never
tabs, and keep each block's indentation exactly as printed.

### In the web UI

The comparison builder holds each side's request as a form with a button that sends it, and the rest
of the spec as YAML in a box. It is the place to start for a pair of your own. You need a token of the
role `spec_author`, such as `first-run`. The steps build the smallest spec of "On the command line",
step 1, for the customer mock pair.

1. Click "Specs" in the bar at the top, then "New comparison" under the heading. The page is headed
   "New comparison".
2. Type `C00012001` in "Sample key". It is the key the page asks the services for. The page keeps it
   in memory only, and it is no part of the spec.
3. Under "Side A", choose `customer-a` in "Target" and type `/api/v1/customers/{{ key }}` in "Path".
   Press "Send". You should see a line such as `HTTP 200, 1 ms, 1 attempt, 764 bytes`. Under it are
   "The answer as it came: this is a trial installation on this machine. An office installation shows
   stand-ins only." and the customer as version 1 answers, in `"customer"` beside `"asOf"`.
4. Under "Side B", choose `customer-b`, type `/v2/customers/{{ key }}` in "Path" and press "Send". You
   should see `HTTP 200, 1 ms, 1 attempt, 985 bytes` and the same customer as version 2 answers, in
   `"data"` beside `"meta"`.
5. Leave the box "Everything else (YAML)" as it is, and press "Validate". You should see "No issues."
   beside the buttons.
6. Press "Save and compare this key". You should see "Saved as spec #N." beside the buttons, and below
   them the panel "Compared now (run R-...)" with `MISMATCH` and the two answers side by side: the
   result of step 1 on the command line, because the two answers are not in one shape yet.

"Send" asks one service once, for the sample key, and stores nothing: no spec, no run, no answer. The
audit log records who asked which target, with the sample key as reports show it: a stand-in, unless
the key's class is `business_code`, when it is the key itself. It respects the target's limits, and
it does not try a second time. You can press "Send" under one side before the other side has a path:
the side you send then stands in for the other, and a problem is listed once, under the side you sent.

**The box "Everything else (YAML)"** holds every part of the spec but the two requests: what "On the
command line" writes from step 2 on (`select`, `map`, `compare`, `knownDiffs`, `security`) goes into
it, under the same names. It starts with the lines to change for your own pair, each marked
`# change`: the name, the key's label, the `pattern` your keys fit, one or more keys, and for each side
where its answer holds a list and the most items the service returns.

**Keep the name `scratch-comparison` while you build.** Each "Save as a new version" and each "Save and
compare this key" stores the spec as it stands, unless that content is stored already. A stored version
is never deleted, and it is at once the newest of its name, which a schedule that follows the name runs
next (section 4). When the comparison is right, write the pair's real name and a version in the first
lines and save once more.

**From a request you already have.** Press "Import curl…" under a side, paste one `curl` command and
press "Import". A browser's developer tools copy a request this way ("Copy as cURL", in the form for
bash), and so do REST clients. The method, the path, the query, the headers and a JSON body fill the
side's fields. With a target that was declared in the browser chosen first, the path comes without the
part the target's address already holds. What a browser adds for its own cache (`If-None-Match`,
`If-Modified-Since` and the like) is left out, and so is DeltaSleuth's own sign-in cookie, which a
browser sends to every app on the same host. Then replace the record's id in "Path" with `{{ key }}`, as
the page says. A request is a `GET`, or a `POST` with a JSON body under "Body (JSON)"; the key can
stand in the query, a header and the body too. A key of several values is typed as its parts joined by
`|`, and a request names one part as `{{ keyParts.<name> }}` (step 11).

**A credential in the command** never enters the comparison. The page says "The command carried a
credential (Cookie). It belongs to the target, not to a spec.", and "Declare the endpoint…" opens
"Targets" with the credential filled in.

- With a target that was declared in the browser chosen for the side, at the address the command was
  sent to, the panel is headed "Change" and the target's id. Its address, its boxes and its budget are
  as they were, and only "Authentication" and the credential are filled in. A command sent to
  `localhost` counts as sent to a target at `host.docker.internal` with the same port. Tick
  "Credential over plain http" if the address starts with `http://`, and press "Save endpoint".
- Otherwise the panel is "Declare an endpoint", with the address filled in, the part the import took
  off the path included. Give it an id, tick what the address needs (the page names one missing box at
  a time) and press "Save endpoint". If you press "Change" in another target's row instead, the page
  asks before it drops the credential, unless the command was sent to that target's address.

Then press "Back to the comparison": the page has kept your work ("Your unsaved work on this page is
back."). Choose the target, if it is a new one, and press "Send".

A spec that exists as a file is pasted whole instead:

1. Click "Specs" in the bar at the top. The page is headed "Specs".
2. In the panel "Upload a spec", press the file chooser and pick your file, or paste the text into the
   large box under it. The box then shows the YAML.
3. Press "Validate". Beside the buttons you see "No issues.", or below them a table with the columns
   "Pointer", "Severity", "Code" and "Message" (section 3).
4. Press "Save". Beside the buttons you see "Saved as spec #N.", and the table at the top of the page
   gains a row for the version. If this content is stored already, N is the id it has and the table
   does not change. That is the case for every file you have given to `ds compare` or `ds run`, which
   store what they run.
5. Click "Runs" in the bar. In the panel "New run", choose your version in the list "Spec", where it
   reads like `guide-customer 0.4.0 (#8)`. Leave "Mode" at `batch`. Paste keys into the box "Keys, one
   per line (none: the spec's key source; a table pair without one scans both tables)", or leave it
   empty. Press "Start". The page of the new run opens; [chapter 3](03-first-comparison.md) reads it.

Pasted into the box "Everything else (YAML)" of "New comparison" and validated there, a whole spec also
fills the two sides' fields. Only that page compares one key of your choice with a spec, which the
steps below do all the time with `ds compare`: the "Compare now" button of a key's page exists only on
a key that is already in a run.

### On the command line

Every step ends the same way: copy the folder in, check the file, then compare one key or run all 40.
The file is always `my-specs/customer.yaml` on your machine and
`/var/lib/deltasleuth/p0/my-specs/customer.yaml` for `ds`. Step 1 prints the command that copies the
folder in and the command that validates, and step 2 the command that runs the 40 keys. Later steps
name those three without printing them again; a command that compares one key is printed whenever the
key is new.

**Step 1. The smallest spec that runs.** Create the file. This is one command: paste it whole.

```bash
cat > my-specs/customer.yaml <<'EOF'
apiVersion: deltasleuth/v1
kind: ComparisonSpec
metadata:
  name: guide-customer
  version: "0.1.0"
  owner: guide-reader
normalizerVersion: "1.1.0"
key:
  name: customer_id
  type: string
  pattern: "^C[0-9]{8}$"
  source:
    kind: list
    keys: [C00012001]
sides:
  a:
    role: legacy
    target: customer-a
    request: {method: GET, path: "/api/v1/customers/{{ key }}"}
    truncation: {countPath: "$.customer.accountCount", itemsPath: "$.customer.accounts", cap: 50}
  b:
    role: new
    target: customer-b
    request: {method: GET, path: "/v2/customers/{{ key }}"}
    truncation: {countPath: "$.data.productCount", itemsPath: "$.data.products", cap: 100}
compare: {}
recheck: {delay: 1s, times: 1}
EOF
```

You should see no output. What the lines say:

| Part | Meaning |
|---|---|
| `apiVersion`, `kind` | Fixed words that say what kind of file this is. |
| `metadata` | `name` is the pair's name in every list and report: lower-case words joined by `-`. `version` is three numbers in quotes. `owner` is the team to ask about the spec. |
| `normalizerVersion` | The version of DeltaSleuth's built-in value clean-ups (step 5) the spec was written for. This build has `1.1.0` and refuses another. |
| `key` | What identifies a record. `name` labels it in results. `type` is `string` here; the schema also has `integer`, `uuid` and `composite`, a key of several values (step 11). `pattern` is a regular expression every key must match; it is required here because the key is put into the request's path. `source` says where the keys come from: for now, a list of one. |
| `sides.a`, `sides.b` | Side a is the baseline, the old system. Side b is the candidate, the new one. `role` is a label of your choice. `target` is an id of the targets file. |
| `request` | What to send for one key: `GET` or `POST`, and a path in which `{{ key }}` stands for the key. A request may also have `query`, `headers` and, for `POST`, `body`; `examples/public/rickandmorty/spec.yaml` shows a `POST` side. |
| `truncation` | How to tell that a service cut a list short: where the answer says how many items exist (`countPath`), where the items are (`itemsPath`), and the most the service returns (`cap`). A side must say this unless its target does. A service that says itself when it cut an answer is declared with `flagPaths`, alone or beside these: with `truncation: {flagPaths: ["$..more"]}` beside `recheck: {delay: 1s, times: 1, outcomes: [MISMATCH, NOT_FOUND_A, NOT_FOUND_B]}` a key whose answer holds `more: true` anywhere is read once, is INCONCLUSIVE `truncated`, counted in `api_uncomparable`, and is not compared. Without that `outcomes` line, as in this step's spec, the default `recheck.outcomes` holds INCONCLUSIVE too: a cut key is then read again as any INCONCLUSIVE key is, and reads INCONCLUSIVE `recheck_failed` with `api_uncomparable` 0. A path that finds no flag detects nothing, so check its spelling. |
| `compare: {}` | Compare everything, exactly, lists in order. Steps 4 to 6 fill it. |
| `recheck` | A key that differs is read a second time before the difference counts: after `delay`, at most `times` times. It cannot be left out. |

Copy the folder in.

```bash
docker compose cp my-specs app:/var/lib/deltasleuth/p0/
```

You should see the `Copied` line of "Before you begin". Now check the file. The command tests its form
against the schema, the formal description of a spec that ships with the product; then rules that a
schema cannot express; then, because of `--targets`, the targets file.

```bash
ds spec validate /var/lib/deltasleuth/p0/my-specs/customer.yaml --targets /etc/deltasleuth/targets.yaml
```

You should see:

```text
ok: customer.yaml is valid (0 warning(s))
```

Compare its one key.

```bash
ds compare C00012001 --spec /var/lib/deltasleuth/p0/my-specs/customer.yaml
```

You should see:

```text
C00012001: MISMATCH (DIFFERENT, rechecked)
  MEDIUM FIELD_MISSING missing_in_b /asOf
      A: {"eventOffset": "tok:free_number:v1:TCENMV5HLR2O7IJ7CFP3POA…
      B: <absent>
  MEDIUM FIELD_MISSING missing_in_b /customer
      A: {"accountCount": "tok:free_number:v1:KWDLQPB7FHFRDITINQXAMS…
      B: <absent>
  MEDIUM FIELD_EXTRA extra_in_b /data
      A: <absent>
      B: {"addresses": [{"city": "tok:free_text:v1:YUTGMD7ANI5JWI77N…
  MEDIUM FIELD_EXTRA extra_in_b /meta
      A: <absent>
      B: {"requestId": "tok:free_text:v1:IAYIJQQLHTRGEMXP45C2EJP4RX"…
run R-35T7E0R0G9AFAPKFR1H25VW1EW; observations a O-97035f0714e4fa52 b O-b2bb521ee3235b04
```

The spec runs, and it finds that nothing matches: version 1 wraps a customer in `customer` beside
`asOf`, version 2 wraps it in `data` beside `meta`. Every value is shown as a `tok:` stand-in, because
nothing has said yet which values may be shown (step 9). The same customer in two shapes is the normal
start of a pair, and the next step deals with it.

**Step 2. Pick the part to compare and give both sides one shape.** Two members do this. `select`
picks the part of the answer that holds the record. `map` rebuilds it with the same names on both
sides; the result is called the canonical document, and every later part of the spec speaks of its
names. `select` is written in JSONPath, where `$` is the whole answer. `map` is written in JSONata, a
small language for reshaping JSON.

Open `my-specs/customer.yaml` in a text editor. Change the version line to `version: "0.2.0"`. Then
replace everything from the line `sides:` down to, but not including, the line `compare: {}` with this
block:

```text
sides:
  a:
    role: legacy
    target: customer-a
    request: {method: GET, path: "/api/v1/customers/{{ key }}"}
    truncation: {countPath: "$.customer.accountCount", itemsPath: "$.customer.accounts", cap: 50}
    select: "$.customer"
    map: |
      {
        "customerId": id,
        "givenName": firstName,
        "familyName": lastName,
        "email": email,
        "phones": [phones.{"kind": kind, "number": number}],
        "segment": segment,
        "createdAt": createdAt,
        "preferences": {"marketing": {"sms": prefs.mktSms = "Y"}},
        "accounts": [accounts.{
          "sortCode": sortCode, "accountNo": accountNo, "balance": balance, "interestRate": rate
        }],
        "consents": [consents.{"purpose": purpose, "granted": granted = "Y"}],
        "tags": [tags]
      }
  b:
    role: new
    target: customer-b
    request: {method: GET, path: "/v2/customers/{{ key }}"}
    truncation: {countPath: "$.data.productCount", itemsPath: "$.data.products", cap: 100}
    select: "$.data"
    map: |
      {
        "customerId": customerId,
        "givenName": profile.givenName,
        "familyName": profile.familyName,
        "email": contactPoints[channel = "EMAIL"][0].value,
        "phones": [contactPoints[channel = "PHONE"].{"kind": use, "number": value}],
        "segment": segment,
        "createdAt": createdAt,
        "preferences": {"marketing": {"sms": preferences.channels.sms.marketing}},
        "accounts": [products.{
          "sortCode": account.sortCode, "accountNo": account.number, "balance": balance.amount,
          "interestRate": interestRate
        }],
        "consents": [consents.{"purpose": purpose, "granted": status = "GRANTED"}],
        "tags": [labels]
      }
```

Three things in the maps do more than rename. `prefs.mktSms = "Y"` turns version 1's `"Y"` and `"N"`
into true and false, which version 2 sends already. `contactPoints[channel = "EMAIL"][0].value` takes
the e-mail address out of version 2's list of contact points. `[accounts.{...}]` builds a list with one
entry for each account. The map leaves out what you do not want compared, such as addresses here; the
bundled `mocks/customer/spec.yaml` maps the whole profile.

Copy the folder in and validate, with the two commands of step 1. Then compare the key again.

```bash
ds compare C00012001 --spec /var/lib/deltasleuth/p0/my-specs/customer.yaml
```

You should see:

```text
C00012001: MISMATCH (DIFFERENT, rechecked)
  MEDIUM ORDER_MISMATCH order_changed /consents
      A: [{"granted": false, "purpose": "tok:free_text:v1:3P6TLWW6SS…
      B: [{"granted": false, "purpose": "tok:free_text:v1:OPN2DYAQQS…
run R-35T7E13K1H78YHW0RAVRA8MFJ9; observations a O-0da2bb974269a078 b O-de0ef218e8f02558
```

One difference is left for this customer: the two services list the same consents in another order.
Now run all 40 keys. `--keys` gives the run a key list of its own, in place of the spec's one key.

```bash
ds run /var/lib/deltasleuth/p0/my-specs/customer.yaml --keys /var/lib/deltasleuth/p0/my-specs/customer-keys.txt
```

You should see:

```text
run R-35T7E1BG71XC9T5YPCRP243E2Y started
another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
completed
  total 40, compared 39, match 6, different 33, indeterminate 1, cancelled 0, outstanding 0
  INCONCLUSIVE 1, MATCH 6, MISMATCH 33
```

Six customers match, 33 differ and one could not be judged. The line that begins `another worker` is
normal: the running app does the work and `ds run` waits for it.

**Step 3. Say what "no such record" looks like.** The key that could not be judged is a customer that
version 2 does not hold. Compare it.

```bash
ds compare C00012027 --spec /var/lib/deltasleuth/p0/my-specs/customer.yaml
```

You should see:

```text
C00012027: INCONCLUSIVE (INDETERMINATE, rechecked) reason recheck_failed
run R-35T7E1RKAJTNJ12P34MKQF61NY; observations a O-f78d1a4d837ce5b8 b O-fa4cd98e086a7808
```

Version 2 answered 404, and the spec does not say what a 404 means, so DeltaSleuth will not guess. In
the file, change the version to `"0.3.0"` and add this line under each of the two lines that start
with `    truncation:`. It starts with four spaces.

```text
    notFound: {status: [404]}
```

Copy the folder in, validate, and compare the key again with the command above. You should see:

```text
C00012027: NOT_FOUND_B (DIFFERENT, rechecked)
  CRITICAL MISSING_DOCUMENT missing_in_b /
      A: <absent>
      B: <absent>
run R-35T7E23NBAWBYW7BFKAGH0J82B; observations a O-37d2cd9a5cb13702 b O-9075d23bd8ad4e75
```

The customer is now reported as missing on side b. The two `<absent>` lines belong to a finding about
the whole record and carry no value; side a does hold the customer. A side may say `notFound` only with
a status its target declares as "no such record" (section 1, `capabilities`).

**Step 4. Say how each list is matched.** Without a rule a list is compared position by position, so a
list in another order differs. `compare.arrays` gives each list a mode:

| Mode | Two lists are equal when |
|---|---|
| `ordered` | They hold the same members in the same order. This is the mode without a rule. |
| `set` | They hold the same members, in any order; a repeated member counts once. |
| `multiset` | They hold the same members the same number of times, in any order. |
| `keyed` | Members are paired by the fields named in `key`, like rows by an id, and each pair is then compared field by field. |

In the file, change the version to `"0.4.0"` and replace the line `compare: {}` with:

```text
compare:
  arrays:
    - {path: "$.phones", mode: keyed, key: [number]}
    - {path: "$.accounts", mode: keyed, key: [sortCode, accountNo]}
    - {path: "$.consents", mode: keyed, key: [purpose]}
    - {path: "$.tags", mode: set}
```

Copy the folder in, validate, and compare `C00012001` as in step 2. You should see:

```text
C00012001: MATCH (MATCH, provisional)
run R-35T7E2FA81TGQEXECXMDRVC90R; observations a O-463a647e84f563fc b O-05c5ebae410ad493
```

Run the 40 keys with the `ds run` command of step 2. You should see these last two lines:

```text
  total 40, compared 40, match 31, different 9, indeterminate 0, cancelled 0, outstanding 0
  MATCH 31, MISMATCH 8, NOT_FOUND_B 1
```

From 6 matches to 31, by four lines. The runbook warns of the opposite: a wrong key field turns one
difference into many.

**Step 5. Clean values up before comparing them.** Some of the eight mismatches are the same value
written in two ways. Compare one of them.

```bash
ds compare C00012015 --spec /var/lib/deltasleuth/p0/my-specs/customer.yaml
```

You should see:

```text
C00012015: MISMATCH (DIFFERENT, rechecked)
  MEDIUM FIELD_EXTRA extra_in_b /phones/tok:free_text:v1:V45PCEETJR6C2PSDTYWNLNIDF3
      A: <absent>
      B: {"kind": "tok:free_text:v1:Q6KFJFT5ZL5UMQOWAHNWDGHGX5", "nu…
  MEDIUM FIELD_MISSING missing_in_b /phones/tok:free_text:v1:OYTDFQPLU5GVHANAJ4UQYSNYZ5
      A: {"kind": "tok:free_text:v1:Q6KFJFT5ZL5UMQOWAHNWDGHGX5", "nu…
      B: <absent>
run R-35T7E3AXXA4T7FSXH9KE0KQYNR; observations a O-88196f99a16c11e0 b O-4d358240763ad748
```

Version 1 writes this customer's phone number as `07700 900115` and version 2 as `+447700900115`. The
list is keyed by the number, so DeltaSleuth sees one phone missing and another one extra. A normaliser
is a named clean-up applied to a field on both sides before comparing. `compare.fields` gives a field
its normalisers, in the order they run. In the file, change the version to `"0.5.0"` and add these
lines after the line `    - {path: "$.tags", mode: set}`:

```text
  fields:
    - {path: "$.phones[*].number", normalize: [{e164: {defaultRegion: GB}}]}
    - {path: "$.createdAt", normalize: [{timestamp_utc: {precision: s}}]}
    - {path: "$.tags[*]", normalize: [trim, lower]}
    - {path: "$.accounts[*].balance", normalize: [{decimal: {scale: 2}}]}
```

In a path, `[*]` means every member of a list. The four rules write every phone number in the
international form, with `GB` assumed where the country is missing; cut every creation time to whole
seconds in UTC; strip spaces around each tag and write it in lower case; and write every balance as a
number with two decimal places, whether the service sent a number or text.

The normalisers of this build are `trim`, `collapse_ws`, `nfkc`, `casefold`, `upper`, `lower`,
`email_basic`, `digits_only`, `null_if_empty` and `address_std`, written as bare names, and four that
take settings: `e164` with `defaultRegion`, `tax_id` with `country`, `decimal` with `scale` and an
optional `rounding`, and `timestamp_utc` with `precision` (`s`, `ms` or `us`) and an optional `mode`.
Only the five used above were run here.

Copy the folder in, validate, and compare `C00012015` again. You should see:

```text
C00012015: MATCH (MATCH, provisional)
run R-35T7E3PBZFEVTK216AYH7B52Z8; observations a O-b614813c69393d1c b O-732d51eba103c01f
```

Run the 40 keys. You should see these last two lines:

```text
  total 40, compared 40, match 35, different 5, indeterminate 0, cancelled 0, outstanding 0
  MATCH 35, MISMATCH 4, NOT_FOUND_B 1
```

A difference that a normaliser removed is not thrown away. It is kept as a finding of the lowest
severity, `INFO`, which `ds compare` leaves out of its short output. `--json` prints everything:

```bash
ds compare C00012015 --spec /var/lib/deltasleuth/p0/my-specs/customer.yaml --json | grep -e '"path_pattern"' -e '"severity"' -e '"taxonomy_code"' -e '"outcome"'
```

You should see:

```text
      "path_pattern": "$.phones[*].number",
      "severity": "INFO",
      "taxonomy_code": "NORMALIZATION_ONLY",
    "outcome": "MATCH",
```

**Step 6. Allow a small gap between two numbers.** A field rule can also say how values are compared.
`compare: {tolerance: {abs: <number>, rel: <number>}}` calls two numbers equal when their gap is at
most `abs`, or at most `rel` times the larger of the two; either may be left out. No customer of the
mock pair has two rates that differ, so first make a test copy of the spec in which side b's rate is
off by half a millionth. The command also gives the copy a name of its own.

```bash
sed -e 's/name: guide-customer/name: guide-tolerance-test/' -e 's/"interestRate": interestRate$/"interestRate": interestRate + 0.0000005/' my-specs/customer.yaml > my-specs/tolerance-test.yaml
```

You should see no output. Copy the folder in, then compare a key with the copy.

```bash
ds compare C00012001 --spec /var/lib/deltasleuth/p0/my-specs/tolerance-test.yaml
```

You should see:

```text
C00012001: MISMATCH (DIFFERENT, rechecked)
  MEDIUM VALUE_MISMATCH value_changed /accounts/tok:free_text:v1:UZ434UIWYHHTBNCUEYJIKBUNLG/interestRate
      A: tok:free_number:v1:WS4PEVJZRDOTXOQHYSAINRAKYB
      B: tok:free_number:v1:ULJXNBIUK4SLCG7B6PFOXQGQIC
run R-35T7E4BGRHKSC35TS7B104TRBP; observations a O-998669e85b88808f b O-5422fbb691a9739f
```

In `my-specs/customer.yaml`, change the version to `"0.6.0"` and add this line after the line that
starts with `    - {path: "$.accounts[*].balance"`:

```text
    - {path: "$.accounts[*].interestRate", compare: {tolerance: {abs: 0.000001}}}
```

Make the test copy again with the `sed` command above, copy the folder in, validate `customer.yaml`,
and compare the key with the copy again. You should see:

```text
C00012001: MATCH (MATCH, provisional)
run R-35T7E4QGF44BVF7TV0S6S0MJX9; observations a O-051d9827e839c94c b O-3b1c120c1a13b0b9
```

The gap is inside the tolerance. Like a normalised difference it is kept as an `INFO` finding
(`NORMALIZATION_ONLY`, seen here with `--json`). A tolerance does not turn text into a number: tried
on the balance of `C00012019`, which one side sends as the text `"1250.10"`, a tolerance alone still
gave `TYPE_MISMATCH`, and the `decimal` normaliser of step 5 is what reconciles it. The schema also has
`compare: {similarity: {threshold: <number>}}` for text that is nearly the same. Not verified here:
similarity, which no field of the mock pair needs.

**Step 7. Record the differences you already know.** Four customers still differ. Look at one.

```bash
ds compare C00012031 --spec /var/lib/deltasleuth/p0/my-specs/customer.yaml
```

You should see:

```text
C00012031: MISMATCH (DIFFERENT, rechecked)
  MEDIUM VALUE_MISMATCH value_changed /segment
      A: tok:free_text:v1:TVHR3TU2H3PHTDRXYI7W2OXV5X
      B: tok:free_text:v1:QGQ4FSVKVPCKWJFFZPQUSXWVQN
run R-35T7E4V6BCGQ1PVGC3AFNC9C10; observations a O-647079561b2cdad6 b O-4084eb2c9fdefc6d
```

Suppose the two teams have told you that version 2 renamed the customer segment `PRIVATE` to `WEALTH`
on purpose, and that it wrongly leaves out the SMS preference of customers without a mobile number. A
known difference is a rule that recognises such a finding and files it under its reason, so that it
stops hiding the differences nobody knows about. In the file, change the version to `"0.7.0"`. Then
add the rules at the end of the file with this command:

```bash
cat >> my-specs/customer.yaml <<'EOF'
knownDiffs:
  - id: KD-GUIDE-0001
    when: "path == '$.segment' && a == 'PRIVATE' && b == 'WEALTH'"
    classification: expected_by_design
    reason: The new service renamed the PRIVATE segment to WEALTH; reporting maps both to one value.
    ticket: CUST-1042
  - id: KD-GUIDE-0002
    when: "path == '$.preferences.marketing.sms' && change_type == 'missing_in_b'"
    classification: bug
    reason: The new service omits the SMS preference of customers without a mobile number; a fix is planned.
    ticket: CUST-1107
EOF
```

You should see no output. Every rule needs all five members:

| Member | Meaning |
|---|---|
| `id` | A name for the rule in capitals, digits and `-`. Findings and reports cite it. |
| `when` | A condition, tested on each finding. `path` is the finding's place with `[*]` for list members, `a` and `b` are the two values after normalising, and `change_type` is a word such as `value_changed`, `missing_in_b` or `extra_in_b`. It is written in CEL, a small expression language: `==`, `&&`, `\|\|`, text in single quotes. |
| `classification` | `expected_by_design` or `accepted`: the difference is fine. `bug`: it is a known defect that someone is fixing. `accepted` was not run here. |
| `reason` | Why the difference is known, in at least ten characters. |
| `ticket` | Where it is tracked, in the form `CUST-1042`. |

Copy the folder in, validate, and compare the key again. You should see:

```text
warning: spec 17 (guide-customer 0.7.0) needs a four-eyes approval of known_diffs; tokens mode has no approvers, so run R-35T7E56QMTP7RN2QR85ZVV1AJ8 is labelled unapproved
C00012031: KNOWN_DIFF (MATCH, provisional)
  MEDIUM VALUE_MISMATCH value_changed /segment suppressed by KD-GUIDE-0001
      A: tok:free_text:v1:TVHR3TU2H3PHTDRXYI7W2OXV5X
      B: tok:free_text:v1:QGQ4FSVKVPCKWJFFZPQUSXWVQN
run R-35T7E56QMTP7RN2QR85ZVV1AJ8; observations a O-883c02c9244d8277 b O-8d36bac996c8c4a6
```

The finding is still there, marked `suppressed by KD-GUIDE-0001`, and the customer counts as a match.
The warning is new: a rule that hides findings is something a second person should approve. This
installation has nobody who can, so the run goes ahead with a label (section 1, "The warning of steps
14 and 17"). Now the customer of the second rule:

```bash
ds compare C00012035 --spec /var/lib/deltasleuth/p0/my-specs/customer.yaml
```

You should see, after the same warning:

```text
C00012035: KNOWN_DIFF (DIFFERENT, provisional)
  MEDIUM FIELD_MISSING missing_in_b /preferences/marketing/sms suppressed by KD-GUIDE-0002
      A: False
      B: <absent>
run R-35T7E5ABNNBW2QD347CXGJC6KY; observations a O-166443f611f51da2 b O-0c3370f2f764b6d4
```

This one says `DIFFERENT`: a difference filed as a `bug` is explained, but the customer still counts
as different until the bug is fixed. Run the 40 keys. You should see these last two lines:

```text
  total 40, compared 40, match 36, different 4, indeterminate 0, cancelled 0, outstanding 0
  KNOWN_DIFF 2, MATCH 35, MISMATCH 2, NOT_FOUND_B 1
```

These are the counts that `mocks/customer/expected.json` promises for this pair: two real mismatches,
two known differences and one missing customer.

**Step 8. Say how much each difference matters.** Every finding has a severity: `CRITICAL`, `HIGH`,
`MEDIUM`, `LOW` or `INFO`. So far all were `MEDIUM`. Look at the two real mismatches.

```bash
ds compare C00012003 --spec /var/lib/deltasleuth/p0/my-specs/customer.yaml
```

You should see, after the warning:

```text
C00012003: MISMATCH (DIFFERENT, rechecked)
  MEDIUM VALUE_MISMATCH value_changed /consents/tok:free_text:v1:3P6TLWW6SSGT7PN6VFGZDSVC6D/granted
      A: True
      B: False
run R-35T7E64DFDPTG3R7BVJ9A195HT; observations a O-f35f8b18cc763a72 b O-e683791c196efdea
```

```bash
ds compare C00012007 --spec /var/lib/deltasleuth/p0/my-specs/customer.yaml
```

You should see, after the warning:

```text
C00012007: MISMATCH (DIFFERENT, rechecked)
  MEDIUM FIELD_MISSING missing_in_b /accounts/tok:free_text:v1:KN5HGP33SG3M2SFWYUD4GNNKBO
      A: {"accountNo": "tok:free_text:v1:UOWBSD3C4GJFCA567PQAC3EPXR"…
      B: <absent>
run R-35T7E6BY37Y1Y7VAKCA2E6WJ2Y; observations a O-1f51f347722243aa b O-52bbe86e8b8a815b
```

A consent that differs, and an account that the new service lost, matter more than a tag. A field rule
sets the severity of a changed value; a list rule sets the severity of a member that only one side
has. In the file, change the version to `"0.8.0"`, and change two list rules and one field rule and add
one field rule, so that these four lines read:

```text
    - {path: "$.accounts", mode: keyed, key: [sortCode, accountNo], severity: HIGH}
    - {path: "$.consents", mode: keyed, key: [purpose], severity: HIGH}
    - {path: "$.accounts[*].balance", normalize: [{decimal: {scale: 2}}], severity: HIGH}
    - {path: "$.consents[*].granted", severity: CRITICAL}
```

The first three exist already without `severity`. The fourth is new: add it as the last line of
`fields`, after the `interestRate` line. Copy the folder in, validate, and compare the two keys again.
The finding lines now read:

```text
  CRITICAL VALUE_MISMATCH value_changed /consents/tok:free_text:v1:3P6TLWW6SSGT7PN6VFGZDSVC6D/granted
```

```text
  HIGH FIELD_MISSING missing_in_b /accounts/tok:free_text:v1:KN5HGP33SG3M2SFWYUD4GNNKBO
```

**Step 9. Say what may be shown and what must stay hidden.** Until now every text and every number
was shown as a stand-in such as `tok:free_text:v1:TVHR3T…`. The product calls such a stand-in a token;
it has nothing to do with an API token. It is made from the value with a key that only this
installation holds. The same value always gives the same token, so you can see that two values are
equal or different without seeing either. That is the safe default. The `security` block refines it:

- `tokenize` lists fields that hold personal data, each with its class: `name`, `phone`, `email`,
  `tax_id`, `address`, `account_number`, `device_id`, `date_of_birth`, `identifier`, `free_text` or
  `free_number`. The class becomes part of the token.
- `clear` lists fields whose values may be shown as they are: codes, flags, dates of no personal
  meaning.
- `unlisted` says what happens to every other field: `tokenize`, which is also what applies without
  the line, or `clear`.

The key has a class too, in `key.class`. Without one it is `business_code`, and keys are shown as they
are, which is why you have seen `C00012031` in every output. A customer number is personal data. In
the file, change the version to `"0.9.0"` and add this line under the `pattern` line of `key`. It
starts with two spaces.

```text
  class: identifier
```

Then add the `security` block at the end of the file:

```bash
cat >> my-specs/customer.yaml <<'EOF'
security:
  unlisted: tokenize
  tokenize:
    - {path: "$.givenName", class: name}
    - {path: "$.familyName", class: name}
    - {path: "$.email", class: email}
    - {path: "$.phones[*].number", class: phone}
    - {path: "$.accounts[*].accountNo", class: account_number}
  clear:
    - "$.segment"
    - "$.createdAt"
    - "$.phones[*].kind"
    - "$.preferences.marketing.sms"
    - "$.accounts[*].sortCode"
    - "$.accounts[*].interestRate"
    - "$.consents[*].purpose"
    - "$.consents[*].granted"
    - "$.tags[*]"
EOF
```

You should see no output. Copy the folder in and validate. You should see a warning now, which step 11
explains:

```text
warning: /key/source/keys [inline_keys_stored] inline keys of class identifier are stored in clear with the spec and never purged; pass them with 'ds run --keys'
ok: customer.yaml is valid (1 warning(s))
```

Compare `C00012031`, `C00012003` and `C00012007` once more. You should see, each after a warning that
now names `clear, known_diffs`:

```text
tok:identifier:v1:EIMOS4OXMIEE55UAPD5Y67DMGT: KNOWN_DIFF (MATCH, provisional)
  MEDIUM VALUE_MISMATCH value_changed /segment suppressed by KD-GUIDE-0001
      A: PRIVATE
      B: WEALTH
```

```text
tok:identifier:v1:JNDN7BU6JY4ORQGSFFFSKBJYZ3: MISMATCH (DIFFERENT, rechecked)
  CRITICAL VALUE_MISMATCH value_changed /consents/MARKETING/granted
      A: True
      B: False
```

```text
tok:identifier:v1:ZQMSDCJJADY62DB22ISN5LZIHR: MISMATCH (DIFFERENT, rechecked)
  HIGH FIELD_MISSING missing_in_b /accounts/tok:account_number:v1:KOAPFXIDPKPGONJB4C55XS3EDQ
      A: {"accountNo": "tok:account_number:v1:BKKD74PEXQNUHS4IF6N4KT…
      B: <absent>
```

The customer number is a token now, in every output. The segment and the consent's purpose are
readable, because the spec says they may be. The account number is still a token, of the class
`account_number`. [Chapter 5](05-runs.md) has more on masked values and on finding one record among
them. Not verified here: the class `date_of_birth`, which this spec does not use.

**Step 10. Set how a difference is confirmed.** `recheck` has been in the file since step 1. It can
say more:

| Member | Meaning |
|---|---|
| `delay` | How long to wait before the second read, for example `1s`, `30s`, `5m`. |
| `times` | How many more reads at most, from 1 to 10. Rechecking cannot be switched off. |
| `outcomes` | Which first results are read again. `MISMATCH`, `NOT_FOUND_A` and `NOT_FOUND_B` must be in the list; `INCONCLUSIVE` may be. |
| `oneShot` | The same for one key compared at once, as `ds compare` and the "Compare now" button do. Without it the wait is two seconds. |

In the file, change the version to `"0.10.0"` and replace the line `recheck: {delay: 1s, times: 1}`
with:

```text
recheck:
  delay: 1s
  times: 1
  outcomes: [MISMATCH, NOT_FOUND_A, NOT_FOUND_B]
  oneShot: {delay: 250ms, times: 1}
```

Copy the folder in, validate, and compare `C00012003` again. The output is the same as in step 9, and
it comes sooner: here the command took 3 to 4 seconds before the change and 2 after it.

To see what the second read is for, use the other mock pair, which has a record that changes between
two reads. Copy its bundled spec into the working folder, copy the folder in, and restart the mocks so
that they forget earlier calls.

```bash
cp mocks/linker/spec.yaml my-specs/linker.yaml
```

You should see no output.

```bash
docker compose cp my-specs app:/var/lib/deltasleuth/p0/
```

You should see the `Copied` line.

```bash
docker compose --profile mocks restart mocks
```

You should see ` Container deltasleuth-mocks-1  Started`. Compare the key twice, with the same command.

```bash
ds compare ORG-000029 --spec /var/lib/deltasleuth/p0/my-specs/linker.yaml
```

The first time you should see, after a warning about `clear`:

```text
ORG-000029: DRIFT (INDETERMINATE, rechecked) reason b_converged
run R-35T7E8Q30EN211JCXSFJCDVPCQ; observations a O-e91bfe3c7dbcd57f b O-e773ca0ee2150eae
```

The second time:

```text
ORG-000029: MATCH (MATCH, provisional)
run R-35T7E8VEM3JC6XBK62DXA9C9GH; observations a O-17788a21b3a863e1 b O-199ed106f5414d0f
```

The new service answered with an old name once, and with the current name from then on. Without a
second read the first comparison would have reported a mismatch that was already gone. With it,
DeltaSleuth reports `DRIFT`: side b caught up with side a between the two reads.

**Step 11. Choose where the keys come from.** This chapter uses three places, and describes a fourth
and a key of several values.

*A list given to the run.* This is what `--keys` has done since step 2: a text file, one key on each
line; a line that starts with `#` is skipped. In the web UI it is the box "Keys, one per line" of "New
run". The keys belong to the run, not to the spec.

*A list in the spec.* `key.source` with `kind: list`, which the file has had since step 1 with one
key. A run without `--keys` takes it. Make a copy with three keys and run it:

```bash
sed -e 's/keys: \[C00012001\]/keys: [C00012001, C00012003, C00012031]/' -e 's/version: "0.10.0"/version: "0.10.1"/' my-specs/customer.yaml > my-specs/customer-inline.yaml
```

You should see no output.

```bash
docker compose cp my-specs app:/var/lib/deltasleuth/p0/
```

You should see the `Copied` line.

```bash
ds run /var/lib/deltasleuth/p0/my-specs/customer-inline.yaml
```

You should see, after the `started` line and the warning:

```text
completed
  total 3, compared 3, match 2, different 1, indeterminate 0, cancelled 0, outstanding 0
  KNOWN_DIFF 1, MATCH 1, MISMATCH 1
```

The warning that validation has printed since step 9 is about this kind of list: keys written into a
spec are stored with it, readable, for as long as the spec exists. For keys that are personal data,
give the list to the run instead and keep one harmless key in the spec, as `customer.yaml` does.

*A listing that a service serves.* With `kind: paginated_endpoint` DeltaSleuth asks a target for its
keys, page by page, when the run starts. The block names the target, the request of the first page,
how the pages are walked and where a key is in each item. Neither mock pair serves a listing, so the
spec below validates and its run then fails; that is what you can reproduce. Create the file:

```bash
cat > my-specs/listing.yaml <<'EOF'
apiVersion: deltasleuth/v1
kind: ComparisonSpec
metadata:
  name: guide-listing
  version: "0.1.0"
  owner: guide-reader
normalizerVersion: "1.1.0"
key:
  name: customer_id
  type: string
  pattern: "^C[0-9]{8}$"
  source:
    kind: paginated_endpoint
    target: customer-a
    request: {method: GET, path: "/api/v1/customers"}
    pagination: {kind: cursor, itemsPath: "$.items", cursorPath: "$.next", cursorParam: cursor}
    keyPath: "$.id"
sides:
  a:
    role: legacy
    target: customer-a
    request: {method: GET, path: "/api/v1/customers/{{ key }}"}
    truncation: {countPath: "$.customer.accountCount", itemsPath: "$.customer.accounts", cap: 50}
  b:
    role: new
    target: customer-b
    request: {method: GET, path: "/v2/customers/{{ key }}"}
    truncation: {countPath: "$.data.productCount", itemsPath: "$.data.products", cap: 100}
compare: {}
recheck: {delay: 1s, times: 1}
EOF
```

You should see no output. Copy the folder in, then validate and run it.

```bash
ds spec validate /var/lib/deltasleuth/p0/my-specs/listing.yaml --targets /etc/deltasleuth/targets.yaml
```

You should see:

```text
ok: listing.yaml is valid (0 warning(s))
```

```bash
ds run /var/lib/deltasleuth/p0/my-specs/listing.yaml
```

You should see:

```text
error: the key listing was not read completely (HTTP 404: an incomplete or unstable page walk)
```

The `pagination` line reads: the items of a page are the list at `items`; the member `next` holds the
word to send as the query parameter `cursor` to get the next page; no `next` means the last page. The
other kinds are `offset` and `page`, which need `pageParam`, `sizeParam` and `size`, and `link_header`.
Not verified here with the bundled mocks: a run that gets its keys from a listing. It was checked with
a small stand-in listing service, which the product does not ship: with the block above pointed at
it, a run walked three pages of 15, 15 and 10 customer numbers and compared all 40, with the counts of
step 7.

*A table, a view or files of keys* (a key table). With `kind: table`, a `target` and a `table`,
DeltaSleuth reads the keys from a database's table or view, or from CSV or Parquet files, which need
be neither side's:

```yaml
  source: {kind: table, target: keys-db, table: {name: shelf_keys, keyColumns: [shelf_code], where: "live = 1"}}
```

The target is declared as a table side's is ([chapter 6](06-tables.md); for files,
[chapter 7](07-engines-and-files.md)). The table is read when the run is created and again at each
cycle's end of a rolling soak: each key once, at most 1,000,000 keys; a row without a key refuses the
run (`the key source holds 1 row(s) without a key: leave them out with table.where`). Not verified here
with the bundled mocks, whose installation has no table of keys; checked with a pair of stand-in
services, which the product does not ship, where a PostgreSQL table whose filter selected 14 rows gave
a run its 12 keys.

*A key of several values* (a multi-part key). A service asked with more than one value takes
`type: composite` and `parts: [region, shelf, code]` in `key`; each part must fit `pattern`, and a
request names one as `{{ keyParts.region }}`, as in
`path: "/v1/{{ keyParts.region }}/shelves/{{ keyParts.shelf }}"`. Such a key is written as its parts in
that order joined by `|`, for example `eu|s1|7`, in a line of `--keys`, the "Keys" box, an inline list
and `ds compare` alike (`\|` and `\\` for a `|` or `\` inside a part), and a key table gives one key
column for each part. Not verified here with the bundled mocks, whose keys have one part; checked with
the same pair of stand-in services.

The schema names more kinds of key source than these, and this build runs only some of them. It
refuses `csv`, `sql` and `run_keys`, each with a line such as `not supported: key source 'csv'; use
list, paginated_endpoint or a key table`. It refuses a `table` that names a `side`, and a `union`,
standing alone: their messages say that they belong inside a `hash_sample`, the kind that draws a
sample of a listing's keys instead of comparing them all. A spec with a `hash_sample` over a listing validated. Each was tried here with `ds spec
validate` only. Sampled runs are the subject of [chapter 11](11-sign-off.md), section 3.

**Step 12. Leave a field out, with care.** `compare.exclude` lists paths that are not compared at all,
and `compare.include` lists the only paths that are. Both hide differences, so use them for fields
that truly do not matter. This makes a test copy that excludes the preferences:

```bash
sed 's/name: guide-customer/name: guide-exclude-test/' my-specs/customer.yaml | awk '{print} /^compare:$/ {print "  exclude: [\"$.preferences\"]"}' > my-specs/exclude-test.yaml
```

You should see no output. Copy the folder in, then compare the customer of the second known
difference with the copy.

```bash
ds compare C00012035 --spec /var/lib/deltasleuth/p0/my-specs/exclude-test.yaml
```

You should see:

```text
warning: spec 32 (guide-exclude-test 0.10.0) needs a four-eyes approval of clear, known_diffs, population_filters; tokens mode has no approvers, so run R-35T7E9J2S4QJEJN22HVPE6KH5P is labelled unapproved
tok:identifier:v1:T6P53WPXUVT3ZZATNUW3OKG6MI: MATCH (MATCH, provisional)
run R-35T7E9J2S4QJEJN22HVPE6KH5P; observations a O-88321674f5ff0f0f b O-be5b163f7f01310a
```

The known bug is gone from the result, without a trace, and the warning names one more thing a second
person should approve: `population_filters`. A known difference with its reason (step 7) is the better
tool whenever the field matters.

**The finished file.** After step 10, `my-specs/customer.yaml` has 110 lines in this order: the head
with `metadata` and `normalizerVersion`; `key` with `class`; `sides` with `request`, `truncation`,
`notFound`, `select` and `map` on each side; `compare` with four `arrays` rules and six `fields` rules;
`recheck` with four members; `knownDiffs` with two rules; `security`. "How to read the result" has a
way to check that your file equals this chapter's. The bundled `mocks/customer/spec.yaml` is the same
pair with the whole customer profile mapped, and gives the same counts.

### How to read the result

**The first line of `ds compare`** reads `<key>: <outcome> (<verdict>, <confirmation>)`, sometimes
followed by `reason <word>`.

| Outcome | Meaning | Verdict |
|---|---|---|
| `MATCH` | No finding above `INFO`. | `MATCH` |
| `MISMATCH` | At least one finding above `INFO` that no known difference explains. | `DIFFERENT` |
| `KNOWN_DIFF` | Every finding above `INFO` is explained by a known difference. | `MATCH`, or `DIFFERENT` when a rule filed as `bug` explained it |
| `NOT_FOUND_B`, `NOT_FOUND_A` | One side holds the record and the other says, in the way its target declares, that it does not. | `DIFFERENT` |
| `DRIFT` | The second read saw a side change. | `INDETERMINATE` |
| `INCONCLUSIVE` | A side could not be read in a way that allows a judgement. | `INDETERMINATE` |

The confirmation is `rechecked` when a second read confirmed the first result, and `provisional` when
there was none, as for every `MATCH`. [Chapter 5](05-runs.md) lists every outcome and reason.

**Each finding** takes three lines: `<severity> <code> <change> <place>`, then the value on side a and
on side b. The place is a path into the canonical document. In a keyed list the member's key value
stands in the path, as in `/consents/MARKETING/granted`; it is a token unless the spec shows that
field. `suppressed by <id>` names the known difference that explains the finding.

**The last line** names the run that holds the result: every `ds compare` makes a run of one key, of
the mode `one_shot`, which the Runs screen lists. The two `O-` ids are the stored answers of the two
sides.

**The two lines of `ds run`.** `total` is the number of keys. `compared` counts those both sides
answered for; `match`, `different` and `indeterminate` add up the verdicts of the table above. The
second line counts outcomes. In step 7, `match 36` is 35 `MATCH` and the known difference that is fine,
and `different 4` is 2 `MISMATCH`, the missing customer and the known bug.

**Check your edits.** A stored spec has a content hash, a fingerprint of what the file says. It does
not depend on your machine, on comments or on the order of lines. If your file equals this chapter's
after a step, the hash of that version begins as below. The command of section 4, step 3, lists the
hashes, and so does the "Hash" column of the Specs screen.

| Version | After step | Hash begins |
|---|---|---|
| `0.1.0` | 1 | `5aeca267b3e4` |
| `0.2.0` | 2 | `e0a41ced5d3d` |
| `0.3.0` | 3 | `5fd30c31d2e2` |
| `0.4.0` | 4 | `5e8415a89411` |
| `0.5.0` | 5 | `8592fc23c7d0` |
| `0.6.0` | 6 | `9b64140df01b` |
| `0.7.0` | 7 | `7990ef4c8ecd` |
| `0.8.0` | 8 | `e82968c70b05` |
| `0.9.0` | 9 | `fe00e79f7de2` |
| `0.10.0` | 10 | `d1d95349eeca` |

### Limits

- Only a side's target and request are fields ("New comparison"). Every other part of a spec is typed.
- "Send" shows the answer as it came only in a trial installation on one machine, which chapter 1
  makes. Any other installation, one that runs from source without sign-in among them, reads "Every
  value is a stand-in: equal values get equal stand-ins." and shows the answer's structure with a `tok:`
  stand-in for every text and number. A member's name is a stand-in too when it reads as data: an id,
  an e-mail address, a phone number, or a name with three digits in a row or four in all, such as an
  account number. A name that is data with fewer digits, such as a surname, is shown. No installation
  shows anything of an answer with status 401 or 403.
- "Send" reads at most 1 MiB of an answer, and only JSON: "the answer is not JSON" stands for anything
  else. A trial shows no answer whose numbers would take over 4 MiB written in full (thousands of
  numbers such as `1e3999`): "the answer's numbers, written in full, take over 4194304 characters".
- A number too large for a document (beyond about 1.8e308, such as `1e999`) in the YAML or in a
  request's body is refused by "Validate", "Save as a new version" and "Send" alike, with the
  code `number_out_of_range`.
- "Import curl…" reads one command in the form for bash. It refuses `-k` ("DeltaSleuth always verifies
  TLS: remove -k."), a body from a file, and anything but `GET` and `POST` with a JSON body ("Only GET,
  and POST with a JSON body, are sent."). It reads the `curl` command alone: for `| jq .`, `> out.json`,
  `;` or `&&` after it the page says "Paste the curl command alone: remove | and what follows it.", with
  the sign it met. A `-b` that names a file, such as `-b cookies.txt`, is refused with "Import does not
  read a cookie file: remove it.".
- A header that carries a credential is recognised by its name (`Authorization`, `Cookie`, and names
  that hold `secret`, `password`, `apikey`, `auth-token`, `access-token` and the like) or by a value
  that reads as a token. A header of another name, such as `X-CSRF-Token`, stays in "Headers" and would
  be stored with the spec in clear: delete the line and add the header on "Targets" with "Add another
  credential header".
- This build refuses some members that the schema knows, each with the code `unsupported`. Three were
  tried here: `canonical: {mode: graph}` (`not supported: graph mode`), `time: {mode: frozen}`
  (`not supported: time.mode 'frozen'; both sides are read live`) and `sampling: {mode: hash}`
  (`not supported: sampling.mode 'hash'; every key is compared`). Others are accepted and not applied,
  with a warning of the code `ignored`; tried here: `reporting: {formats: [csv]}`
  (`/reporting/formats is not applied: reports have a fixed set of outputs`).
- A spec may carry more than this section shows, for example an `execution` block for pauses, timeouts
  and retries, which `mocks/customer/spec.yaml` has. With and without `execution: {concurrency: 4}` a
  run of the 40 mock keys took the same seven seconds here, so nothing is claimed about it.
- Sides that are database tables or files are written differently. They are the subject of the
  chapters on tables and on engines and files.
- Under single sign-on, a spec with `security.clear` entries, `unlisted: clear`, known differences or
  `include` and `exclude` needs a second person's approval before it runs. Not verified here: this
  installation signs in with tokens, where such runs go ahead labelled unapproved, as the warnings say.
- This chapter met no command, route or button that turns a token back into its value. The runbook's
  table of roles calls revealing values "not built yet".

### If something goes wrong

**The result did not change after an edit**
You did not copy the folder in. `ds` read the older copy. Run the `docker compose cp` command.

**`error: / [document_unreadable] cannot read my-specs/customer.yaml: No such file or directory`**
`ds` was given the path on your machine. Give it the path inside the container, which starts with
`/var/lib/deltasleuth/p0/`.

**`error: 1 key(s) do not match key.pattern after key.normalize, or are over 1024 bytes or hold NUL (positions 1)`**
The key given to `ds compare`, or a key in the `--keys` file, does not match the spec's `key.pattern`.
Provoked with `ds compare 12001`. Position 1 is the first key.

**`error: No such file or directory: /var/lib/deltasleuth/p0/my-specs/nokeys.txt`**
The `--keys` file is not in the container. Check its name and copy the folder in.

**`error: the key listing was not read completely (HTTP 404: an incomplete or unstable page walk)`**
A `paginated_endpoint` key source asked a path that does not serve a listing (step 11). Check the
request's path and the pagination members against the service.

**`<key>: INCONCLUSIVE (INDETERMINATE, rechecked) reason recheck_failed`**, for a record one side does not hold
The side has no `notFound`, so a 404 is not taken for "no such record" (step 3).

**A run takes minutes**
The spec's `recheck.delay` is long, and a run waits that long before its second reads. A draft of
section 5 says `5m`: a run of it on the 40 mock keys took 305 seconds here. A compare of one key does
not wait for `recheck.delay`: with the same draft it took 4 seconds. It waits for
`recheck.oneShot.delay`, which is two seconds when the spec does not say. With `oneShot: {delay: 20s,
times: 1}` a compare of a key that differs took 22 seconds here, and one of a key that matches took 2.

**`error: set DS_OPERATOR to your user name (docs/mvp/AUTH.md §4.2)`**
`ds compare` or `ds run` was started without the alias of "Before you begin". Define the alias.

**`warning: spec N (...) needs a four-eyes approval of ...; tokens mode has no approvers, so run R-... is labelled unapproved`**
Not an error. See section 1, "The warning of steps 14 and 17".

Messages about the file itself, such as a misspelt member, are in section 3.

## 3. Validate a spec

### What it is for

Validation reads a spec without calling any service and says whether DeltaSleuth could run it. It
checks three things: the form of the file against the schema, a set of rules the schema cannot
express, and, when it knows the targets file, whether the targets exist and declare what the spec
relies on. A spec author validates after every edit; an upload validates again by itself.

### Before you start

Nothing has to be switched on. For the command line, the spec must be in the container (the copy
command of "Before you begin"). For the web UI, sign in with a token of the role `spec_author`, such as
`first-run` of chapter 1; with a lower role the panel is not shown.

### In the web UI

1. Click "Specs" in the bar at the top. The page is headed "Specs".
2. In the panel "Upload a spec", press the file chooser and pick a `.yaml`, `.yml` or `.json` file, or
   paste the text into the box. The buttons "Validate" and "Save" become usable once the box holds
   text.
3. Press "Validate".
   - For a spec without issues, "No issues." appears beside the buttons, and "None." below them.
   - Otherwise a table appears below the buttons, one row for each issue, with the columns "Pointer",
     "Severity", "Code" and "Message". A row whose "Severity" is `warning` does not stop a save; a row
     with `error` does.

The screen checks against the targets the app knows: the targets file as it was at the app's start, and
the targets declared in the browser. It cannot check against another targets file. "Validate" on the
page "New comparison" (section 2) is the same check.

### On the command line

1. Validate against the installation's targets file. This is the form to use.
   ```bash
   ds spec validate /var/lib/deltasleuth/p0/my-specs/customer.yaml --targets /etc/deltasleuth/targets.yaml
   ```
   You should see, for the file of section 2:
   ```text
   warning: /key/source/keys [inline_keys_stored] inline keys of class identifier are stored in clear with the spec and never purged; pass them with 'ds run --keys'
   ok: customer.yaml is valid (1 warning(s))
   ```
2. Validate a spec against a targets file of its own. The public examples of the product each bring
   one, for services that this installation does not declare. Copy them in first.
   ```bash
   docker compose cp examples/public app:/var/lib/deltasleuth/p0/
   ```
   You should see a `Copied` line.
   ```bash
   ds spec validate /var/lib/deltasleuth/p0/public/rickandmorty/spec.yaml --targets /var/lib/deltasleuth/p0/public/rickandmorty/targets.yaml
   ```
   You should see:
   ```text
   warning: /security/unlisted [unapproved_clear] under SSO a run needs a four-eyes approval of unlisted_clear
   ok: spec.yaml is valid (1 warning(s))
   ```
   Now validate the same spec against the installation's own targets file.
   ```bash
   ds spec validate /var/lib/deltasleuth/p0/public/rickandmorty/spec.yaml --targets /etc/deltasleuth/targets.yaml
   ```
   You should see:
   ```text
   warning: /security/unlisted [unapproved_clear] under SSO a run needs a four-eyes approval of unlisted_clear
   error: /sides/a/target [unknown_target] target 'rickandmorty-rest' is not in the registry
   error: /sides/b/target [unknown_target] target 'rickandmorty-graphql' is not in the registry
   error: 2 blocking issue(s) in spec.yaml
   ```
   A spec is valid for an installation only with the targets that installation declares.
3. Ask the app, as the "Validate" button does. The file is read on your machine, so nothing has to be
   copied in.
   ```bash
   { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - -H 'Content-Type: application/yaml' --data-binary @my-specs/customer.yaml http://localhost:8000/v1/specs/validate
   ```
   You should see:
   ```text
   {"valid": true, "issues": [{"pointer": "/key/source/keys", "message": "inline keys of class identifier are stored in clear with the spec and never purged; pass them with 'ds run --keys'", "code": "inline_keys_stored", "severity": "warning"}]}
   ```

Now five common mistakes. Each command makes a broken copy of `my-specs/customer.yaml`.

4. Misspell a member: `patern` for `pattern`.
   ```bash
   sed 's/  pattern:/  patern:/' my-specs/customer.yaml > my-specs/broken-1.yaml
   ```
5. Name a target that is not declared.
   ```bash
   sed 's/target: customer-a/target: customer-v1/' my-specs/customer.yaml > my-specs/broken-2.yaml
   ```
6. Leave out side a's `truncation` line.
   ```bash
   sed '/accountCount/d' my-specs/customer.yaml > my-specs/broken-3.yaml
   ```
7. Indent one line by a space too many.
   ```bash
   sed 's/^  type: string/   type: string/' my-specs/customer.yaml > my-specs/broken-4.yaml
   ```
8. Put a key in the list that does not match the pattern.
   ```bash
   sed 's/keys: \[C00012001\]/keys: [C00012001, 12002]/' my-specs/customer.yaml > my-specs/broken-5.yaml
   ```
   You should see no output from the commands of steps 4 to 8.
9. Copy the folder in.
   ```bash
   docker compose cp my-specs app:/var/lib/deltasleuth/p0/
   ```
   You should see the `Copied` line.
10. Validate each copy: this command with `broken-1.yaml` to `broken-5.yaml` in turn.
    ```bash
    ds spec validate /var/lib/deltasleuth/p0/my-specs/broken-1.yaml --targets /etc/deltasleuth/targets.yaml
    ```
    You should see, for the five files in turn, these lines. The warning of step 1 comes with each
    of them but the fourth, which has only its one line:
    ```text
    error: /key [schema.required] 'pattern' is a required property
    error: /key/patern [schema.additionalProperties] unexpected member 'patern'
    error: 2 blocking issue(s) in broken-1.yaml
    ```
    ```text
    error: /sides/a/target [unknown_target] target 'customer-v1' is not in the registry
    error: 1 blocking issue(s) in broken-2.yaml
    ```
    ```text
    error: /sides/a [truncation_undeclared] neither this side nor target 'customer-a' declares truncation indicators (X33), so truncation would go undetected
    error: 1 blocking issue(s) in broken-3.yaml
    ```
    ```text
    error: / [yaml_syntax] mapping values are not allowed here (line 10, column 8)
    ```
    ```text
    error: /key/source/keys/1 [invalid_key] after key.normalize the key is empty, '.', '..', spans lines or fails key.pattern
    error: 1 blocking issue(s) in broken-5.yaml
    ```
11. Validate the second copy without `--targets`.
    ```bash
    ds spec validate /var/lib/deltasleuth/p0/my-specs/broken-2.yaml
    ```
    You should see:
    ```text
    warning: /key/source/keys [inline_keys_stored] inline keys of class identifier are stored in clear with the spec and never purged; pass them with 'ds run --keys'
    ok: broken-2.yaml is valid (1 warning(s))
    ```
    Without `--targets` nothing is checked against the targets, and a spec that names an unknown
    target is called valid. Always give `--targets`.
12. Ask the app about the second copy, as the "Validate" button would.
    ```bash
    { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - -H 'Content-Type: application/yaml' --data-binary @my-specs/broken-2.yaml http://localhost:8000/v1/specs/validate
    ```
    You should see:
    ```text
    {"valid": false, "issues": [{"pointer": "/key/source/keys", "message": "inline keys of class identifier are stored in clear with the spec and never purged; pass them with 'ds run --keys'", "code": "inline_keys_stored", "severity": "warning"}, {"pointer": "/sides/a/target", "message": "target 'customer-v1' is not in the registry", "code": "unknown_target", "severity": "error"}]}
    ```
    These four members of each issue are the four columns of the screen's table.

### How to read the result

Every line has the same form: `<severity>: <place> [<code>] <message>`.

- The severity is `error` or `warning`. One error is enough for `ds spec validate` to end with
  `error: N blocking issue(s) in <file>` and exit status 2. Warnings alone end with `ok: <file> is
  valid (N warning(s))` and status 0.
- The place is a path into the spec, with `/` between names and list positions counted from 0:
  `/key/source/keys/1` is the second key of the list, `/compare/fields/2/normalize/1` the second
  normaliser of the third field rule. `/` alone is the whole file.
- The code is a fixed word you can search for. Codes that begin with `schema.` come from the form of
  the file. `yaml_syntax` means the file is not YAML at all: then it is the only line, with the line
  and column of the trouble, and nothing else was checked.

All problems of a readable file are reported in one pass, so correct them all before you try again.

### Limits

- Validation calls no service. It cannot know whether a path exists on the service, whether `select`
  finds anything in a real answer, or whether a map produces what you meant. Only a comparison shows
  that (section 2).
- `ds spec validate` reads a file inside the container. It has no form that reads your terminal's
  input, and the image does not hold `mocks` or `examples`, so the bundled specs must be copied in too.
- The web UI validates against the app's own targets only.

### If something goes wrong

Beside the five messages above, these were provoked the same way, each with one changed line of
`customer.yaml`:

**`error: /compare/fields/2/normalize/1 [schema.enum] must be one of 'trim', 'collapse_ws', 'nfkc', 'casefold', 'upper', 'lower', 'email_basic', 'digits_only', 'null_if_empty', 'address_std'`**
A normaliser's name is wrong, here `lowercase` for `lower`.

**`error: /sides/a/map [invalid_jsonata] S0202: Expected ], got } (at character 469)`**
A `map` is not a complete JSONata expression: here a `]` was missing. The position counts characters
from the start of the map.

**`error: /normalizerVersion [unsupported] not supported: normalisers 1.0.0; this build implements 1.1.0`**
`normalizerVersion` names another version than this build has. Write `"1.1.0"`.

**`error: /metadata/version [schema.type] must be of type 'string'`**
The version is written without quotes and with two numbers, such as `version: 1.0`, which YAML reads as
a number. Write three numbers in quotes: `"1.0.0"`.

**`error: /knownDiffs/0 [schema.required] 'ticket' is a required property`**, **`error: /knownDiffs/0/reason [schema.minLength] must have at least 10 character(s)`**
A known difference lacks its ticket, or its reason is too short to be one.

**`error: /knownDiffs/0/when [invalid_cel] 1:6: Syntax error: token recognition error at: '= '`**
A condition is not valid CEL: here `=` was written for `==`.

**`warning: /knownDiffs/0/when [path_literal_not_canonical] path is compared with 'segment', which never equals a path pattern (it is not a path pattern)`**
A condition compares `path` with a text that no path can equal. Write the path as findings show it,
`'$.segment'`. It is only a warning, and the rule would never match.

**`error: /key/source/kind [unsupported] not supported: key source 'csv'; use list, paginated_endpoint or a key table`**
`key.source` has a kind that the schema names and this build does not run: `csv`, `sql` or `run_keys`.
Use a list, a listing or a key table (section 2, step 11).

**`error: /sides/a/notFound/status/1 [not_found_undeclared] status 410 is not in target 'customer-a' authoritative_absence.status [404] (X2)`**
A side lists a "no such record" status that its target does not declare. Remove it, or have the
target's `capabilities` declare it.

**`error: /security/clear/1 [security_path_conflict] $.email is both tokenised and clear`**
One path is in both lists of `security`.

**`error: /sides/a/request/headers/Authorization [inline_credential] the value looks like an inline credential; credentials come only from secret_ref`**
A request carries a credential header. Remove it; the target's `auth` sends the credential (section 1).

**`error: / [yaml_syntax] while scanning for the next token; found character '\t' that cannot start any token (line 9, column 1)`**
A line is indented with a tab. Use spaces.

**`error: /recheck/times [schema.minimum] must be >= 1`**
`recheck.times` is 0. Rechecking cannot be switched off.

**`error: / [document_unreadable] cannot read deploy/config/targets.yaml: No such file or directory`**
`--targets` was given a path of your machine. Inside the container the installation's targets file is
`/etc/deltasleuth/targets.yaml`.

## 4. Store a spec, and its versions

### What it is for

A run does not read your file. It reads a copy that the app has stored in its database, so that months
later it is still certain what a run compared. Storing a spec makes it available to runs, schedules and
pre-checks. A stored version is never changed and never deleted: a change to the file is stored as one
more version beside the old ones.

### Before you start

Nothing has to be switched on. The spec's targets must be declared in the targets file the app has
read (section 1), or the upload is refused. For the web UI and the API you need a token of the role
`spec_author`, such as `first-run`.

### In the web UI

1. Click "Specs" in the bar at the top.
2. In the panel "Upload a spec", choose your file or paste its text into the box.
3. Press "Save".
   - A spec that validates is stored: "Saved as spec #N." appears beside the buttons, and the table
     at the top of the page gains a row. If this content is stored already, nothing new is stored: N
     is the id the content has, and the table keeps its rows. After section 2 on the command line
     that is the case for `my-specs/customer.yaml`.
   - A spec with an error is not stored: "invalid spec" appears beside the buttons, and the issues
     table below them (section 3).
4. Read the table at the top. Its columns are "Id", "Name", "Version", "Hash" (the first twelve
   characters of the content hash), "Created", "Approval" (empty under token sign-in), "Schedules"
   with a link "Schedule…", and "Table pre-check" with a link "Pre-check…". The newest version is first.

Click a version's name in the table to open it. With a token of the role `spec_author` it opens in the
comparison builder of section 2, where a save stores a new version and never changes the one you
opened; with a lower role it is shown as YAML, read-only. What opens is the stored content, not your
file: without your comments, its members in another order, and the keys of a list as `tok:` stand-ins
unless the key's class is `business_code`. Stand-ins cannot be saved back; the page says "The inline
keys of this text are stand-ins: type the keys again, or declare another key source and give keys with
the run." Keep your own copy of every spec you store.

### On the command line

`ds` has no command that uploads, lists or shows specs: `ds spec` has only `validate`. A spec is
stored as a side effect, the first time `ds run` or `ds compare` is given the file. Every step of
section 2 did that. To store a spec without running it, and to list and read stored specs, ask the app.

1. Store the file. It is read on your machine.
   ```bash
   { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - -H 'Content-Type: application/yaml' --data-binary @my-specs/customer.yaml http://localhost:8000/v1/specs
   ```
   You should see:
   ```text
   {"spec_id": 27, "name": "guide-customer", "version": "0.10.0", "content_hash": "d1d95349eeca0300a993152f494ada2332a4ed95d418e0d301050182a960b7c8", "needs": ["clear", "known_diffs"], "approval_id": null}
   ```
   The id was 27 already: `ds compare` stored this content in section 2, step 10, and the same content
   is never stored twice. Run the command again and the answer is the same; sent three times here, it
   left the list of step 3 with the rows it had.
2. See what counts as a change. Make one copy that differs only by a comment, and one in which the key
   of the list differs but the version does not.
   ```bash
   { echo '# reviewed by the payments team'; cat my-specs/customer.yaml; } > my-specs/comment-only.yaml
   ```
   You should see no output.
   ```bash
   sed 's/keys: \[C00012001\]/keys: [C00012002]/' my-specs/customer.yaml > my-specs/same-version.yaml
   ```
   You should see no output. Store both, with the command of step 1 and `comment-only.yaml`, then
   `same-version.yaml`, in place of `customer.yaml`. You should see, for the first, the answer of step
   1 again, with `"spec_id": 27`. For the second:
   ```text
   {"spec_id": 37, "name": "guide-customer", "version": "0.10.0", "content_hash": "f345799027b39ee366aebd7a27e2a1bd720478d1e6c87ed70ae3b03d254f92c5", "needs": ["clear", "known_diffs"], "approval_id": null}
   ```
3. List the stored versions of the name, newest first.
   ```bash
   { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - http://localhost:8000/v1/specs | tr '{' '\n' | grep '"guide-customer"'
   ```
   You should see one line for each version; the first four here:
   ```text
   "spec_id": 37, "name": "guide-customer", "version": "0.10.0", "content_hash": "f345799027b39ee366aebd7a27e2a1bd720478d1e6c87ed70ae3b03d254f92c5", "created_at": "2026-10-05T20:33:32.532375+00:00", "approval": null},
   "spec_id": 30, "name": "guide-customer", "version": "0.10.1", "content_hash": "652283e61ce7dae80fafb4c2d7d493373f7d461229416c707310e5d0f9318558", "created_at": "2026-10-05T20:32:47.727738+00:00", "approval": null},
   "spec_id": 27, "name": "guide-customer", "version": "0.10.0", "content_hash": "d1d95349eeca0300a993152f494ada2332a4ed95d418e0d301050182a960b7c8", "created_at": "2026-10-05T20:32:31.444779+00:00", "approval": null},
   "spec_id": 24, "name": "guide-customer", "version": "0.9.0", "content_hash": "fe00e79f7de204ac11c9b0bfc176f68e49713c8191228bb0e48495e55798d3d4", "created_at": "2026-10-05T20:32:19.800882+00:00", "approval": null},
   ```
4. Read one stored version back. `<spec id>` is its number, here 27. The command prints the first 420
   characters.
   ```bash
   { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - http://localhost:8000/v1/specs/<spec id> | head -c 420
   ```
   You should see:
   ```text
   {"spec_id": 27, "name": "guide-customer", "version": "0.10.0", "content_hash": "d1d95349eeca0300a993152f494ada2332a4ed95d418e0d301050182a960b7c8", "body": {"key": {"name": "customer_id", "type": "string", "class": "identifier", "source": {"keys": ["tok:identifier:v1:CQNWAWB5FGXEMVKM6Y5L3FMWV7"], "kind": "list"}, "pattern": "^C[0-9]{8}$"}, "kind": "ComparisonSpec", "sides": {"a": {"map": "{\n  \"customerId\": id,\n  \
   ```

### How to read the result

- **A version is its content.** The content hash is computed from what the file says, not from how it
  is written. A comment, a blank line or another order of lines changes nothing: step 2's first copy
  got the same id. Any change of meaning gives a new id.
- **The version number is yours to change.** Nothing forces it. Step 2's second copy was stored as a
  second `guide-customer 0.10.0` beside the first, and the list of step 3 shows both. In lists and on
  the Runs screen's "Spec" list the two differ only by their id and hash. Change `metadata.version`
  every time you change the file, as section 2 did.
- **`needs`** names what a second person would have to approve under single sign-on (section 1, "The
  warning of steps 14 and 17"). An empty list means nothing.
- **Ids skip numbers.** Here ten versions got ids from 3 to 27. Do not read anything into the gaps.
- **What comes back is not your file.** Step 4 returns the content as JSON: without your comments,
  with the members in another order, and, because the key's class is `identifier`, with the keys of
  the list as tokens. It tells you what a version says. It does not give you the file back.

**Keep your own copy.** Put your spec files under version control, in a repository of your own, and
store from there. The file with its comments is the source; the stored version is the record of what
ran. To find out which of your files a stored version is, compare content hashes: store the file again
and read the hash in the answer.

### Limits

- Nothing edits or deletes a stored version.
- No `ds` command shows a stored spec's content; the Specs screen does (a version's name). Under single
  sign-on the review of an approval request shows it too, by the pages' source; not verified here.
- An upload is validated like any spec: one whose `normalizerVersion` is not this build's is refused
  (section 3). Not verified here: what becomes of versions that an older build stored.
- Not verified here: single sign-on, under which the command line runs only a version that was
  uploaded on the Specs screen and approved.

### If something goes wrong

**`{"detail": "invalid spec", "issues": [{"pointer": "/sides/a/target", "message": "target 'customer-v1' is not in the registry", "code": "unknown_target", "severity": "error"}]}`**, with HTTP status 422
The upload has an error and nothing was stored. Read the issues as in section 3.

**`{"detail": "spec 999 not found"}`**
The number in the address of step 4 is not a stored spec's id.

**`{"detail": "not signed in"}`** or **`invalid or expired API token`**
The request carried no token, or a wrong one. See the [appendix](A1-install-by-hand.md) of chapter 1.

**Two rows with the same name and version**
Two different contents were stored under one version number (step 2). Neither can be removed. Give
the next change a new version, and choose versions by their id.

## 5. Draft a spec from sample answers

### What it is for

Writing the first version of a spec by hand means reading two unfamiliar answers side by side. The
profiler does the first pass for you. You give it the answers of both services for the same keys, and
it proposes a spec: which part of each answer holds the record, which fields correspond, how lists
should be matched, which clean-ups remove format-only differences, and a pattern for the key. The
proposal is a draft for a person to review, with a table of the evidence behind each suggestion.

### Before you start

Nothing has to be switched on. You need sample answers of both sides for the same keys, in one of two
forms:

- A folder with one sub-folder for each side: `a/<key>.json` and `b/<key>.json` hold the answers'
  bodies as served. Beside each, `<key>.meta.json` may give the status, for example `{"status": 404}`;
  without it the answer counts as status 200.
- One text file in JSON Lines form, one record on each line:
  `{"key": "...", "side": "a", "status": 200, "body": {...}}`. For the command line the file lies in a
  folder of its own and its name ends in `.jsonl`.

Include answers for records that one side does not hold, with their real status. A side whose status
is not between 200 and 299 counts as having no record for that key.

For the mock pair, the product writes the first form for you (step 1 below). The web UI takes only the
second form, or one JSON list of `{"key": ..., "a": ..., "b": ...}`, which the same tool writes for the
mock pair (step 8 below).

### In the web UI

Do steps 3, 8 and 9 of "On the command line" first. Step 3 makes the folder `p0-out` on your
machine, and the other two put the file `p0-out/customer-pairs.json` into it.

1. Click "Specs" in the bar at the top, and find the panel "Draft a spec from sample pairs".
2. Press its file chooser and pick `p0-out/customer-pairs.json`.
3. Fill the four fields, which are labelled with their internal names: `spec_name` with
   `guide-customer-draft`, `target_a` with `customer-a`, `target_b` with `customer-b`, and `key_name`
   with `customer_id`. The two target fields suggest the declared target ids.
4. Press "Draft". Four things appear:
   - The box of the panel "Upload a spec", above, now holds the draft, and below its buttons is an
     issues table with three rows: one warning with the code `inline_keys_stored`, and two errors with
     the code `truncation_undeclared`, for `/sides/a` and `/sides/b`.
   - Under the "Draft" button, a list of eleven notes. The third begins "request templates are
     placeholders".
   - Under the notes, two lines with the paths of each side that found no partner on the other:
     `unmatched[a]: $.email, $.status, $.prefs.mktEmail, $.prefs.mktSms, $.phones[*].number,
     $.consents[*].granted` and `unmatched[b]: $.lifecycleStatus, $.preferences.channels.email.marketing,
     $.preferences.channels.sms.marketing, $.contactPoints[*].channel, $.contactPoints[*].value,
     $.consents[*].status`.
   - Under those, the evidence table, with the columns `kind`, `path_a`, `path_b`, `decision`,
     `scores`, `sample_count` and `note`, and 142 rows for these samples.
5. Review and correct the draft in the box, as "How to read the result" says. Then "Validate" and
   "Save" work as in sections 3 and 4.

The form has no fields for the two requests and none for a key pattern. A draft made here therefore
has the path `/{{ key }}` on both sides, which you must replace with the real paths. The evidence table
cannot be downloaded.

### On the command line

1. Write the sample answers of the customer mock pair into the container: 40 keys, both sides. The
   mock's own module does it.
   ```bash
   docker compose run --rm app python -m mocks.fixtures customer --out /var/lib/deltasleuth/p0/customer-samples
   ```
   You should see:
   ```text
   wrote 80 samples for customer to /var/lib/deltasleuth/p0/customer-samples
   ```
2. Draft a spec from them. `--name` is the draft's spec name, `--target-a` and `--target-b` its two
   targets, `--key-name` the key's name. `--request-a` and `--request-b` are the two requests, method
   and path. `--targets` checks the draft against the targets file. `--out` is a folder for the result;
   name one directly under `/var/lib/deltasleuth/p0`.
   ```bash
   ds profile --samples /var/lib/deltasleuth/p0/customer-samples --out /var/lib/deltasleuth/p0/customer-profile --name guide-customer-draft --target-a customer-a --target-b customer-b --key-name customer_id --request-a 'GET /api/v1/customers/{{ key }}' --request-b 'GET /v2/customers/{{ key }}' --targets /etc/deltasleuth/targets.yaml
   ```
   You should see:
   ```text
   note: Side a selects $.customer; envelope members $.asOf are not compared.
   note: Side b selects $.data; envelope members $.meta are not compared.
   note: truncation is left out: declare sides.<side>.truncation or the target's truncation_indicators before a live run (loader rule X33).
   note: notFound [404] must be declared in each target's authoritative_absence (loader rule X2).
   note: unmatched[a] lists 6 side-a paths with no side-b counterpart: they surface as missing_in_b until mapped or excluded after review.
   note: unmatched[b] lists 6 side-b paths with no side-a counterpart: they are not compared.
   note: Mappings that need logic (a value lookup such as A -> ACTIVE, a scalar that side b keeps in a filtered array) are not inferred; unmatched paths show where to add them.
   note: Key stability (w4) was not scored: the samples hold no repeated reads of a key.
   note: key.source lists the 40 sampled keys; replace it with the run's key source for a full comparison.
   note: key.class is identifier (fail-closed: keys are tokenised in results); set business_code if keys are not personal data.
   warning: /key/source/keys [inline_keys_stored] inline keys of class identifier are stored in clear with the spec and never purged; pass them with 'ds run --keys'
   error: /sides/a [truncation_undeclared] neither this side nor target 'customer-a' declares truncation indicators (X33), so truncation would go undetected
   error: /sides/b [truncation_undeclared] neither this side nor target 'customer-b' declares truncation indicators (X33), so truncation would go undetected
   profiled 40 pair(s): /var/lib/deltasleuth/p0/customer-profile/draft.spec.yaml, /var/lib/deltasleuth/p0/customer-profile/profile_evidence.csv
   error: the draft does not validate; review it (see the issues above)
   ```
   The last line and the exit status 2 are expected here. The two files were written. The draft does
   not validate yet because the samples cannot tell the profiler where a service cuts a list short.
3. Make a folder on your machine for copies out of the container. Git ignores `p0-out`.
   ```bash
   mkdir -p p0-out
   ```
   You should see no output.
4. Copy the result out.
   ```bash
   docker compose cp app:/var/lib/deltasleuth/p0/customer-profile p0-out/
   ```
   You should see a `Copied` line. `p0-out/customer-profile` now holds `draft.spec.yaml` and
   `profile_evidence.csv`.
5. Put the draft into your working folder under a name of its own.
   ```bash
   cp p0-out/customer-profile/draft.spec.yaml my-specs/customer-draft.yaml
   ```
   You should see no output.
6. Make it valid. Open `my-specs/customer-draft.yaml` in a text editor. Under the line
   `    select: $.customer` add the first line below, and under the line `    select: $.data` the
   second. Each starts with four spaces.
   ```text
       truncation: {countPath: "$.customer.accountCount", itemsPath: "$.customer.accounts", cap: 50}
   ```
   ```text
       truncation: {countPath: "$.data.productCount", itemsPath: "$.data.products", cap: 100}
   ```
7. Copy the folder in, validate the draft, and compare one key with it.
   ```bash
   docker compose cp my-specs app:/var/lib/deltasleuth/p0/
   ```
   You should see the `Copied` line.
   ```bash
   ds spec validate /var/lib/deltasleuth/p0/my-specs/customer-draft.yaml --targets /etc/deltasleuth/targets.yaml
   ```
   You should see the warning about inline keys and `ok: customer-draft.yaml is valid (1 warning(s))`.
   ```bash
   ds compare C00012001 --spec /var/lib/deltasleuth/p0/my-specs/customer-draft.yaml
   ```
   You should see:
   ```text
   tok:identifier:v1:CQNWAWB5FGXEMVKM6Y5L3FMWV7: MISMATCH (DIFFERENT, rechecked)
     MEDIUM FIELD_MISSING missing_in_b /consents/tok:free_text:v1:3P6TLWW6SSGT7PN6VFGZDSVC6D/granted
         A: tok:free_text:v1:6GUY2V7UXOW4EQBM5GGUFKTZJU
         B: <absent>
     MEDIUM FIELD_MISSING missing_in_b /consents/tok:free_text:v1:OPN2DYAQQSXM55CA2HEMZMCHV7/granted
         A: tok:free_text:v1:6GUY2V7UXOW4EQBM5GGUFKTZJU
         B: <absent>
     MEDIUM FIELD_MISSING missing_in_b /email
         A: tok:free_text:v1:T2BJHEOOH3WL4K4N3Z4RJQRDZ5
         B: <absent>
     MEDIUM FIELD_MISSING missing_in_b /phones/tok:free_text:v1:Q6KFJFT5ZL5UMQOWAHNWDGHGX5/number
         A: tok:free_text:v1:ILCNBQ63BI72C7FELQG5PDZADT
         B: <absent>
     MEDIUM FIELD_EXTRA extra_in_b /phones/tok:free_text:v1:AOSA7CDMFXFRV4QYRU2UGTPUNT
         A: <absent>
         B: {}
     MEDIUM FIELD_MISSING missing_in_b /prefs/mktEmail
         A: tok:free_text:v1:6GUY2V7UXOW4EQBM5GGUFKTZJU
         B: <absent>
     MEDIUM FIELD_MISSING missing_in_b /prefs/mktSms
         A: tok:free_text:v1:FLYTKV4HXYXZOKYWAFPDBXL223
         B: <absent>
     MEDIUM FIELD_MISSING missing_in_b /status
         A: tok:free_text:v1:TDQPXFMOSA5TDCBNFTRNTTV23O
         B: <absent>
   run R-35T7ECVVDCB54806ETZ94902N2; observations a O-255cdff0adf8b332 b O-f83740cd1f0bab0a
   ```
   The draft runs, and its findings are its to-do list: each `missing_in_b` names a field of side a
   for which the profiler found no counterpart on side b.

Steps 8 and 9 are for the web UI's form only. They need step 3, for the folder `p0-out`.

8. Write the same samples as the one file the form takes, a JSON list with a line for each key. The
   mock's module does this too.
   ```bash
   docker compose run --rm app python -m mocks.fixtures customer --pairs /var/lib/deltasleuth/p0/customer-pairs.json
   ```
   You should see:
   ```text
   wrote 40 pairs for customer to /var/lib/deltasleuth/p0/customer-pairs.json
   ```
9. Copy the file out, into the folder `p0-out` of step 3.
   ```bash
   docker compose cp app:/var/lib/deltasleuth/p0/customer-pairs.json p0-out/customer-pairs.json
   ```
   You should see a `Copied` line. The file has 42 lines (`wc -l p0-out/customer-pairs.json`): the 40
   keys between a line `[` and a line `]`. The request that the "Draft" button sends was sent here with
   curl, for a JSON Lines file of the same samples, of which the button makes the same 40 pairs, and its
   answer is what "In the web UI", step 4, describes. The two `unmatched` lines came to the screen
   later: they are the draft's unmatched paths as the product computes them for this file, and were not
   seen on the screen.

### How to read the result

Open `p0-out/customer-profile/draft.spec.yaml` beside the `my-specs/customer.yaml` of section 2. The
draft starts with the notes of step 2 as comments, and comments above `compare` say why each list mode
and normaliser was proposed. For the mock pair the comparison comes out like this:

| Part | The draft | What a review changes |
|---|---|---|
| `select` | `$.customer` and `$.data`. | Right. |
| `map` | Side a has none and keeps its own names; side b's map renames to side a's names. It reaches over the whole profile: the addresses, the date of birth, the account count, and each account's product, status and currency are in it, all of which section 2 leaves out. | Six fields of each side found no partner: the e-mail address, the status, the phone number, the consent flag and two preferences. They need the map expressions of section 2, step 2. Leave out what you do not want compared. |
| `key` | Pattern `^[A-Z][0-9]{8}$`, class `identifier`, and all 40 sampled keys as an inline list. | Tighten the pattern if you know the rule. Replace the list (section 2, step 11). |
| `arrays` | Five rules: `$.phones` keyed by `kind`, `$.addresses` by `kind`, `$.accounts` by `sortCode`, `$.consents` by `purpose`, `$.tags` as `multiset`. | Key each list by what is unique in it: phones by their number, accounts by sort code and account number, as section 2 and the bundled spec do. |
| `fields` | `timestamp_utc` with precision `us` for `$.createdAt`, `upper` for the tags, `decimal` with scale 38 for the balance. | Choose the precision and scale the business means: seconds and two decimal places in section 2. |
| `notFound` | `status: [404]` on both sides. | Right for these targets. |
| `truncation` | Left out. | Add it (step 6), or declare it on the targets (section 6). |
| `recheck` | `delay: 5m`. | The draft's own comment calls it a conservative default to tune. On the mocks a batch run of the draft waited five minutes for its second reads; `1s` is enough there. |
| `security` | None, so everything is a token. | List personal fields with their class and the fields that may be shown (section 2, step 9). |

`profile_evidence.csv` has one row for each decision and each rejected alternative. Its columns are
`kind`, `path_a`, `path_b`, `decision`, `sample_count`, `scores` and `note`. Rows of the kind `select`
and `key_pattern` come first; rows of the kinds `array` and `key_candidate` are the ones to read when
you doubt a list's key.

[RUNBOOK.md](../mvp/RUNBOOK.md), "Reviewing the profiler draft", has the review as a checklist of seven
points.

### Limits

- The draft is a suggestion. A run of the unreviewed draft of this pair, with only `truncation` added,
  ended after 305 seconds with 39 `MISMATCH` and 1 `NOT_FOUND_B` out of 40.
- The profiler aligns fields that correspond one to one. It does not invent a lookup (`A` to
  `ACTIVE`), a test (`granted = "Y"`) or a pick from a list, and says so in its notes.
- It sees only the answers. Requests, truncation and the meaning of a 404 are yours to state.
- The web UI's form takes one file, not a folder; has no request or key pattern fields; and cannot
  save the evidence table.
- The inline key list of a draft holds the sampled keys as they are. Remove it before you store a
  draft made from real keys.

### If something goes wrong

**`error: the draft does not validate; review it (see the issues above)`**
Expected whenever `--targets` is given and the targets declare no truncation. The files were written.

**`error: sample root '/var/lib/deltasleuth/p0/no-such-folder' does not exist`**
`--samples` names a folder the container does not hold. Samples must be under
`/var/lib/deltasleuth/p0`: write them there (step 1) or copy them in.

**`error: Permission denied: /var/lib/deltasleuth/p0/my-specs/profile`**
`--out` names a place inside a folder you copied in, which `ds` may read but not write. Name a folder
directly under `/var/lib/deltasleuth/p0`.

**A run of the draft seems to hang**
It waits `recheck.delay`, five minutes in a draft, before its second reads: 305 seconds here for the 40
mock keys. Meanwhile the terminal shows only the `started` line and the line that begins `another
worker`. Lower the delay, or wait. `ds compare` with the same draft does not wait (section 2, "A run
takes minutes").

**`invalid output path: directory "<folder>/IntelliDiff/p0-out" does not exist`**
A `docker compose cp` out of the container names a folder that your machine does not have, and nothing
was copied. Make the folder with step 3, `mkdir -p p0-out`, then copy again. Provoked here with a
folder name that did not exist.

## 6. Measure what the endpoints do

### What it is for

A target's `capabilities` and a side's `truncation` state how a service behaves: what it answers for a
record it does not hold, whether it cuts long lists short, whether it pages. If you do not know, `ds
measure` reads the answers a run has stored and reports what it saw on each side: statuses, sizes,
response times, the shape of "no such record" answers, list lengths. It then proposes a `capabilities`
block for each target, with the evidence for every line, for a person to confirm with the service's
owners.

### Before you start

You need a finished run of the pair whose stored answers are still kept. To learn how a service says
"no such record", the run must include a few keys that you know do not exist. Nothing has to be
switched on.

### In the web UI

The web UI does not offer this. [RUNBOOK.md](../mvp/RUNBOOK.md) counts measuring among the tools that
only the command line has, and no screen or API route runs it.

### On the command line

1. Write two keys that cannot exist into a file. They match the spec's key pattern, and no customer
   has them.
   ```bash
   printf 'C00099901\nC00099902\n' > my-specs/missing-keys.txt
   ```
   You should see no output.
2. Make the key list of the run: the 40 real keys and those two.
   ```bash
   cat my-specs/customer-keys.txt my-specs/missing-keys.txt > my-specs/measure-keys.txt
   ```
   You should see no output.
3. Copy the folder in.
   ```bash
   docker compose cp my-specs app:/var/lib/deltasleuth/p0/
   ```
   You should see the `Copied` line.
4. Run the spec of section 2 with that list.
   ```bash
   ds run /var/lib/deltasleuth/p0/my-specs/customer.yaml --keys /var/lib/deltasleuth/p0/my-specs/measure-keys.txt
   ```
   You should see:
   ```text
   run R-35T7EDWA7NGQX3MCEKVSXYCNYR started
   warning: spec 27 (guide-customer 0.10.0) needs a four-eyes approval of clear, known_diffs; tokens mode has no approvers, so run R-35T7EDWA7NGQX3MCEKVSXYCNYR is labelled unapproved
   another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
   completed
     total 42, compared 40, match 36, different 4, indeterminate 2, cancelled 0, outstanding 0
     INCONCLUSIVE 2, KNOWN_DIFF 2, MATCH 35, MISMATCH 2, NOT_FOUND_B 1
   ```
   The two keys that do not exist are the two `INCONCLUSIVE`: a record that neither side holds is
   never called a match.
5. Measure the run. `<run id>` is the id in the first line of step 4, here
   `R-35T7EDWA7NGQX3MCEKVSXYCNYR`. `--out` is a folder for the result, directly under
   `/var/lib/deltasleuth/p0`.
   ```bash
   ds measure <run id> --out /var/lib/deltasleuth/p0/measure --missing-keys /var/lib/deltasleuth/p0/my-specs/missing-keys.txt
   ```
   You should see:
   ```text
   2 item(s) to review: /var/lib/deltasleuth/p0/measure/measure.json, /var/lib/deltasleuth/p0/measure/capabilities.proposed.yaml
   ```
6. Copy the result out.
   ```bash
   docker compose cp app:/var/lib/deltasleuth/p0/measure p0-out/
   ```
   You should see a `Copied` line.
7. Read the proposal.
   ```bash
   cat p0-out/measure/capabilities.proposed.yaml
   ```
   You should see this for `customer-a`, and the same shape for `customer-b`:
   ```text
   # Proposed TargetCapabilities, measured black-box by DeltaSleuth.
   # Evidence, not a declaration: review each field and merge it into targets.yaml by hand.
   targets:
     customer-a:
       capabilities:
         # evidence: 2 HTTP 404 response(s) with body keys {error} match declared shape(s) not_found_body
         authoritative_absence: {shapes: [not_found_body], status: [404]}
         # evidence: declared predicates that matched
         absence_shapes: {not_found_body: has(body.error) && body.error == "NOT_FOUND"}
         # evidence: $.customer.accountCount counts $.customer.accounts: 43 docs, equal 43, total > items 0, max items 3
         # evidence: 4 count/array pairing(s) discarded: the count was below the items returned
         # REVIEW: a count member tracks its array but never exceeded it: declare truncation_indicators with the documented cap (the cap cannot be measured from these samples)
         # truncation_indicators: not proposed
         # evidence: no pagination member or Link header in 45 responses
         pagination: none
         # evidence: ordering stability cannot be proven from single reads; declare true only if documented
         stable_ordering: false
   ```

### How to read the result

`capabilities.proposed.yaml` has the form of the targets file, so that a confirmed block can be copied
into `deploy/config/targets.yaml` by hand. Section 1, "A later change of the targets file alone", makes
it take effect. Nothing is merged for you.

- A line that starts with `# evidence:` says what was seen. Above, both 404 answers of `customer-a`
  had a body with one member, `error`, and matched the shape the target already declares.
- A line that starts with `# REVIEW:` is a question for a person. `2 item(s) to review` in step 5
  counts them: one for each target here. This one says that `accountCount` always equalled the number
  of accounts returned, so the samples cannot show the most the service returns. The cap must come
  from the service's documentation. Until a target declares it, each side states `truncation` itself,
  as section 2 does.
- `pagination: none` and `stable_ordering: false` are what the answers support, not a promise of the
  service.

`measure.json` holds the numbers behind the proposal, for each side: `statuses` (how many answers of
each status; for side a here, 43 with 200 and 2 with 404), `body_bytes` and `latency_ms` (the largest
value and the values that 50, 95 and 99 of 100 answers stay under), `not_found_shapes`,
`fanout_canonical` (how long each list of the canonical document gets) and `truncation_candidates`.

For these targets the proposal is the same with and without `--missing-keys`: it was run both ways
here, and the two files did not differ. The targets already declare their "no such record" shape, and
`ds measure` confirms a declared shape from any 404 that fits it. The option matters for a target that
declares none: [RUNBOOK.md](../mvp/RUNBOOK.md), "Measured capabilities", says a shape is then proposed
only from the answers to keys known not to exist. Not verified here: a target without a declared
shape.

### Limits

- Every proposal needs a person's confirmation against the service's documentation. A measurement of
  43 answers proves little about a service's limits.
- It reads stored answers. A run whose stored answers have been deleted cannot be measured.
- It measures what a run asked. Keys that exercise a limit, such as a customer with more accounts than
  the cap, must be in the run for the limit to show.
- The command line only: no screen, no API route.

### If something goes wrong

**`error: run R-00000000000000000000000000 not found`**
The run id is mistyped. `ds runs` lists the latest runs with their ids.

**`error: Permission denied: ...`** naming the `--out` folder
`--out` is inside a folder you copied in. Name a folder directly under `/var/lib/deltasleuth/p0`
(section 5 has the same message).

**`error: set DS_OPERATOR to your user name (docs/mvp/AUTH.md §4.2)`**
The alias of "Before you begin" is not defined in this terminal.

## What this chapter leaves behind

Everything the practice made stays until you take it away. None of it does harm: nothing of it runs by
itself, and the practice target points at a mock.

**Stored spec versions and runs.** Done exactly as written, the chapter leaves 20 stored spec versions:
twelve of `guide-customer`, two of `guide-tolerance-test`, and one each of `guide-profile-v1`,
`mock-customer-v1-vs-v2`, `mock-linker-legacy-vs-new`, `guide-listing`, `guide-exclude-test` and
`guide-customer-draft`. It also leaves 33 runs: 8 batch runs, and 25 runs of one key, one for each
`ds compare`. They fill the table of the Specs screen, the "Spec" list of "New run" and the Runs
screen. Nothing deletes a stored spec version (section 4). The only way to clear them is to remove the
installation ([chapter 1](01-install-docker.md), section 8), which deletes everything else with them.
[Chapter 12](12-administration.md), section 7, says how long the results of runs are kept.

**Files.** On your machine, the folders `my-specs` and `p0-out` stay in the installation's folder;
other chapters copy into `p0-out` too. In the container's folder `/var/lib/deltasleuth/p0` stay
`my-specs`, `public`, `customer-samples`, `customer-samples.jsonl`, `customer-profile` and `measure`.
Keep `my-specs/customer.yaml` as your own copy of the spec (section 4).

**The practice target.** Section 1 left three things: the entry `profile-v1` in
`deploy/config/targets.yaml`, the file `deploy/secrets/profile_v1_token`, and two edits of
`deploy/docker-compose.site.yml`. Keep them if you want to repeat section 1. To take them out again:

1. Open `deploy/config/targets.yaml` in a text editor and delete the lines that section 1, step 7,
   added: everything from the line that begins `  # A service that wants a token` to the end of the
   file.
2. Open `deploy/docker-compose.site.yml`. Change the line under `app:` back so that it starts with
   `    secrets: []`, keeping whatever follows on it. Then delete the two lines you added at the end,
   `secrets:` and `  profile_v1_token: {file: ./secrets/profile_v1_token}`.
3. Check the targets file.
   ```bash
   docker compose run --rm app ds licence
   ```
   You should see the four lines of section 1, step 8.
4. Apply the change. The app's container is made anew, without the token file.
   ```bash
   docker compose --profile mocks up -d
   ```
   You should see the lines of section 1, step 9, with `Recreated` and `Started` for the app.
5. Wait five seconds. Then ask for health and for the targets, with the commands of section 1, steps
   10 and 2. You should see `"status": "ok"`, and twelve ids again, without `profile-v1`.
6. Delete the token file, last of all. It is read-only, so `rm` needs `-f`.
   ```bash
   rm -f deploy/secrets/profile_v1_token
   ```
   You should see no output.

The target `profile-web` of section 1, "In the web UI", is removed where it was made: click "Targets",
then "Remove" in its row. Each "Save" on the page "New comparison" left a stored version of
`scratch-comparison`, which stays like every stored version.

The stored version `guide-profile-v1` stays in the lists, like every stored version, and can no longer
run. `ds run` with its file answers `error: /sides/a/target [unknown_target] target 'profile-v1' is not
in the registry`. The app answers the request that "Start" of "New run" sends for it with status 422
and `"detail": "invalid spec"`; not seen in a browser. Not verified here: these six steps in another
order.

## Reference

- [RUNBOOK.md](../mvp/RUNBOOK.md): the black-box principle, what to send, every command, reviewing the
  profiler draft, measured capabilities, the table of outcomes, and troubleshooting.
- [DEPLOY.md](../mvp/DEPLOY.md): section 7, "Point it at your endpoints", for targets of every kind and
  their secrets, and under "In the product" for the targets declared in the browser; section 9 for the
  folder that carries files in and out; section 11, "Targets and specs", for more messages.
- [CONTRACTS.md](../mvp/CONTRACTS.md): the stored tables, the API routes for specs, and what the Specs
  screen does.
- [APPROVALS.md](../mvp/APPROVALS.md): which parts of a spec need a second person under single sign-on.
- [RETENTION.md](../mvp/RETENTION.md): why keys written into a spec are never purged.
- [SAMPLING.md](../mvp/SAMPLING.md): key listings as the frame of a sample.
- [TABLES.md](../mvp/TABLES.md), [ENGINES.md](../mvp/ENGINES.md), [FILES.md](../mvp/FILES.md): sides
  that are database tables or files.
- In the source: `deltasleuth/core/spec/spec.schema.json` describes every member of a spec;
  `mocks/customer/spec.yaml`, `mocks/linker/spec.yaml` and the two folders under `examples/public` are
  complete specs with comments; `mocks/customer/expected.json` and `mocks/linker/expected.json` list
  what each key of the mock pairs should give.

Back to [chapter 3](03-first-comparison.md). Next: [chapter 5](05-runs.md), runs in depth.
