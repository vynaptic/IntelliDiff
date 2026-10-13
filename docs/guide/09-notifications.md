# 9. Alerts and notifications

DeltaSleuth can tell people that something happened without anyone looking at a screen. It sends a
short message, a notification, to a destination you declare: an HTTP address of your own, an e-mail
relay, Slack or Teams. This chapter switches that on, sends a first message to a small receiver on
your own machine, and shows what happens when the receiver is down.

At the end you have one destination, five rules that say which events go to it, and a receiver that
prints every message it gets. Nothing leaves the machine. Section 1 is a list to come back to; the
first command is in section 2.

**How this chapter was checked.** Every command was run on 2026-10-05, on macOS
with Docker Desktop, in a fresh installation made as [chapter 1](01-install-docker.md) says, in order
from section 2 to "Tidy up". The outputs printed are shortened, and ids, times, counts and audit row
numbers will differ on your machine. `<you>` stands for your user name. Not verified here: anything
in a browser. Every step under "In the web UI" is read from the page's source,
`web/src/Notifications.tsx`, and the requests behind signing in, "Send test", "Send failed again",
"Mute" and "End" were sent with curl instead. Not verified here: anything on Linux.

These words are used throughout:

- A **run** compares the keys of a comparison spec. A **cycle** is one pass over those keys: a batch
  run has one cycle, a soak run repeats them.
- The **worker** is the background part of the app that does the comparing. It also sends the
  notifications, and it runs inside the app container.
- The **audit log** is DeltaSleuth's record of who did what. Its rows are numbered. Every
  notification about a run is made from one audit row and names it.
- A **channel** is one destination you declared. A **rule** says which events go to which channels.

Run every command from the installation's folder, `IntelliDiff`, with the `ds` alias of
[chapter 1](01-install-docker.md), section 5, defined in your terminal. In a second installation on
the same machine, read the note at the start of "Before you start" in section 3 before you type the
receiver's commands: they name the installation themselves.

## 1. What DeltaSleuth can tell you about

### What it is for

A run can take hours and a schedule fires at night. An **event** is one kind of thing that happened
and that a person may need to know: a cycle ended with differences, a run paused, a schedule missed
its time. Operators route the events that need quick action to one channel, and the owners of the
compared systems route the findings to another.

These are the seventeen events a rule can name, as the code lists them
(`deltasleuth/core/notify.py`). Each message carries one of three severities: `alert`, `warning` or
`info`.

| Event | In plain words | Severity |
|---|---|---|
| `cycle.ended` | A cycle of a batch or soak run ended. The message counts the keys that differ, are indeterminate or were not read, and says what is new since the cycle before | `warning` when a key differs or is indeterminate, or a rule's condition was met; otherwise `info` |
| `run.finished` | A run ended without failing: completed, completed with issues, or cancelled | `warning` when completed with issues; otherwise `info` |
| `run.failed` | A run failed. The message names the type of the error, never its text | `alert` |
| `run.stopped` | A stop condition written in the spec ended the run early | `alert` for `maxErrorRate`, `maxInconclusiveRate`, `maxConsecutiveErrors` and `maxUnexplainedCritical`; otherwise `warning` |
| `run.paused` | DeltaSleuth itself paused a run and a person must act: a side refused its credential, the keyring lacks a key, the spec version lacks an approval it needs, the licence refused the next cycle, the sign-off sample of the cycle before did not pass, or a table the run reads changed | `alert` for the credential, the key and the changed table; otherwise `warning` |
| `schema.drift` | The schema guard, which watches the tables a run reads, recorded a change, or could not look | `alert` when it paused the run; otherwise `warning` |
| `schedule.missed` | A schedule's due time started no run, and the message says why; or the worker paused a schedule it could not read | `alert`; `warning` when the reason is a run of it still live, a missing approval, the licence or a sample |
| `sample.judged` | The sign-off sample of a cycle was judged | `info` when it passed; otherwise `warning` |
| `sample.draw_failed` | A sign-off sample could not be drawn, and the run is paused | `warning` |
| `accuracy.measured` | People finished labelling a set of results of a cycle. The message counts the items, never what was found | `info` |
| `approval.waiting` | A spec approval or a sign-off waits for one or more approvers | `info` |
| `approval.revoked` | An approval was revoked. The message counts the runs this paused or stopped | `alert` |
| `readiness.changed` | A pair's cut-over readiness changed: its state, or one of its gates | `alert` when the pair regressed or a gate that was met no longer is; `warning` when the state fell for another reason; otherwise `info` |
| `licence.state` | The licence changed state. This build needs none, so it records one such row, `open`, at its first start | `info` here; in a build that needs a licence `alert` when expired and `warning` when ending |
| `notify.dead` | A notification could not be delivered and was given up (section 5) | `alert` |
| `notify.skipped` | Audit rows were never turned into notifications: they were older than a day when the worker came back, or a row could not be read | `warning` |
| `notify.daily` | A summary, once a UTC day. Its use is as a heartbeat: when it stops arriving, the worker is down (section 5) | `info` |

An eighteenth event, `notify.test`, is sent only by a test (section 3) and cannot be named in a rule.

### Before you start

Nothing.

### In the web UI

The UI has no list of events. Once notifications are on, the "Rules" table of the Notifications screen
shows which events your rules route (section 4).

### On the command line

No command lists the events. A rule that names an unknown event is refused with the full list; the
refusal is quoted under "If something goes wrong" in section 2.

### How to read the result

- `alert`: something stopped or was lost, and a person should act now. Alerts are never held back by
  the hourly limit of section 2.
- `warning`: something was observed that someone should look at: keys that differ, a run that
  completed with issues.
- `info`: for the record.

Every message says that it reports what was *observed*, and ends with the words "not evidence and not
a sign-off".

### Limits

- An event is about a cycle, a run, a schedule, an approval, a pair or a channel, never about one key.
- A single key compared on its own (a run of mode `one_shot`) notifies only when a rule lists that
  mode.
- A pause that a person makes on the Run screen sends nothing, and no event says that a run was
  resumed.
- Thresholds are judged when a cycle ends, never in the middle of one.
- No event is sent while no worker runs. Section 5 shows how that looks and what to do about it.

### If something goes wrong

Nothing here can fail. A rule that fires less often than you expect is the subject of section 7.

## 2. Switch notifications on

### What it is for

Notifications are off until the settings file has a `notify` block. The block declares the channels
and the rules. The person who administers the host writes it once, and every later change to it takes
effect when the app restarts. No screen and no API call can add a destination: that is deliberate, so
that where messages go is changed only by someone who can change the installation's files.

### Before you start

You need the installation of chapter 1 and a destination. This chapter declares one channel, a
**webhook**: DeltaSleuth sends each message as an HTTP POST to an address, and signs it with a key so
that the receiver can check who sent it. The address used here, `http://127.0.0.1:18421/hook`, is a
receiver that section 3 starts on your own machine. Slack, Teams and e-mail are the other three kinds
of channel. This chapter needs none of them; "How to read the result" below shows how each is
declared.

### In the web UI

The UI cannot switch notifications on, and cannot create or change a channel or a rule. The contract
says why: "Channels and rules live only in settings (config change control, applied at restart) ...;
no API creates a destination." Until the block exists the bar has no "Notifications" link, and the
address `http://localhost:8000/#/notifications`, typed by hand, shows the one line "Notifications are
off: add notify to deltasleuth.yaml and restart."

### On the command line

1. See that notifications are off.
   ```bash
   ds notify
   ```
   You should see:
   ```text
    Container deltasleuth-db-1  Running
   notifications are off: add notify to deltasleuth.yaml and restart (docs/mvp/ALERTS.md §8)
   ```
2. Create the signing key. The key goes from `openssl` straight into its file and is never shown;
   `umask 077` creates the file readable by you alone.
   ```bash
   (umask 077; { printf 'whsec_'; openssl rand -base64 32; } > deploy/secrets/notify_hook_key)
   ```
   You should see nothing.
3. Let the containers read the file, as chapter 1's four secrets are.
   ```bash
   chmod 444 deploy/secrets/notify_hook_key
   ```
4. Check that the file holds one key, without printing it.
   ```bash
   grep -c '^whsec_' deploy/secrets/notify_hook_key
   ```
   You should see:
   ```text
   1
   ```
5. Hand the file to the app container. Open `deploy/docker-compose.site.yml` in a text editor. Under
   `app:` there is a line that starts with `    secrets: []`. Write the secret's name between the
   brackets and leave the rest of the line as it is:
   ```text
       secrets: [notify_hook_key]
   ```
6. Tell Compose where that secret is, by adding two lines at the end of the same file. Run this once.
   If the file already has a line `secrets:` at its left edge that does not start with `#`, add only
   the second line under it, in the editor.
```bash
cat >> deploy/docker-compose.site.yml <<'EOF'
secrets:
  notify_hook_key: {file: ./secrets/notify_hook_key}
EOF
```
7. Check that Compose reads both changes. This command starts nothing.
   ```bash
   docker compose config | grep 'source: notify_hook_key'
   ```
   You should see:
   ```text
         - source: notify_hook_key
   ```
8. Add the `notify` block at the end of the settings file. Run this once.
```bash
cat >> deploy/config/deltasleuth.yaml <<'EOF'

notify:
  label: trial
  channels:
    local-hook: {kind: webhook, url: "http://127.0.0.1:18421/hook",
                 signing_key_refs: [file:/run/secrets/notify_hook_key], allow_private_network: true}
  rules:
    - {name: new-gaps, events: [cycle.ended], channels: [local-hook], when: {new_root_causes_at_least: 1}}
    - {name: mismatch-over-1pc, events: [cycle.ended], channels: [local-hook], when: {mismatch_rate_above: "0.01"}}
    - {name: low-coverage, events: [cycle.ended], channels: [local-hook], when: {coverage_below: "0.99"}}
    - {name: run-health, events: [run.paused, run.stopped, run.failed, schedule.missed], channels: [local-hook]}
EOF
```
   "How to read the result" below explains each line.
9. Check the block before it takes effect. `ds notify check` reads each secret and looks up each
   destination's address as a send would, and sends nothing.
   ```bash
   ds notify check
   ```
   You should see one line for the channel:
   ```text
    Container deltasleuth-db-1  Running
   ok: channel local-hook (webhook, 127.0.0.1): resolves to 127.0.0.1 (private: allowed); 1 signing key readable
   ```
10. Apply both files. The site file changed, so `restart` is not enough: this command makes the app
    container anew, with the secret, and the new app reads the new settings.
    ```bash
    docker compose --profile mocks up -d
    ```
    You should see the app made anew and the other two left alone:
    ```text
     Container deltasleuth-db-1  Running
     Container deltasleuth-mocks-1  Running
     Container deltasleuth-app-1  Recreate
     Container deltasleuth-app-1  Recreated
     Container deltasleuth-db-1  Waiting
     Container deltasleuth-db-1  Healthy
     Container deltasleuth-app-1  Starting
     Container deltasleuth-app-1  Started
    ```
11. Wait five seconds, then check that the app came back.
    ```bash
    curl -fsS http://localhost:8000/v1/health
    ```
    You should see:
    ```text
    {"status": "ok", "ai": false, "auth": "tokens"}
    ```
12. Look at the notifications' state.
    ```bash
    ds notify
    ```
    You should see the label, the channel and the four rules. The two numbers of the first line are
    10 in an installation that was only installed, and higher in one that was used:
    ```text
     Container deltasleuth-db-1  Running
    label trial: audit row 10 of 10 notified
    channel local-hook (webhook, 127.0.0.1): 0 pending; in 24 h 0 sent, 0 dead, 0 held back
    rule new-gaps: cycle.ended to local-hook
    rule mismatch-over-1pc: cycle.ended to local-hook
    rule low-coverage: cycle.ended to local-hook
    rule run-health: run.paused, run.stopped, run.failed, schedule.missed to local-hook
    ```

A later change to the `notify` block alone, with the site file untouched, needs only the check of
step 9 and then `docker compose restart app`. Section 5 does that once.

### How to read the result

**The block.** `label` names this installation at the start of every message, here `[trial]`: 1 to
32 lower-case letters, digits and hyphens. `channels` maps a name of your choice to a destination.
`rules` is a list, and each rule has these members:

| Member | Meaning |
|---|---|
| `name` | A name of your choice, lower-case with hyphens, used once |
| `events` | The events the rule takes, from the table of section 1 |
| `channels` | One to five declared channels |
| `specs` | Optional. Patterns such as `"cards-*"` for the names of the specs it takes. Without it: every spec. Not allowed beside `licence.state`, `notify.dead`, `notify.skipped` and `notify.daily`, which concern no spec |
| `modes` | Optional. The run modes it takes: `batch` and `soak` unless you write another list. `one_shot` only when named |
| `when` | Optional, for `cycle.ended` only: conditions on the cycle, any one of which makes the rule fire. Without it the rule fires for every cycle |
| `reasons` | Optional, for `run.paused`, `run.stopped` and `run.finished`: only these reasons, for example `stop_condition:maxErrorRate` |
| `states` | Optional, for `licence.state` and `readiness.changed`: only these new states |
| `statuses` | Optional, for `sample.judged`: only `passed`, `failed`, `incomplete` or `unjudged` |
| `causes`, `programmes`, `gates` | For `readiness.changed` only; `causes` is required there |

The conditions of `when` are of two sorts. A **change** condition holds in every cycle in which its
count reaches the number: `new_root_causes_at_least` (root causes not seen in an earlier cycle of the
run) and `newly_discrepant_at_least` (keys that are discrepant now and were not in the cycle before;
section 3 says which keys count as discrepant). A **level** condition fires when it is *crossed*,
which means it holds in this cycle and did not in the one before, and again when it *recovers*, and
never while it stays: `mismatch_rate_above`, `indeterminate_rate_above`, `coverage_below` and
`unexplained_critical_at_least`. A rate is written in quotes, as a number from 0 to 1. A run's first
cycle has no cycle before it, so everything in it counts as new, and a level that holds in it counts
as crossed.

So the four rules above say: tell `local-hook` when a cycle shows a root cause for the first time,
when more than 1% of the compared keys differ, when fewer than 99% of the keys could be compared, and
when a run pauses, stops early or fails or a schedule misses its time.

**The four kinds of channel.** The line `local-hook: {kind: webhook, ...}` of step 8 declares a
channel of the first kind. Each kind has its address settings and one secret, and every secret is a
file that the settings name with `file:`:

| Kind | Address settings | The secret, as a file |
|---|---|---|
| `webhook` | `url`: an `https` address without a user name, a query or a fragment. Plain `http` is accepted only for `localhost`, `127.0.0.1` and `[::1]`. An address that itself holds a secret goes into a file instead, with `url_ref` and `host` as for Slack | `signing_key_refs`: one or two files, each holding `whsec_` followed by the base64 form of 24 to 64 random bytes |
| `slack` | `host`: the host name of your Slack webhook address | `url_ref`: a file holding that whole address, because the address itself is the secret |
| `teams` | `host`: the host name of your Teams workflow address | `url_ref`, as for Slack |
| `email` | `smtp_host`, `sender`, `recipients` (1 to 20 addresses); optional `smtp_port` and `tls` (`starttls`, the default, on port 587; `implicit` on 465; `none` on 25, for a private relay only) | `password_ref`, together with `username` |

Any kind takes `allow_private_network: true`, which a destination at a private address needs (the
receiver of this chapter is one), and the webhook, Slack and Teams kinds take `proxy: true`, which
sends through the HTTP proxy named in `notify.proxy`. Two more members say what a message may carry:
`labels: false` leaves the spec's name out, and `links: false` leaves out the link to the screen.
Both are on for a webhook and for e-mail and off for Slack and Teams unless you write otherwise.

A Slack, a Teams and an e-mail channel are declared like this. Each is one more entry under
`channels:`, written under the two lines of `local-hook` with the same four spaces in front, and a
rule sends to it once its name stands in the rule's `channels`. `<host>` is the host name of the
address in the file, `<relay>` your mail relay, `<account>` its sign-in name and each `<address>` an
e-mail address. Each file is made, and handed to the container, as steps 2 to 6 do for the signing
key.

```yaml
    team-chat:   {kind: slack, url_ref: file:/run/secrets/notify_slack_url, host: <host>}
    team-teams:  {kind: teams, url_ref: file:/run/secrets/notify_teams_url, host: <host>}
    owners-mail: {kind: email, smtp_host: <relay>, username: <account>,
                  password_ref: file:/run/secrets/notify_smtp_password,
                  sender: <address>, recipients: [<address>]}
```

Not verified here: a delivery to Slack, to Teams or to a mail relay. These three lines were loaded
and checked with `ds notify check` in a copy of the settings file, with the mocks container as a
stand-in for `<host>` and `<relay>`, stand-in files for the three secrets and
`allow_private_network: true` on each, which that stand-in needs. The check answered `ok:` for each
kind, ending `the URL file names the host` for Slack and for Teams and `the password readable` for
e-mail.

**The check.** An `ok:` line names the channel, its kind and its host, the address the host has now,
`(private: allowed)` when that address is not a public one, and what was read: `1 signing key
readable`, `the URL file names the host` or `the password readable`. A line that starts `error:`
names what a send would fail on, and the command then ends with status 2.

**The state.** `audit row 10 of 10 notified` says how far the worker has read the audit log: it has
turned every row up to number 10 into notifications where a rule asked for one, and row 10 is the
newest. Both numbers grow with everything DeltaSleuth records, a sign-in at the web pages included,
so yours will soon be higher. The first number catches up with the second a few seconds after a row
is written; when the two are equal, nothing waits to be read. The `channel` line counts what waits
(`pending`) and, for the last 24 hours, what was delivered (`sent`), given up (`dead`) and
deliberately not sent (`held back`).

**Against noise.** These settings go beside `label`, each with a default:

| Setting | Default | Meaning |
|---|---|---|
| `rate_per_hour` | 30 | Per channel. Once a channel got this many in an hour, further messages are held back, alerts excepted (1 to 600) |
| `dedup_hours` | 6 | A message about the same thing as one already sent or waiting on that channel is held back for this long (0 to 168) |
| `max_attempts` | 8 | Attempts before a message is given up (2 to 20) |
| `timeout_seconds` | 10 | The longest one attempt may take (1 to 60) |
| `max_age_hours` | 24 | Audit rows older than this are never notified, and a message that waited this long is given up (1 to 72) |
| `daily_hour` | 7 | The UTC hour from which the daily summary is sent (0 to 23) |

A message that was held back is stored and never sent. The next message that does go to that channel
ends with a count of them. There are no quiet hours: a setting of that name is refused. To silence a
channel or a run for some hours, mute it (section 6).

### Limits

- At most 20 channels and 50 rules.
- The app reads the block only when it starts. `ds notify` and `ds notify check`, which start a
  container of their own, read the file as it is now: they show a change before the running app has
  it.
- The secrets are different: they are read at every send. Here a new key written into
  `notify_hook_key` was used by the next attempt without a restart.
- A webhook can hold two signing keys and then signs with both, so that a receiver can move from one
  key to the next. Not verified here.
- The proxy takes no user name or password.

### If something goes wrong

Each line below is what `ds notify check` printed, with status 2. The lines that start `error:
invalid settings` matter most: an app restarted with such a mistake logs the same line, stops, and is
started again by Docker in a loop, as chapter 1's appendix describes under "Starting by hand". That
was tried here with a webhook without its key: `docker compose logs --tail 3 app` repeated the line,
and curl answered `curl: (52) Empty reply from server` until the file was corrected and the app
restarted. So run the check before every restart. Most of these lines were provoked with copies of
the settings file, so the path in them was the copy's, shortened to `...` below; yours reads
`/etc/deltasleuth/deltasleuth.yaml`.

**`error: channel local-hook: cannot read secret file /run/secrets/notify_hook_key: No such file or
directory`**
The settings name the secret, but the site file does not hand it to the container. Do steps 5 and 6.

**`error: channel local-hook: the signing key in /run/secrets/notify_hook_key is not whsec_ and base64
of 24 to 64 bytes`**
The file does not hold a key as step 2 makes it. Make it again: run `chmod u+w
deploy/secrets/notify_hook_key`, then steps 2 and 3. Give the new key to whoever runs the receiver.

**`error: invalid settings ...: notify.channels.local-hook.url: must be https://host[:port]/path
without userinfo, query or fragment (http only for localhost, 127.0.0.1 or [::1]); a URL carrying a
secret goes in url_ref with host`**
The address is plain `http` to another name than those three, here `host.docker.internal`.

**`error: invalid settings ...: notify.channels.local-hook: Value error, 127.0.0.1 is a private
address: set allow_private_network to allow it`**
Add `allow_private_network: true` to the channel.

**`error: invalid settings ...: notify.channels.local-hook: Value error, kind webhook needs
signing_key_refs (one or two file: references)`**
A webhook is always signed.

**`error: invalid settings ...: notify.channels.team-chat: Value error, url is only for webhook`**, or
**`... Value error, kind slack needs url_ref and host (its webhook URL is a secret)`**
A Slack or Teams address goes into a file, named with `url_ref`, and its host name into `host`.

**`error: invalid settings ...: notify: Value error, rules.0.channels: unknown channel local-hok`**
A rule names a channel that is not declared. The number counts the rules from 0.

**`error: invalid settings ...: notify.rules.0.events.0: Input should be 'cycle.ended',
'sample.judged', 'run.finished', 'run.failed', 'run.stopped', 'run.paused', 'sample.draw_failed',
'accuracy.measured', 'approval.waiting', 'approval.revoked', 'licence.state', 'readiness.changed',
'schedule.missed', 'schema.drift', 'notify.dead', 'notify.skipped' or 'notify.daily'`**
A rule names an event that does not exist, here `cycle.end`. **`... notify.test is sent only by a
test`** is the same for that one name.

**`error: invalid settings ...: notify.rules.0: Value error, when is only for cycle.ended`**
The rule has `when` and names another event. Give that event a rule of its own. Likewise **`...
Value error, licence.state, notify.dead, notify.skipped and notify.daily match no spec`** for `specs`
beside one of those four, and **`... Value error, readiness.changed needs causes (baseline and config
are never implied)`**.

**`error: invalid settings ...: notify.label: String should match pattern
'^[a-z0-9][a-z0-9-]{0,31}$'`**
The label has a capital letter or a space.

**`error: invalid settings ...: notify.timeout_seconds: Input should be less than or equal to 60`**
A number is outside its range. **`... notify.quiet_hours: Unexpected keyword argument`** is a setting
that does not exist.

**`error: channel local-hook: host hooks.invalid does not resolve`**, or **`error: channel
local-hook: mocks resolves to <address>, a private address: set allow_private_network to allow it`**
The settings load, and the destination's name has no address, or has a private one. `<address>` is
the address the name has at that moment. Here the name was `mocks`, the container of the practice
services, so the address was one of the installation's own network, which is `172.30.0.0/24` unless
you chose another. Section 8 lists what can never be a destination.

**`error: notifications are off: add notify to deltasleuth.yaml and restart (docs/mvp/ALERTS.md
§8)`**
Every `ds notify` command but `ds notify` itself and `ds notify deliveries` answers this, with status
2, while the settings have no `notify` block.

## 3. A first notification, end to end

### What it is for

Before you rely on a channel you want to see a message arrive on it. A **test** sends one message now
and tells you at once whether the destination took it. A real event then shows what a message about a
run looks like. Operators do this after every change to a channel.

### Before you start

Section 2 is done. You also need something that listens at `http://127.0.0.1:18421/hook`.

DeltaSleuth accepts a plain `http` address only on the loopback address, 127.0.0.1, and the app runs
in a container, whose 127.0.0.1 is the container's own. So the receiver has to run inside the app
container's network. The five steps below start a second container that shares that network. It runs
a small Python program from the image you already built, and publishes nothing on your machine.

**In a second installation, change three names.** The receiver is started with plain `docker`
commands. Unlike `docker compose`, they do not read the project name from the folder: they name the
installation themselves, in three places. These are the receiver's own name, `deltasleuth-hook`, the
app container, `deltasleuth-app-1`, and the image, `deltasleuth:source`. If your installation has
another project name (chapter 1, "A second installation on the same machine"), write it in place of
`deltasleuth` in all three, wherever this chapter prints them: in this section, in sections 5 and 6
and in "Tidy up". With the project `ds2`, for example, they are `ds2-hook`, `ds2-app-1` and
`ds2:source`. The address `http://127.0.0.1:18421/hook` stays as it is in every installation. Typed
as printed in a second installation, the commands would act on the first installation's app container
and image, and your tests would find no receiver. Not verified here.

1. Check which project this folder acts on.
   ```bash
   docker compose config | grep '^name:'
   ```
   You should see the name to use in the three places. In a default installation:
   ```text
   name: deltasleuth
   ```
2. Create a folder for the program. Git ignores everything under `.deltasleuth`.
   ```bash
   mkdir -p .deltasleuth/receiver
   ```
3. Write the program. It listens on port 18421, checks the signature of each message against the key
   file, prints what arrived, and answers 200, or 401 when the signature is wrong.
```bash
cat > .deltasleuth/receiver/receiver.py <<'EOF'
"""A local webhook receiver for trying notifications: it checks each signature and prints what arrived."""
import base64, hashlib, hmac, json, os
from http.server import BaseHTTPRequestHandler, HTTPServer

KEY_FILE = "/run/secrets/notify_hook_key"
ANSWER = int(os.environ.get("ANSWER", "200"))  # the status a request with a good signature gets


def good(headers, body):
    """Standard Webhooks: HMAC-SHA256 of "<id>.<timestamp>.<body>" under the key's decoded bytes."""
    try:
        with open(KEY_FILE) as handle:
            key = base64.b64decode(handle.read().strip().removeprefix("whsec_"))
    except OSError:
        return False
    signed = f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    given = [s.removeprefix("v1,") for s in (headers["webhook-signature"] or "").split()]
    return any(hmac.compare_digest(g, expected) for g in given)


class Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"] or 0))
        signed = good(self.headers, body)
        status = ANSWER if signed else 401
        print(f"--- POST {self.path} from {self.headers['User-Agent']}")
        print(f"    webhook-id {self.headers['webhook-id']}  webhook-timestamp {self.headers['webhook-timestamp']}")
        print(f"    signature {'good' if signed else 'REFUSED'}, answered {status}")
        try:
            print(json.dumps(json.loads(body), indent=2), flush=True)
        except ValueError:
            print(body.decode(errors="replace"), flush=True)
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


print(f"listening on 127.0.0.1:18421, answering {ANSWER}", flush=True)
HTTPServer(("127.0.0.1", 18421), Hook).serve_forever()
EOF
```
4. Start it. `--name deltasleuth-hook` names the new container; `--network
   container:deltasleuth-app-1` puts it in the app container's network; the two `-v` options show it
   the program and the key, read-only; `--no-healthcheck` switches off the image's own check, which
   is meant for the app; `deltasleuth:source` is the image. These are the three names of the note
   above.
   ```bash
   docker run -d --name deltasleuth-hook --no-healthcheck --network container:deltasleuth-app-1 -v "$PWD/.deltasleuth/receiver:/receiver:ro" -v "$PWD/deploy/secrets/notify_hook_key:/run/secrets/notify_hook_key:ro" deltasleuth:source python /receiver/receiver.py
   ```
   You should see the new container's id, 64 characters.
5. Read what it printed. This is the command that shows every message from now on.
   ```bash
   docker logs deltasleuth-hook
   ```
   You should see:
   ```text
   listening on 127.0.0.1:18421, answering 200
   ```

The receiver lives in the app container's network, so it loses that network whenever the app
container restarts. After every `docker compose restart app`, run `docker restart deltasleuth-hook`.
After the app container was made anew (`Recreated` in the output of `docker compose up -d`), remove
the receiver with `docker rm -f deltasleuth-hook` and run step 4 again.

### In the web UI

1. Open `http://localhost:8000` and sign in as chapter 1, section 4, says. If you were signed in
   before step 10 of section 2, reload the page. The bar now has one more link, "Notifications",
   after "Licence".
2. Click "Notifications". A page headed "Notifications" opens. Under the heading stands a grey line,
   `label trial: audit row N of N notified`, then the headings "Channels", "Rules", "Mutes" and
   "Deliveries". N is the number of the newest audit row, and the two numbers are equal when nothing
   waits to be read. Here the line read `11 of 11`, one more than at the end of section 2,
   because signing in wrote an audit row of its own. The "Channels" table has one row, `local-hook`,
   and the row ends with two buttons, "Send test" and "Send failed again".
3. Click "Send test". A line appears above the grey one: `sent to local-hook: HTTP 200 in 2 ms
   (N-...)`, where `N-...` is the message's id. In the row, "Sent 24 h" becomes 1 and "Last" reads
   `ok 200`. The grey line can now read `11 of 12` until you reload the page: the test wrote an audit
   row, and the page read the state before the worker had passed that row.
4. Now cause a real event. Click "Specs". In the panel "Upload a spec", press the file button and
   choose `mocks/customer/spec.yaml` from the installation's folder. Its text appears in the box
   below. Click "Save". Beside the button appears `Saved as spec #1.`
5. Click "Runs". In the panel "New run", choose `mock-customer-v1-vs-v2 1.0.0 (#1)` under "Spec",
   leave "Mode" at `batch` and the box "Keys, one per line ..." empty, and click "Start". The page of
   the new run opens.
6. After ten seconds click "Notifications" again. The first row of the "Deliveries" table reads:
   "Event" `cycle.ended`, "Severity" `warning`, "State" `sent`, "Attempts" 1, "Last" `200`, and a
   number under "Audit row". Under "Body", click `SHA-256 ...` to open the message exactly as it was
   sent.

The requests behind these steps were sent with curl here: the sign-in, "Send test", the upload
and the start of the run. "Send test" answered `{"delivery_id": "N-35T7E16HBHFM3RJYH840PKRKNC",
"state": "sent", "status": 200, "error": null, "latency_ms": 2}`, from which the page makes the line
of step 3, and the state read straight after it said audit row 11 of 12.

### On the command line

1. Send a test. This one command is not typed with the `ds` alias. The alias starts a new container
   for each command, and a new container has a 127.0.0.1 of its own, where no receiver listens.
   `docker compose exec` runs `ds` inside the app container instead. Not verified here: for a
   destination that every container can reach, `ds notify test <channel>` with the alias.
   ```bash
   docker compose exec -e DS_OPERATOR="$(id -un)" app ds notify test local-hook
   ```
   You should see a copy of the audit row, then the result:
   ```text
   {"id": 18, ... "action": "notify.test", "object": "notify:local-hook", "detail": {"error": null, "state": "sent", "status": 200, ...}, ...}
   sent to local-hook: HTTP 200 in 2 ms (N-35T7E2GV72B148TGMGB841M5R4)
   ```
2. Read what the receiver got.
   ```bash
   docker logs --tail 29 deltasleuth-hook
   ```
   You should see the message; each `...` stands for lines left out here:
   ```text
   --- POST /hook from deltasleuth-notify/0.1.0.dev0
       webhook-id N-35T7E2GV72B148TGMGB841M5R4  webhook-timestamp 1791232259
       signature good, answered 200
   {
     "format": "deltasleuth-notify/1",
     "id": "N-35T7E2GV72B148TGMGB841M5R4",
     "event": "notify.test",
     ...
     "title": "a test of channel local-hook",
     "text": "[trial] This is a test of channel local-hook: the channel delivers. Observed by DeltaSleuth (installation I-05110f6fb3b83fe8); not evidence and not a sign-off."
   }
   ```
3. Now cause a real event: a run on the bundled mock pair. First store its spec. The API token goes
   from its file through a pipe to curl, as in chapter 1's appendix, and is never shown.
   ```bash
   { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - -H 'Content-Type: application/yaml' --data-binary @mocks/customer/spec.yaml http://localhost:8000/v1/specs
   ```
   You should see the spec's id, 1 in a fresh installation. A spec that is stored already is not
   stored twice: the same command, run again, answered the same line.
   ```text
   {"spec_id": 1, "name": "mock-customer-v1-vs-v2", "version": "1.0.0", "content_hash": "dadc627b...", "needs": ["clear", "known_diffs"], "approval_id": null}
   ```
4. Start a batch run of it. If step 3 answered another `spec_id`, write that number in place of 1.
   The later sections use this command again, each time for a new run.
   ```bash
   { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - -H 'Content-Type: application/json' -d '{"spec_id": 1, "mode": "batch"}' http://localhost:8000/v1/runs
   ```
   You should see the id of the run:
   ```text
   {"run_id": "R-35T7E2H9G29PD5YTBS9FZ3CWXK", "warnings": []}
   ```
5. Wait ten seconds, then list the notifications, newest first.
   ```bash
   ds notify deliveries
   ```
   You should see the run's message as the first line and the test of step 1 under it. The lines
   below those two, left out here, are the run and the test sent from the page, if you followed "In
   the web UI":
   ```text
    Container deltasleuth-db-1  Running
   N-35T7E2WJF32QQRN63MS85KEF82  2026-10-05T20:31:05+00:00  local-hook  cycle.ended  warning  sent  1 attempt(s)  audit row 21  run R-35T7E2H9G29PD5YTBS9FZ3CWXK  last ok (200)
   N-35T7E2GV72B148TGMGB841M5R4  2026-10-05T20:30:59+00:00  local-hook  notify.test  info  sent  1 attempt(s)  last ok (200)
   ...
   ```
6. Read the message the receiver got.
   ```bash
   docker logs --tail 80 deltasleuth-hook
   ```
   You should see, in full:
   ```text
   --- POST /hook from deltasleuth-notify/0.1.0.dev0
       webhook-id N-35T7E2WJF32QQRN63MS85KEF82  webhook-timestamp 1791232265
       signature good, answered 200
   {
     "format": "deltasleuth-notify/1",
     "id": "N-35T7E2WJF32QQRN63MS85KEF82",
     "event": "cycle.ended",
     "event_id": "seq:21:cycle.ended",
     "severity": "warning",
     "occurred_at": "2026-10-05T20:31:04Z",
     "installation": {
       "id": "I-05110f6fb3b83fe8",
       "label": "trial"
     },
     "audit": {
       "seq": 21,
       "hash": "17d44338754dcb8c676d3ba0ce37b061a0452736b17e2de7a86f3fb5aab34731"
     },
     "rules": [
       "new-gaps",
       "mismatch-over-1pc"
     ],
     "crossed": [
       "mismatch_rate_above"
     ],
     "recovered": [],
     "run": {
       "run_id": "R-35T7E2H9G29PD5YTBS9FZ3CWXK",
       "spec_id": 1,
       "spec_name": "mock-customer-v1-vs-v2",
       "spec_version": "1.0.0",
       "mode": "batch",
       "now": {
         "cycle": 1,
         "status": "completed",
         "licence": null,
         "approval": "unapproved"
       }
     },
     "facts": {
       "cycle": 1,
       "counters": {
         "total": 40,
         "compared": 40,
         "match": 36,
         "different": 4,
         "indeterminate": 0,
         "cancelled": 0,
         "outstanding": 0,
         "mismatch_rate": "0.1",
         "coverage": "1",
         "absent_both": 0
       },
       "levels": {
         "mismatch_rate": "0.1",
         "indeterminate_rate": "0",
         "coverage": "1",
         "unexplained_critical": 2
       },
       "previous": {
         "mismatch_rate": null,
         "indeterminate_rate": null,
         "coverage": null,
         "unexplained_critical": null
       },
       "new_root_causes": 4,
       "gone_root_causes": 0,
       "baseline_from": null,
       "newly_discrepant": 3,
       "no_longer_discrepant": 0,
       "between_samples": false,
       "unavailable": []
     },
     "suppressed_since_last": 0,
     "withheld": {},
     "claim": "observed",
     "link": "http://localhost:8000/#/runs/R-35T7E2H9G29PD5YTBS9FZ3CWXK?cycle=1",
     "title": "mock-customer-v1-vs-v2: cycle 1 of run R-35T7E2H9G29PD5YTBS9FZ3CWXK ended",
     "text": "[trial] mock-customer-v1-vs-v2: cycle 1 of run R-35T7E2H9G29PD5YTBS9FZ3CWXK ended: 4 of 40 keys different, 0 indeterminate, 0 not read (observed). 4 root causes and 3 discrepant keys observed in the first cycle. Mismatch rate 0.1 (not held in an earlier cycle; above 0.01). Observed by DeltaSleuth (installation I-05110f6fb3b83fe8); not evidence and not a sign-off. Audit row 21. Not sign-off evidence."
   }
   ```

### How to read the result

**The request.** DeltaSleuth sends a POST with a JSON body and these headers. The receiver above
prints the first three.

| Header | Meaning |
|---|---|
| `User-Agent` | `deltasleuth-notify/` and the product's version |
| `webhook-id` | The message's id. It stays the same when the message is sent again, so a receiver that keeps the ids it has seen can drop a repeat |
| `webhook-timestamp` | When this attempt was made, in seconds since 1970. A careful receiver refuses one that is more than five minutes old |
| `webhook-signature` | `v1,` and a signature, one for each signing key. It is the HMAC-SHA256, under the key's decoded bytes, of the id, a dot, the timestamp, a dot and the body, in base64. The function `good` of the receiver checks it |
| `Content-Type` | `application/json` |

**The body.** Every message has the same members.

| Member | Meaning |
|---|---|
| `format` | The name and version of this layout, `deltasleuth-notify/1` |
| `id` | The message's id, the same as `webhook-id` |
| `event` | The event, from the table of section 1 |
| `event_id` | Where it came from: `seq:21:cycle.ended` is audit row 21; a test has `test:` and its id, the daily summary `daily:` and the date |
| `severity` | `alert`, `warning` or `info` |
| `occurred_at` | When the audit row was written, in UTC |
| `installation` | This installation's id and the `label` of the settings |
| `audit` | The number of the audit row the message was made from, and that row's hash in the audit log. `null` for a test and for the daily summary |
| `rules` | The names of the rules that fired. One message goes out per channel however many rules fired |
| `crossed`, `recovered` | The level conditions that were crossed, or that recovered, in this cycle |
| `run` | The run: its id, the spec's id, name and version, the mode, and under `now` the run as it is at the moment the message is made, which can be later than the event. `licence` is `null` in a build that needs none. `approval` is `unapproved` here because an installation with token sign-in has no approvers. `null` for an event that concerns no run |
| `facts` | What was observed. Its members depend on the event; those of `cycle.ended` are below |
| `suppressed_since_last` | How many messages to this channel were held back since the last one that was sent |
| `withheld` | Members left out on purpose and why: for example `spec_name` with `labels_off`, or `link` with `links_off` |
| `claim` | Always `observed` |
| `link` | The address of the screen that shows the subject, built from `public_url` |
| `title`, `text` | The message in words, for people. Slack, Teams and e-mail show these |

**The facts of `cycle.ended`.**

| Member | Meaning |
|---|---|
| `cycle` | The number of the cycle that ended |
| `counters.total` | Keys in the cycle |
| `counters.match`, `different`, `indeterminate` | Keys by verdict: the two sides agree, differ, or could not be judged |
| `counters.compared` | `match` plus `different` |
| `counters.cancelled` | Keys that were never read. The text calls them "not read" |
| `counters.outstanding` | Keys still owed another read |
| `counters.mismatch_rate`, `coverage` | `different` divided by `compared`, and `compared` divided by `total` |
| `counters.absent_both` | Keys that neither side holds |
| `levels` | The four values that level conditions judge: the two rates above, the share of indeterminate keys, and `unexplained_critical`, the number of keys with a critical finding that no rule of the spec explains |
| `previous` | Each level in the cycle before, with that cycle's number. `null` in a first cycle |
| `new_root_causes`, `gone_root_causes` | Root causes seen for the first time in this run, and those of the cycle before that are gone. `baseline_from` is the earliest cycle they were compared with |
| `newly_discrepant`, `no_longer_discrepant` | Among the keys read in both cycles: those that are discrepant now and were not before, and the reverse. A key is discrepant when its values differ or one side does not hold it, which is narrower than `different`: see below the table. In a first cycle every discrepant key is new |
| `between_samples` | `true` when each cycle reads a fresh sample of keys, so that key changes cannot be counted |
| `unavailable` | The facts above that could not be counted, for example because the cycle before is already deleted |

In this run the mock pair gave 4 keys that differ out of 40, so the mismatch rate is 0.1. The rule
`new-gaps` fired because 4 root causes are new, and `mismatch-over-1pc` because 0.1 is above 0.01 and
no earlier cycle held it. `low-coverage` did not fire: every key was compared. The last words, "Not
sign-off evidence.", are added because the spec version is unapproved.

**Why the text says 4 different and then 3 discrepant.** The two words count different things, and
both numbers are right. A **discrepant** key is one whose values differ between the two sides, or
that one side does not hold: here 3. `different` counts those 3 and one more key, whose only
difference the spec explains. A spec can declare a **known difference**, a difference that is
expected, and say of what sort it is. The mock spec declares two. One is classified `bug`: a key
that only it explains still counts as `different`, because the difference is a defect that is to be
corrected. The other is classified `expected_by_design`: a key that only it explains counts as a
match. The run's own counters, read from `GET /v1/runs/<run id>`, said so: by outcome 35 `MATCH`, 2
`MISMATCH`, 1 `NOT_FOUND_B` and 2 `KNOWN_DIFF`. So `match` is 35 + 1, `different` is 2 + 1 + 1, and
`newly_discrepant` is 2 + 1.

**What a message never contains.** No key, no value from either side and no masked form of one. No
path of a field, no root cause id, no text typed by a person such as a reason or a note, no person's
name and no e-mail address. No address, file path or secret of a channel. A failed run is told by the
type of its error, never by the error's text. The spec's name is the only label a message carries,
and it is left out when the channel says `labels: false`, which is the default for Slack and Teams.
So name specs after the systems they compare, never after a customer.

### Limits

- A test is one attempt, outside the queue. It is not held back by a mute or a limit, it is never
  sent again, and it is listed as `sent` or `dead`.
- From the page, a channel takes one test in ten seconds.
- `sent` means the receiver answered with a status from 200 to 299. It does not mean that anyone read
  the message.
- Delivery is at least once: a receiver can get the same message twice, with the same `webhook-id`.
- The receiver of this chapter is for trying things on one machine. A real one has an `https`
  address, refuses old timestamps and remembers the ids it has seen.
- Not verified here: the receiver's two `-v` options on Linux, where the program in the container
  runs as user id 10001 and must be allowed to read both files.

### If something goes wrong

**`error: local-hook: connect_error (N-...)`**
Nothing listens where the message was sent. There are four causes. You typed the test with the `ds`
alias, which runs in a container of its own: use the command of step 1. The receiver is not running:
`docker ps --filter name=deltasleuth-hook` shows whether it is. The app was restarted since the
receiver started: run `docker restart deltasleuth-hook`. Or, in a second installation, the receiver
was started with the names as printed and sits beside another installation's app: see the note under
"Before you start".

A test that failed leaves a trace, and the trace is harmless. This was tried here after section 7,
with the alias: the test answered the line above, and after it `ds notify` counted `1 dead` and ended
the channel's line with `last connect_error`, although the channel worked. `ds notify deliveries
--state dead` listed the test. No banner appeared, and `ds notify retry local-hook` answered `0 dead
deliveries of local-hook since ... are pending again`, because a test is never sent again. The next
message that was sent, here a test typed as step 1 says, put `last ok (200)` back. The dead test
itself stays in the count "Dead 24 h" for a day, so after such a mistake your counts of dead messages
in section 5 are one higher than those printed there.

**`error: local-hook: http_4xx (HTTP 401) (N-...)`**, and the receiver prints `signature REFUSED,
answered 401`
The receiver does not hold the key the message was signed with. Provoked here by starting the
receiver without its second `-v` option. Remove it and start it as step 4 of "Before you start" says.

**`a test of channel local-hook was sent less than 10 s ago`** above the "Channels" table
Wait ten seconds and click "Send test" again.

**`error: channel nope is not declared`**
The name after `test` is not a channel of the settings. `ds notify` lists them.

**`docker: Error response from daemon: Conflict. The container name "/deltasleuth-hook" is already in
use by container "...". You have to remove (or rename) that container to be able to reuse that
name.`**
A receiver exists already, running or stopped. Remove it with `docker rm -f deltasleuth-hook`, then
start it again.

**`Error response from daemon: Cannot restart container deltasleuth-hook: joining network namespace
of container: No such container: ...`**
The app container was made anew since the receiver started. Remove the receiver and start it again.
**`Error response from daemon: cannot join network namespace of a non running container: container
deltasleuth-app-1 is exited`** means the app is stopped: start it first.

**The run completed and no `cycle.ended` is listed**
No rule fired for that cycle. Section 7 shows how to ask why.

## 4. The Notifications screen

### What it is for

The screen shows, to everyone who can sign in, where messages go, what was sent, what failed and what
was held back. Operators also send a test from it and send failed messages again. It answers the
question "did the people on the other end hear about this?".

### Before you start

Notifications are on (section 2). Any role may read the screen. The two buttons need the role
`operator` or higher; the token `first-run` of chapter 1 has it.

### In the web UI

Click "Notifications" in the bar. From top to bottom the page shows:

1. **Banners**, above the bar and on every screen, when there is something to say: a channel that is
   muted, a channel whose messages are failing, a secret file that cannot be read, or a worker that is
   behind. Each is a link to this screen. Sections 5 and 6 quote them.
2. **The grey line**, `label trial: audit row 22 of 22 notified`: how far the worker has read the
   audit log. When rows wait it ends with `; unread since` and a time.
3. **"Channels"**, one row for each declared channel. "Channel" is its name; a Slack or Teams channel
   carries the badge `external`, because its messages leave your network. "Kind" and "Host" are what
   the settings declare; the address, its path and the secrets are never shown. "Route" reads
   `private network allowed` or `via notify.proxy`. "Pending" counts the messages that wait. "Sent
   24 h", "Dead 24 h" and "Held back 24 h" count the last 24 hours. "Last" is the result of the newest
   attempt, `ok 200` or a code such as `connect_error`. For operators the row ends with "Send test"
   and "Send failed again" (section 5).
4. **"Rules"**, under the note "Declared in deltasleuth.yaml (notify.rules); a change applies at the
   next restart." One line for each rule, for example `new-gaps: cycle.ended to local-hook (modes
   batch, soak; when new_root_causes_at_least 1)`.
5. **"Mutes"**, the mutes in force, or `None.` (section 6).
6. **"Deliveries"**, every message, newest first, fifty at a time, with the button "More" for older
   ones. Two boxes above the table narrow it: `every channel` and `every state`. The columns are
   "Delivery" (the id), "Made", "Channel", "Event", "Severity", "State", "Attempts", "Last", "Audit
   row" and "Body". Under "Body", `SHA-256` and twelve characters open the stored message.

The page loads once. After "Send test" or "Send failed again" the "Channels" table is read again, but
the "Deliveries" table and the banners are not: reload the page in the browser to see them change.

### On the command line

1. Show the state: the banners, the line about the audit log, each channel, each rule and each mute.
   ```bash
   ds notify
   ```
   You should see, after section 3 done both in the page and on the command line, which makes two
   tests and two runs:
   ```text
    Container deltasleuth-db-1  Running
   label trial: audit row 22 of 22 notified
   channel local-hook (webhook, 127.0.0.1): 0 pending; in 24 h 4 sent, 0 dead, 0 held back; last ok (200)
   rule new-gaps: cycle.ended to local-hook
   rule mismatch-over-1pc: cycle.ended to local-hook
   rule low-coverage: cycle.ended to local-hook
   rule run-health: run.paused, run.stopped, run.failed, schedule.missed to local-hook
   ```
2. List the messages of one run. `<run id>` is the id that starting the run answered; `--channel
   <name>` and `--state <state>` narrow the list in the same way, and `--limit <n>` shows up to 500.
   ```bash
   ds notify deliveries --run <run id>
   ```
   You should see:
   ```text
    Container deltasleuth-db-1  Running
   N-35T7E2WJF32QQRN63MS85KEF82  2026-10-05T20:31:05+00:00  local-hook  cycle.ended  warning  sent  1 attempt(s)  audit row 21  run R-35T7E2H9G29PD5YTBS9FZ3CWXK  last ok (200)
   ```
3. Print the same list with every stored body, as JSON, for a program to read.
   ```bash
   ds notify deliveries --run <run id> --json
   ```
   You should see one object for each message, with its `body` as one long text and members such as
   `"state": "sent"`, `"attempts": 1` and `"body_sha256"`.

### How to read the result

A message is in one of four states:

| State | Meaning |
|---|---|
| `pending` | It waits: for its first attempt, or for the next one after a failure |
| `sent` | The destination took it |
| `dead` | It was given up, and "Last" says why. It can be sent again (section 5) |
| `suppressed` | It was stored and deliberately not sent, for the reason in brackets: `muted`, `duplicate` or `rate_limited`. It is never sent later |

A line of `ds notify deliveries` reads: the id, when it was made, the channel, the event, the
severity, the state, the attempts so far, the audit row and the run it is about, and the result of the
newest attempt.

### Limits

- The screen cannot create or change a channel or a rule, and cannot run the check of section 2 or
  the explanation of section 7.
- The page narrows deliveries by channel and state only. Only `ds notify deliveries --run` lists one
  run's messages.
- "Send failed again" always reaches back 72 hours. `ds notify retry` can take another start
  (section 5).
- Messages are deleted when they are older than `retention.standard_days` of the settings file, 90
  days as installed. The audit log is the lasting record.

### If something goes wrong

**`error: --state is one of pending, sent, dead, suppressed`**
The word after `--state` is not one of the four.

**The bar has no "Notifications" link**
The settings have no `notify` block, or you signed in before the app restarted with it: reload the
page.

## 5. When a destination is down

### What it is for

A receiver can be down for a minute or for a day, and it can refuse a message outright. This section
shows what DeltaSleuth does in each case, how you see it, and how messages that were given up are sent
again. It also shows the opposite failure: DeltaSleuth's own worker is down and sends nothing at all.
A run is never slowed or failed by a channel that does not answer.

### Before you start

Sections 2 and 3 are done and the receiver runs. In a second installation, the receiver's commands
below need your project name in the three places that the note in section 3 lists.

### In the web UI

1. While a message waits for another attempt, its row under "Deliveries" reads "State" `pending`,
   with the number of "Attempts" so far and the code of the last failure under "Last", for example
   `connect_error`. The channel's row counts it under "Pending".
2. When a message was given up, a banner appears above the bar on every screen: `Notifications to
   local-hook are failing: 1 not delivered since 2026-10-05 20:33 UTC`. The channel's row shows the
   count under "Dead 24 h" and the code under "Last", for example `http_4xx 410`.
3. When the receiver is in order again, click "Send failed again" in the channel's row. The browser
   asks "Send the failed deliveries of the last 72 hours to local-hook again?". Confirm. A line
   appears: `1 failed deliveries of local-hook are pending again.` Reload the page: the message is
   `sent`, and the banner is gone.

The request behind "Send failed again" was sent with curl here, with one message dead, and
answered `{"count": 1}`, from which the page makes the line of step 3.

The page offers no way to see that the worker itself is down other than the fourth banner, quoted in
part C below.

### On the command line

**A. The receiver is down for a while.**

1. Stop the receiver.
   ```bash
   docker stop deltasleuth-hook
   ```
2. Start a run with the curl command of section 3, "On the command line", step 4. Parts B and C and
   section 6 use that command again, and call it "the run command".
3. After fifteen seconds, list what waits.
   ```bash
   ds notify deliveries --state pending
   ```
   You should see the run's message, tried and not delivered:
   ```text
    Container deltasleuth-db-1  Running
   N-35T7E55HY5EKDK2D2QP5MDF242  2026-10-05T20:31:43+00:00  local-hook  cycle.ended  warning  pending  1 attempt(s)  audit row 24  run R-35T7E4TR8GJXJQYCWMRY9SS94G  last connect_error
   ```
   Your line can say `2 attempt(s)` instead. The first attempt is made at once. The wait before the
   second is drawn at random, up to a minute, so the second can already have failed too.
4. Start the receiver again.
   ```bash
   docker start deltasleuth-hook
   ```
5. Wait for the next attempt, then look. How long depends on the line of step 3: up to one minute
   if it said `1 attempt(s)`, up to three minutes if it said `2 attempt(s)`.
   ```bash
   ds notify deliveries --limit 1
   ```
   You should see it delivered. The number of attempts in your line can differ. Here the
   message was made at 20:31:43 and failed at once, and the second attempt, 51 seconds later,
   delivered it:
   ```text
    Container deltasleuth-db-1  Running
   N-35T7E55HY5EKDK2D2QP5MDF242  2026-10-05T20:31:43+00:00  local-hook  cycle.ended  warning  sent  2 attempt(s)  audit row 24  run R-35T7E4TR8GJXJQYCWMRY9SS94G  last ok (200)
   ```
   If the line still says `pending`, wait and run the command again: nothing is lost.

**B. The receiver refuses the message.**

1. Replace the receiver by one that answers every message with status 410, "gone". First remove it.
   ```bash
   docker rm -f deltasleuth-hook
   ```
2. Start it with `-e ANSWER=410`.
   ```bash
   docker run -d --name deltasleuth-hook --no-healthcheck --network container:deltasleuth-app-1 -e ANSWER=410 -v "$PWD/.deltasleuth/receiver:/receiver:ro" -v "$PWD/deploy/secrets/notify_hook_key:/run/secrets/notify_hook_key:ro" deltasleuth:source python /receiver/receiver.py
   ```
3. Start a run with the run command, and wait fifteen seconds.
4. Look at the state.
   ```bash
   ds notify
   ```
   You should see a banner as the first line, one message dead and the code of the refusal:
   ```text
    Container deltasleuth-db-1  Running
   Notifications to local-hook are failing: 1 not delivered since 2026-10-05 20:33 UTC
   label trial: audit row 29 of 29 notified
   channel local-hook (webhook, 127.0.0.1): 0 pending; in 24 h 5 sent, 1 dead, 0 held back; last http_4xx (410)
   ...
   ```
5. List the dead messages of the channel.
   ```bash
   ds notify deliveries --channel local-hook --state dead
   ```
   You should see:
   ```text
    Container deltasleuth-db-1  Running
   N-35T7E9V3TVFSDHMKR7C83NV2DQ  2026-10-05T20:33:02+00:00  local-hook  cycle.ended  warning  dead  1 attempt(s)  audit row 27  run R-35T7E9HEANNKAZJZEMJCDKMHJX  last http_4xx (410)
   ```
6. Put the working receiver back: remove this one.
   ```bash
   docker rm -f deltasleuth-hook
   ```
7. Start the receiver as in section 3, "Before you start", step 4.
   ```bash
   docker run -d --name deltasleuth-hook --no-healthcheck --network container:deltasleuth-app-1 -v "$PWD/.deltasleuth/receiver:/receiver:ro" -v "$PWD/deploy/secrets/notify_hook_key:/run/secrets/notify_hook_key:ro" deltasleuth:source python /receiver/receiver.py
   ```
8. Send the dead messages again. This is what "Send failed again" does. Without an option it takes
   those that died in the last 72 hours; `--since <time>` takes another start, written as a time with
   a zone, for example `2026-10-05T19:00:00+00:00`.
   ```bash
   ds notify retry local-hook
   ```
   You should see an audit row, then:
   ```text
   1 dead deliveries of local-hook since 2026-10-02T20:33:19+00:00 are pending again
   ```
9. Look after five seconds.
   ```bash
   ds notify deliveries --limit 1
   ```
   You should see it delivered, counted from attempt 1 again:
   ```text
    Container deltasleuth-db-1  Running
   N-35T7E9V3TVFSDHMKR7C83NV2DQ  2026-10-05T20:33:02+00:00  local-hook  cycle.ended  warning  sent  1 attempt(s)  audit row 27  run R-35T7E9HEANNKAZJZEMJCDKMHJX  last ok (200)
   ```
   `ds notify` then prints no banner.

**C. DeltaSleuth's own worker is down.**

When the app is stopped, nothing is sent and nothing tells you so. The remedy is a heartbeat: route
the daily summary to a channel that someone or something watches, and treat a day without it as an
alarm.

1. Open `deploy/config/deltasleuth.yaml` in a text editor and add this line as the last line of
   `rules:`, with the same four spaces in front as the rules above it.
   ```text
       - {name: heartbeat, events: [notify.daily], channels: [local-hook]}
   ```
2. Check the block.
   ```bash
   ds notify check
   ```
   You should see the `ok:` line of section 2.
3. Restart the app, so that it reads the rule.
   ```bash
   docker compose restart app
   ```
4. Give the receiver its network back.
   ```bash
   docker restart deltasleuth-hook
   ```
5. Wait a minute, then look.
   ```bash
   ds notify deliveries --limit 1
   ```
   You should see the summary:
   ```text
    Container deltasleuth-db-1  Running
   N-35T7EC786HHPM3FD4JDTXF9NZ2  2026-10-05T20:33:42+00:00  local-hook  notify.daily  info  sent  1 attempt(s)  last ok (200)
   ```
   The summary is made once for each UTC day, and only from `daily_hour` on, which is 07:00 UTC
   unless you set another hour. The worker looks when the app starts and then once an hour. Here the
   app started at 20:33 UTC, so the summary was made two seconds later. Your line can say `2
   attempt(s)`: when the summary is made before the receiver has its network back (step 4), the
   first attempt fails and the second, up to a minute later, delivers it.

   If the newest line is not a `notify.daily`, there are two causes, and `date -u` prints the UTC
   time that tells them apart.

   - Today's summary was made already, because you did this part earlier on the same UTC day. A
     restart makes no second one: here the app was restarted several times more with the
     rule in force, and `ds notify deliveries --limit 500 | grep notify.daily` still listed one
     summary for `local-hook`. The next one is made after 07:00 UTC of the next day. Read step 6
     without running it and go on with step 7.
   - It is before 07:00 UTC. The summary is made in the hour after 07:00 UTC, if the app runs then.
     To get it now, add a line `daily_hour: 0` under the line `label: trial`, with the same two
     spaces in front, and do steps 2 to 5 again. That setting was tried here at 20:49 UTC with a
     second channel that had no summary yet: with `daily_hour: 23` a restart made none, and with
     `daily_hour: 0` the next restart made it within two seconds. Not verified here: the same
     between 00:00 and 07:00 UTC.
6. Read it.
   ```bash
   docker logs --tail 44 deltasleuth-hook
   ```
   You should see, among the other members:
   ```text
     "event": "notify.daily",
     "event_id": "daily:2026-10-05",
     ...
     "facts": {
       "runs": {},
       "paused": {},
       "cycles_ended_24h": 4,
       "runs_finished_24h": 4,
       "approvals_waiting": 0,
       "licence": "open",
       "channels": {
         "local-hook": {
           "pending": 0,
           "sent_24h": 6,
           "dead_24h": 0,
           "suppressed_24h": 0,
           "head_age_s": null
         }
       },
       "feed_lag_s": 0,
       "mutes": 0
     },
     ...
     "text": "[trial] Daily summary: 0 runs running, 0 paused; 4 cycles ended and 4 runs finished in 24 h; 0 approvals waiting; licence open; the notifier is 0 s behind; 0 mutes active. Observed by DeltaSleuth (installation I-05110f6fb3b83fe8); not evidence and not a sign-off."
   ```

7. Now see what a stopped worker looks like. Stop the receiver first, so that a message has to wait.
   ```bash
   docker stop deltasleuth-hook
   ```
8. Start a run with the run command, and wait fifteen seconds. Then stop the app, and with it the
   worker.
   ```bash
   docker compose stop app
   ```
   You should see:
   ```text
    Container deltasleuth-app-1  Stopping
    Container deltasleuth-app-1  Stopped
   ```
9. See when the waiting message is next due. `ds` still answers, because it starts a container of
   its own and reads the database.
   ```bash
   ds notify deliveries --state pending --json | grep due_at
   ```
   You should see a time, in UTC, within about three minutes after you started the run:
   ```text
    Container deltasleuth-db-1  Running
       "due_at": "2026-10-05T20:35:02.491750+00:00",
   ```
10. Wait until ten minutes after that time, then look at the state. Here that was eleven
    minutes after the stop of step 8; seven minutes after the stop the banner was not there yet.
    ```bash
    ds notify
    ```
    You should see a banner as the first line, and the channel with `1 pending`:
    ```text
     Container deltasleuth-db-1  Running
    The notifier is behind: audit rows since 2026-10-05 20:35 UTC are not yet notified; is the worker running?
    label trial: audit row 36 of 36 notified
    channel local-hook (webhook, 127.0.0.1): 1 pending; in 24 h 7 sent, 0 dead, 0 held back; last connect_error
    ...
    ```
11. Start the app again.
    ```bash
    docker compose --profile mocks up -d
    ```
12. Start the receiver again, straight after it.
    ```bash
    docker start deltasleuth-hook
    ```
13. Wait ten seconds, then look.
    ```bash
    ds notify deliveries --limit 1
    ```
    You should see the message that waited, delivered. Here it went out three seconds after the app
    started:
    ```text
     Container deltasleuth-db-1  Running
    N-35T7EFK8PD3NR8YK5AR37M7J42  2026-10-05T20:34:38+00:00  local-hook  cycle.ended  warning  sent  2 attempt(s)  audit row 35  run R-35T7EF91BJM038KAH5DTJ3ZPYY  last ok (200)
    ```
    If your line still says `pending`, the app tried once more before the receiver was back. Wait as
    in part A, step 5, by the number of attempts the line shows, and look again.

### How to read the result

One attempt ends in one of three ways: the message is sent, it is tried again later, or it is given
up. "Last" shows a fixed code for the last two:

| Outcome | Codes |
|---|---|
| Sent | A status from 200 to 299, or the mail relay's 250 |
| Tried again later | `connect_error`, `dns_error`, `tls_error`, `timeout`, `http_5xx`, `http_429`, `http_408`, `http_425`, `smtp_temporary`, `secret_unreadable` |
| Given up at once | `http_4xx`, `http_3xx` (a redirect is never followed), `unsafe_destination`, `url_mismatch`, `smtp_permanent`, `smtp_no_starttls`, `payload_too_large` |
| Given up later | `retries_exhausted` after `max_attempts` attempts; `expired` after `max_age_hours`; `channel_removed` and `channel_changed` when the settings no longer hold the channel the message was made for |

The wait before the next attempt is drawn at random and grows: up to 1 minute after the first
attempt, up to 3 after the second, up to 9 after the third, up to 27 after the fourth and up to an
hour from then on. With the default of 8 attempts a message is given up after about two hours.

The fourth banner, `The notifier is behind`, appears when a message that is due, or an audit row
that a rule routes, has waited more than ten minutes. Its words speak of audit rows in both cases: in
step 10 every row had been read, as the next line said, and it was a message that waited. The time
in the banner is then the `due_at` of step 9. With the app stopped the web pages are down too, so
only `ds notify` showed it.

Messages to one channel go out in order: while the oldest one waits for its next attempt, the ones
behind it wait too. Messages sent again with `ds notify retry` go out after those, so out of their
first order.

When a message is given up, the worker writes an audit row, `notify.dead`, at most once an hour for
each channel: here a second death on the same channel, 24 minutes after the first, wrote none.
A rule can route that event to another channel, never to the one it is about. That was tried with
`notify.dead` added to the events of `run-health`: the row was written and nothing was sent. So with a
single channel the banner is what tells you. With two channels, one rule that sends `notify.dead` to
both tells each one's failures to the other.

In the daily summary, `runs` and `paused` count the live runs by status and the paused ones by
reason, `channels` repeats the counts of the screen, `head_age_s` is how long the oldest waiting
message has waited, and `feed_lag_s` is how far the worker is behind the audit log, in seconds.

### Limits

- The heartbeat tells you that the worker is down only if the place that receives it notices a
  missing message. DeltaSleuth cannot send that alarm itself.
- The health address of chapter 1 says nothing about the worker or about notifications.
- When the worker comes back after more than `max_age_hours`, the audit rows it missed are skipped
  and one `notify.skipped` says so. Not verified here.
- A dead message whose channel changed kind, `labels` or `links` since it was made cannot be sent
  again. Not verified here.
- Dead tests are never sent again: with nothing dead but failed tests, `ds notify retry` answered `0
  dead deliveries of local-hook since ... are pending again`.

### If something goes wrong

**`Notifications to local-hook cannot be sent: a secret file cannot be read`** (a banner), with
`secret_unreadable` under "Last", and at the app's start the log line **`warning: notify channel
local-hook: the signing key in /run/secrets/notify_hook_key is not whsec_ and base64 of 24 to 64
bytes; its deliveries retry until it is readable`**
The key file is damaged or gone. The app starts all the same and the messages wait. Make the key
again (section 2, steps 2 and 3, after `chmod u+w deploy/secrets/notify_hook_key`). Provoked here by
writing other text into the file: the waiting message went out on its next attempt, without a
restart.

**`error: since must be an ISO-8601 time with a zone, not in the future`**
The time after `--since` has no zone, lies in the future, or is not a time.

**A message stays `pending` for a long time**
Look at its attempts and at "Last". After several failures the next attempt can be up to an hour
away. The command of part C, step 9, prints the time it is due. Nothing makes it come sooner; "Send
test" tells you at once whether the destination answers now.

## 6. Mute a channel or a run

### What it is for

During planned work on a receiver, or while a known problem makes one run noisy, you do not want its
messages. A **mute** holds back everything for one channel, or everything about one run, for a number
of hours. Each mute has a reason, is recorded in the audit log, and is announced on every screen for
as long as it lasts.

### Before you start

Notifications are on. From the command line, anyone who can run `ds` in the installation's folder can
mute.

### In the web UI

With token sign-in, as chapter 1 installs it, the screen cannot mute and cannot end a mute. Both need
the role `admin`, and no API token can hold that role. Muting needs one thing more: the admin must
have signed in within the last 15 minutes. Ending a mute needs the role only. The "Mutes" table and
the banner still show every mute to everyone. The page does not say why the form is missing: with a
token, mute from the command line, as below.

With single sign-on, an admin sees a panel "Mute" under the "Mutes" table: a choice "Of" (`a channel`
or `a run`), a field "Channel or run", a field "Hours" that starts at 4, a box for the reason, marked
"Why", and the button "Mute". When the sign-in is older than 15 minutes, pressing "Mute" shows the
words "Sign in again to mute." and a button "Sign in again" under the form, and no mute is made.
Each row of the "Mutes" table ends with the button "End". Not verified here: this installation has no single sign-on. The two requests, the
one the form sends and the one "End" sends, were sent with the token of chapter 1, and each was
answered `{"detail": "needs the admin role"}`.

### On the command line

1. Mute the channel for one hour. `--hours` takes 1 to 168. The reason is written to the audit log,
   so never put personal data in it.
   ```bash
   ds notify mute --channel local-hook --hours 1 --reason 'receiver maintenance'
   ```
   You should see an audit row, then the mute's id, which starts with `M-`:
   ```text
   muted local-hook until 2026-10-05 21:46 UTC (M-35T7FTSGPXCD86T4WJMMT0B00K)
   ```
2. Look at the state.
   ```bash
   ds notify
   ```
   You should see the banner as the first line and the mute as the last:
   ```text
    Container deltasleuth-db-1  Running
   Notifications to local-hook are muted until 2026-10-05 21:46 UTC by local:<you>@deltasleuth-app (M-35T7FTSGPXCD86T4WJMMT0B00K)
   label trial: audit row 38 of 38 notified
   ...
   M-35T7FTSGPXCD86T4WJMMT0B00K  channel local-hook  until 2026-10-05T21:46:43+00:00  by local:<you>@deltasleuth-app: 'receiver maintenance'
   ```
3. Start a run with the run command (section 3, "On the command line", step 4), wait fifteen
   seconds, and list the newest message.
   ```bash
   ds notify deliveries --limit 1
   ```
   You should see it stored and not sent:
   ```text
    Container deltasleuth-db-1  Running
   N-35T7FV6Q075CQYQ66MN16CWXEG  2026-10-05T20:46:50+00:00  local-hook  cycle.ended  warning  suppressed (muted)  0 attempt(s)  audit row 40  run R-35T7FTXXDHVZEVNKRKME015F51
   ```
4. End the mute before its time. `<mute id>` is the id of step 1.
   ```bash
   ds notify unmute <mute id>
   ```
   You should see an audit row, then:
   ```text
   ended mute M-35T7FTSGPXCD86T4WJMMT0B00K
   ```
5. Start one more run, wait fifteen seconds, and read the last two lines of what the receiver got.
   ```bash
   docker logs --tail 2 deltasleuth-hook
   ```
   You should see the end of the new run's message. Its text, shortened here at the `...`, ends by
   saying that one message was held back:
   ```text
     "text": "[trial] mock-customer-v1-vs-v2: cycle 1 of run R-35T7FW1ZQ9SDQKZ2XSE9RT4HSH ended: ... Audit row 46. Not sign-off evidence. (1 notification to this channel was held back since the last one: see Notifications)"
   }
   ```

To mute one run instead of a channel, give `--run <run id>` in place of `--channel local-hook`. Here
that answered `muted run R-35T7E2H9G29PD5YTBS9FZ3CWXK until 2026-10-05 21:47 UTC (M-...)`,
and the banner read `Notifications for run R-35T7E2H9G29PD5YTBS9FZ3CWXK are muted until 2026-10-05
21:47 UTC by local:<you>@deltasleuth-app (M-...)`. That mute was ended again with `ds notify unmute`.
Not verified here: a message held back by a run's mute, since a finished batch run sends nothing
more.

### How to read the result

A muted message is stored with the state `suppressed (muted)`. It is not sent when the mute ends. The
"Held back 24 h" count of the channel, and `suppressed_since_last` in the next message that is sent,
tell the receiver's owner that something was left out; the "Deliveries" table shows what.

### Limits

- A mute lasts 168 hours, one week, at most, and cannot be made longer: make a new one.
- A mute covers one channel or one run. There is no mute for one rule, one spec or one event, and
  there are no quiet hours.
- A test is sent even while its channel is muted.
- Alerts are muted like every other message.

### If something goes wrong

**`error: give --channel or --run`**
Give exactly one of the two.

**`error: hours must be 1 to 168`**
The number after `--hours` is outside that range.

**`error: target must be a declared channel for scope channel, or R-… for scope run`**
The name after `--channel` is not a channel of the settings. A run id that does not exist answers
**`error: run R-00000000000000000000000000 not found`**.

**`error: mute M-... ended already`**, or **`error: mute M-... not found`**
The mute was ended before, or the id is mistyped. `ds notify` lists the mutes in force.

## 7. Ask why a cycle did or did not notify

### What it is for

A cycle ended and you expected a message, or you got one and do not see why. `ds notify explain`
prints the numbers the worker computes for one ended cycle, and what each rule makes of them. It
writes nothing and sends nothing, so it is also the way to try a rule's numbers on a past cycle.

### Before you start

Notifications are on, and the run has at least one ended cycle.

### In the web UI

The UI does not offer this. No screen and no API route shows why a rule fired or did not.

### On the command line

1. Find the run's id. The newest run is the first line.
   ```bash
   ds runs
   ```
   You should see two lines for each run you started in this chapter, here seven runs. The `...`
   stands for the older ones:
   ```text
    Container deltasleuth-db-1  Running
   R-35T7FW1ZQ9SDQKZ2XSE9RT4HSH  mock-customer-v1-vs-v2 1.0.0  batch  completed  cycle 1  different 4 of 40
     Unapproved spec version: clear, known_diffs were not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
   ...
   ```
2. Explain its latest ended cycle. `<run id>` is the id at the start of the line; `--cycle <n>` names
   an earlier cycle of a soak run.
   ```bash
   ds notify explain <run id>
   ```
   You should see the cycle's numbers, then one line for each rule of the settings file, the
   `heartbeat` rule of section 5 included:
   ```text
    Container deltasleuth-db-1  Running
   cycle 1 of run R-35T7FW1ZQ9SDQKZ2XSE9RT4HSH (mock-customer-v1-vs-v2, batch)
     total 40, compared 40, match 36, different 4, indeterminate 0, cancelled 0, outstanding 0, mismatch_rate 0.1, coverage 1, absent_both 0
     mismatch_rate 0.1 (held by no earlier cycle)
     indeterminate_rate 0 (held by no earlier cycle)
     coverage 1 (held by no earlier cycle)
     unexplained_critical 2 (held by no earlier cycle)
     in the first cycle: 4 root causes and 3 discrepant keys observed
     unavailable: -
   rule new-gaps: fires: new_root_causes_at_least held
   rule mismatch-over-1pc: fires: mismatch_rate_above crossed
   rule low-coverage: does not fire
   rule run-health: takes no cycle.ended
   rule heartbeat: takes no cycle.ended
   ```

### How to read the result

The second line holds the counters of section 3. The next four lines are the levels, each with its
value in the cycle before, or `held by no earlier cycle`. Then come the changes: in a first cycle the
root causes and discrepant keys that were observed, in a later one those that are new and those that
are gone. Section 3, "How to read the result", says why 3 discrepant keys stand beside 4 different
ones. `unavailable` names the numbers that could not be counted.

Each rule gets one of these verdicts:

| Verdict | Meaning |
|---|---|
| `fires: <condition> held` | A change condition reached its number |
| `fires: <condition> crossed`, or `recovered` | A level condition holds now and did not in the cycle before, or the reverse |
| `does not fire` | The rule takes this cycle, and none of its conditions is met |
| `does not take this run (specs or modes)` | The rule's `specs` or `modes` leave this run out. Not provoked here |
| `takes no cycle.ended` | The rule is for other events |

When a rule fires and still no message was sent, look at the message's state in `ds notify deliveries
--run <run id>`: it may be `suppressed`, as a duplicate, by a mute or by the hourly limit.

### Limits

- It explains `cycle.ended` only. For every other event the rule's `events`, `specs`, `modes` and
  `reasons` are all there is to check.
- It judges the cycle with the settings file as it is now, not with the rules that were in force when
  the cycle ended.
- It reads the data as it is now: once a cycle's data is deleted at the end of its retention, the
  first line says so and some numbers are unavailable. Not verified here.

### If something goes wrong

**`error: run R-00000000000000000000000000 not found`**
The id is mistyped. `ds runs` lists the runs.

**`error: notifications are off: add notify to deltasleuth.yaml and restart (docs/mvp/ALERTS.md
§8)`**
The settings file has no `notify` block now. The command reads the rules from that file, so it cannot
explain a cycle without one.

**`error: run R-... has no cycle 40`**
The number after `--cycle` is higher than the run's cycles.

## 8. What cannot be a destination

### What it is for

A notification is the one thing DeltaSleuth sends, unasked, to an address that someone typed into a
file. These rules keep a typing mistake, or a person with bad intent, from pointing it at a place
inside your network that was never meant to get it. They matter to whoever approves the installation.

### Before you start

Nothing.

### In the web UI

The "Channels" table shows what each channel is allowed: "Route" reads `private network allowed` for
a channel with `allow_private_network: true` and `via notify.proxy` for one with `proxy: true`, and a
Slack or Teams channel carries the badge `external`. The address itself is never shown.

### On the command line

`ds notify check` (section 2, step 9) applies the same rules as a send, to every channel, without
sending. "If something goes wrong" in section 2 quotes what it answers to a plain `http` address that
is not loopback, to a private address without `allow_private_network: true`, to a host name that has
a private address or none, and to a Slack or Teams channel that gives its address as `url`. It
refuses three more mistakes, with these words in its `error:` line:

| What the block says | What the check answers |
|---|---|
| `url: "https://169.254.169.254/hook"`, with or without `allow_private_network` | `169.254.169.254 is never a notification destination (link-local, unspecified, multicast or metadata)` |
| A Slack channel whose address file holds an `http` address | `the URL in /alt/chat_url_http is not an https URL for host mocks` (the path and the host are those of the stand-in used here; the address found is never printed) |
| `proxy: true` without `notify.proxy` | `channels.team-chat: proxy: true needs notify.proxy` |

### How to read the result

The rules:

- **Only four kinds.** A signed webhook, Slack, Teams and e-mail through a relay. There is no
  ticketing system, no pager service and no text message; a webhook to a service of your own is the
  way to reach those.
- **`https`, or loopback.** A webhook, Slack or Teams address is `https`. Plain `http` is accepted
  only to `localhost`, `127.0.0.1` and `[::1]`, and never through the proxy.
- **Never these addresses.** Link-local addresses, which include the metadata address of cloud
  machines, 169.254.169.254; the unspecified address; multicast addresses. No setting allows them.
- **Private addresses only when you say so.** Any other address that is not public needs
  `allow_private_network: true` on its channel.
- **Checked at every send.** A host name is looked up again for each attempt and every address it
  has is judged, so a name that later points somewhere else is refused then, with
  `unsafe_destination`.
- **No redirects.** An answer that redirects is a failure, `http_3xx`.
- **A secret address stays bound to its host.** The file behind `url_ref` must hold an `https`
  address for exactly the declared `host`; otherwise the message is given up with `url_mismatch`.
- **Nothing from the environment.** The variables `HTTP_PROXY` and `HTTPS_PROXY` are ignored. Only
  `notify.proxy` is used, and only by channels with `proxy: true`.
- **Size.** A message is at most 64 KiB, and 28 KiB for Teams.

### Limits

- Behind `notify.proxy`, the proxy looks up the destination's name. DeltaSleuth then checks the
  declared host only as it is written.
- E-mail with `tls: none` sends no password and is accepted only to a private relay. There is no
  XOAUTH2 sign-in to a mail service: use an internal relay.
- Not verified here: `unsafe_destination`, `http_3xx` and `url_mismatch` at a send, an address file
  that names another host than `host`, the proxy, and the size limits. They are as the contract and
  `deltasleuth/connectors/notify.py` state them; the refusals in the table above were provoked.

### If something goes wrong

**A message died with `unsafe_destination`**
The destination's name now has an address that the rules above refuse. Run `ds notify check`: it
prints the address and the reason.

**A message died with `url_mismatch`**
The address in the file behind `url_ref` is not `https`, or not for the declared `host`. Correct the
file or the `host` line, check, and send the dead messages again (section 5).

## Tidy up

The receiver and the channel of this chapter are for practice. To switch notifications off again:

1. Remove the receiver. In a second installation, write your project name in place of
   `deltasleuth`, as in section 3.
   ```bash
   docker rm -f deltasleuth-hook
   ```
2. Open `deploy/config/deltasleuth.yaml` in a text editor and delete the block you added: the line
   `notify:` and every line under it, to the end of the file.
3. Restart the app.
   ```bash
   docker compose restart app
   ```
4. Check.
   ```bash
   ds notify
   ```
   You should see:
   ```text
    Container deltasleuth-db-1  Running
   notifications are off: add notify to deltasleuth.yaml and restart (docs/mvp/ALERTS.md §8)
   ```

The messages that were stored stay listed by `ds notify deliveries` until their retention ends. The
secret can stay where it is: without the block nothing reads it. To remove it as well, undo steps 5
and 6 of section 2 in the site file, run `docker compose --profile mocks up -d`, which makes the app
container anew, and delete the key with `rm -f deploy/secrets/notify_hook_key`. All of that was done
here, and the app answered at its health address afterwards.

To keep notifications and point them at a real destination instead, write its `https` address as the
channel's `url`, leave `allow_private_network: true` out unless that address is a private one, hand
the receiver's owner the same key, then check and restart as in section 5, part C, steps 2 and 3.

## Reference

- [ALERTS.md](../mvp/ALERTS.md): the contract of notifications: every event with its facts, the
  rules, the message's layout, the outcome codes and every binding message.
- [DEPLOY.md](../mvp/DEPLOY.md), "Notifications" in sections 3 and 11: the settings of an office
  installation with Teams and e-mail, the firewall openings, a signature check for a receiver of your
  own, and a troubleshooting table.
- [RUNBOOK.md](../mvp/RUNBOOK.md), "Notifications": what each event asks of the person who gets it.
- [AUTH.md](../mvp/AUTH.md): who may test, send again and mute, and the audit rows these write.
- [SCHEDULES.md](../mvp/SCHEDULES.md), [PRECHECKS.md](../mvp/PRECHECKS.md) and
  [READINESS.md](../mvp/READINESS.md): the events `schedule.missed`, `schema.drift` and
  `readiness.changed` in full.
- [RETENTION.md](../mvp/RETENTION.md): how long stored messages and mutes are kept.

Back to [chapter 1](01-install-docker.md).
