# 10. The AI chat and your own AI agents

DeltaSleuth has two optional doors for a language model, a program that reads and writes text. Both
are closed as installed, and everything else works without them.

**Part one, the chat.** A panel in the web UI answers a question about one run or one key in plain
words. DeltaSleuth sends the question to a model that you name, lets the model look results up, and
checks the answer before it shows it.

**Part two, the MCP server.** Your own AI agent, for example an assistant in your editor, reads runs
and findings from DeltaSleuth with eleven tools. MCP, the Model Context Protocol, is the common way
such agents call tools.

The difference matters. In the chat DeltaSleuth calls the model and checks what it says. Through MCP
your agent calls DeltaSleuth, and DeltaSleuth checks nothing the agent then writes.

The words of [chapter 3](03-first-comparison.md) are used here: a **run** is one comparison of a set
of keys, a **key** identifies one record, a **finding** is one difference inside a key, and a **root
cause** is a group of findings that look alike. Keys and protected values are shown masked, as
`tok:...` or `class:...`.

**The answers printed in part one are a practice model's, not a language model's.** Part one was run
against a small program that speaks the same API as a model and understands nothing. Section 1 starts
it for you, so that you can repeat every step; it proves the wiring and says nothing about what a
real model answers.

Ids, masked values and times will differ on your machine, and `<you>` stands for your user name.
"How this chapter was checked", near the end, lists what was run and what was not.

## Before you begin

1. Open a terminal in the installation's folder, `IntelliDiff`, and define the short form `ds`, as in
   chapter 1, section 5.
   ```bash
   alias ds='docker compose run --rm -e DS_OPERATOR="$(id -un)" app ds'
   ```
2. Define a second short form, `dscurl`, for requests to the API. Its first word is the name of a
   token file in `.deltasleuth/tokens/`; the rest goes to curl. The token passes from its file through
   a pipe, so it appears neither on the screen nor in a list of running programs.
   ```bash
   dscurl() { t=".deltasleuth/tokens/$1"; shift; { printf 'header = "Authorization: Bearer '; tr -d '\n' < "$t"; printf '"\n'; } | curl -sS -K - "$@"; }
   ```
3. Try it with the token of chapter 1.
   ```bash
   dscurl first-run http://localhost:8000/v1/auth/me
   ```
   You should see the token's name and roles:
   ```text
   {"actor": "token:T-7986935c349d2a1a", "name": "first-run", "display_name": null, "kind": "token", "roles": ["viewer", "operator", "spec_author"], ...}
   ```

Both short forms last until the terminal closes and work only in this folder.

Docker puts a line of its own before the output of every `ds` command: ` Container deltasleuth-db-1
Running`, or in a terminal window the two lines `[+] Creating 1/1` and `✔ Container deltasleuth-db-1
Running`. Many `ds` commands then print lines that start with `{"id":`, copies of the rows they wrote
to the audit log, the record of who did what. The "You should see" blocks leave Docker's line out and
shorten the audit lines.

## Part one: the chat

Sections 1 to 3. The model here is one that DeltaSleuth calls, so these sections need a model
endpoint, an address that answers chat requests. If you have none, section 1 starts the practice
model: with it you can do every step of sections 1 and 3 and the exercise that ends section 2. Part
two needs no model at all.

## 1. Switch the chat on

### What it is for

The chat explains results to the people who read them. The comparison engine has already decided
every outcome; the model only looks stored results up and puts them into sentences, and it must name
the id of each thing it used. Switching it on is a task for whoever administers the installation: it
means telling DeltaSleuth which model to call, with which key, and what data that model may receive.

### Before you start

A model endpoint must speak the OpenAI Chat Completions API, which many model servers and gateways
offer, and it must support tool calling, which lets the model ask its caller to run a function. The
app container must be able to reach it.

Which steps you take depends on what you have:

| You have | Take |
|---|---|
| No endpoint | Steps 1 to 9. They start the practice model beside the app and switch the chat on with it |
| An endpoint on your own network, with https and a key | Steps 10 to 21 |
| The same, with a certificate that an authority of your own organisation signed | Steps 10 to 18, then 22 to 28, then 19 to 21 |
| A model server on this machine, over plain http and without a key | "A model server on this machine", after the steps |
| A hosted model on the internet | Section 2 |

The steps change or add these files. The settings are read when the app starts, so each path ends
with a restart.

| File | Steps | What changes |
|---|---|---|
| `.deltasleuth/practice-model.py` | 1 | New: the practice model's program |
| `deploy/config/llm_profiles.yaml` | 4, 10 | The endpoint's address and model |
| `deploy/config/deltasleuth.yaml` | 5, 17 | Three lines that switch the chat on |
| `deploy/secrets/llm_key` | 11 | New: the endpoint's key |
| `deploy/docker-compose.site.yml` | 15, 26 | Shows the key file, and your authority's certificate, to the app container |

### In the web UI

The web UI cannot switch the chat on, and no page shows which model is configured. It only shows the
state: at the bottom right of a run's page stands the grey label "AI is off" while the chat is off,
and the button "Ask AI about this run" once it is on.

### On the command line

**With the practice model.** Steps 1 to 9 need nothing but the installation of chapter 1. With an
endpoint of your own, read them once and start at step 10.

1. Write the practice model's program into the installation's folder. The command is long, so it
   stands near the end of this chapter under "The practice model's program": copy the whole block
   from there, paste it into the terminal and press Enter. Then check that the file arrived whole.
   ```bash
   wc -l < .deltasleuth/practice-model.py
   ```
   You should see its number of lines:
   ```text
        155
   ```
2. Start the program in a container of its own, beside the app. The container runs the image that
   chapter 1 built, joins the installation's network under the name `practice-model`, and publishes
   no port: only the installation's containers can reach it.
   ```bash
   docker run -d --name deltasleuth-practice-model --no-healthcheck --network deltasleuth_default --network-alias practice-model --mount "type=bind,source=$PWD/.deltasleuth/practice-model.py,target=/practice-model.py,readonly" deltasleuth:source python /practice-model.py
   ```
   You should see the new container's id, one line of 64 letters and digits. This is a `docker`
   command without the word `compose`, so it names the project itself: in a second installation
   (chapter 1, "A second installation on the same machine") write your project name in place of
   `deltasleuth` in the container's name, in `deltasleuth_default` and in `deltasleuth:source`.
3. Check that it listens.
   ```bash
   docker logs deltasleuth-practice-model
   ```
   You should see:
   ```text
   practice model listening on http://0.0.0.0:18441
   ```
4. Open `deploy/config/llm_profiles.yaml` in a text editor. A profile is the record of one endpoint.
   `hosts` lists the host names that are your own, written `internal`; every host not listed counts as
   outside your network. Add two things, indented as shown: under `hosts:` the line that starts
   `practice-model:`, and under `profiles:` the seven lines from `practice:` to `capabilities` with
   an empty line after them. The other three lines shown are in the file already and mark the places.
   Leave the rest of the file as it is.
   ```text
   hosts:
     models.internal.example: internal            # your model gateway, and nothing else
     practice-model: internal                     # the practice model of the guide, chapter 10

   profiles:
     practice:                                    # the practice model: plain http, no key
       dialect: openai_chat
       base_url: http://practice-model:18441/v1
       model: practice-1
       data_classes: [synthetic, golden, schema_summary, aggregates, production_tokenised]
       environments: [dev, test, eval, staging, prod]
       capabilities: {tool_calling: true}

     gateway:                                     # any OpenAI-compatible Chat Completions endpoint
   ```
   `data_classes` lists the kinds of data this endpoint may receive, and `environments` the kinds of
   installation that may use it. "Limits" below says what the chat sends.
5. Open `deploy/config/deltasleuth.yaml`, remove the `# ` at the start of these three lines, and
   write `practice` in place of `gateway`.
   ```text
   llm_profiles_file: llm_profiles.yaml
   llm_profile: practice
   llm_environment: prod
   ```
   `llm_profile` names the profile the chat uses. `llm_environment` says what kind of installation
   this is, one of `dev`, `test`, `eval`, `staging` and `prod`.
6. Probe the profile. The probe sends three small made-up requests, none with your data: a plain one,
   and a tool call with its answer. Without an option it probes the profile that `llm_profile` names,
   so it checks the three lines of step 5 as well.
   ```bash
   ds llm-probe
   ```
   You should see one line with a verdict for each of two checks:
   ```text
   profile practice: tool_roundtrip=pass, usage_cached_tokens=fail
   ```
   Go on when `tool_roundtrip` says `pass`. The word `fail` after `usage_cached_tokens` is harmless
   here; "How to read the result" explains it.
7. Restart the app so that it reads the two files.
   ```bash
   docker compose restart app
   ```
   You should see, as the last line:
   ```text
    Container deltasleuth-app-1  Started
   ```
8. Wait five seconds, then ask the app.
   ```bash
   curl -fsS http://localhost:8000/v1/health
   ```
   You should see `"ai": true`:
   ```text
   {"status": "ok", "ai": true, "auth": "tokens"}
   ```
9. List the profiles the app loaded. `default` is the one the chat uses.
   ```bash
   dscurl first-run http://localhost:8000/v1/llm/profiles
   ```
   You should see the practice profile with `"trust": "internal"`, then the three examples of the
   file:
   ```text
   {"default": "practice", "items": [{"profile_id": "practice", "dialect": "openai_chat", "model": "practice-1", "trust": "internal"}, {"profile_id": "gateway", "dialect": "openai_chat", "model": "your-model-id", "trust": "internal"}, {"profile_id": "claude", "dialect": "anthropic_messages", "model": "claude-opus-5", "trust": "external"}, {"profile_id": "openai", "dialect": "openai_chat", "model": "gpt-5", "trust": "external"}]}
   ```

The chat is on. Section 3 asks it questions.

**With an endpoint of your own, over https and with a key.** Steps 10 to 21. They use the example
profile `gateway`. `<model host>` is the endpoint's host name alone, in lower case, without
`https://` and without a port. `<model>` is the model's name as the endpoint knows it.

10. In `deploy/config/llm_profiles.yaml`, write your host in the first line under `hosts:`, and your
    address and model in the lines `base_url` and `model` of the profile `gateway`. Leave the
    rest; the lines of the practice model, if you added them, may stay.
    ```text
    hosts:
      <model host>: internal            # your model gateway, and nothing else

    profiles:
      gateway:                                     # any OpenAI-compatible Chat Completions endpoint
        dialect: openai_chat
        base_url: https://<model host>/v1
        model: <model>
        auth_ref: file:/run/secrets/llm_key
        data_classes: [synthetic, golden, schema_summary, aggregates, production_tokenised]
        environments: [dev, test, eval, staging, prod]
        capabilities: {tool_calling: true}         # the chat needs tool calling; the probe checks it
        budgets: {max_tool_calls: 8, max_input_tokens_per_call: 20000, max_wall_s: 60}
    ```
    A port, if the endpoint has one, goes into `base_url` only, as in
    `https://<model host>:8443/v1`.
11. Put the endpoint's key into a file without showing it. Copy the key to the clipboard where it was
    issued. Then, on macOS:
    ```bash
    (umask 077; pbpaste | tr -d '\n' > deploy/secrets/llm_key)
    ```
    On Linux, the first command with X11 (it needs `xclip`), the second with Wayland
    (`wl-clipboard`):
    ```bash
    (umask 077; xclip -selection clipboard -o | tr -d '\n' > deploy/secrets/llm_key)
    ```
    ```bash
    (umask 077; wl-paste | tr -d '\n' > deploy/secrets/llm_key)
    ```
    The command prints nothing. `umask 077` creates the file readable by you alone, and `tr` removes
    a line end. Not verified here: the two Linux commands, for lack of a Linux machine.
12. Empty the clipboard. On macOS:
    ```bash
    pbcopy < /dev/null
    ```
    On Linux, the first command with X11, the second with Wayland:
    ```bash
    xclip -selection clipboard < /dev/null
    ```
    ```bash
    wl-copy --clear
    ```
    Not verified here: the two Linux commands, for the same reason. They are the ones chapter 1
    prints.
13. Let the container read the file. The folder's own mode keeps it private on the machine
    ([appendix A1](A1-install-by-hand.md), section 2.4).
    ```bash
    chmod 444 deploy/secrets/llm_key
    ```
14. Check that the file holds something, without printing it.
    ```bash
    wc -c < deploy/secrets/llm_key
    ```
    You should see the key's length in characters, here that of the 24-character value this step was
    tried with:
    ```text
          24
    ```
15. Open `deploy/docker-compose.site.yml`, the site file, so that Docker shows the key file to the
    app container as `/run/secrets/llm_key`. The file has one of two forms.

    If it is the copy of the example, with many comment lines, make three edits. In the line that
    starts with `    secrets: []`, write `llm_key` between the brackets:
    ```text
        secrets: [llm_key]                                # single sign-on: oidc_client_secret; AI chat: llm_key; each with its line under secrets: below
    ```
    Near the end, remove the `# ` at the start of the line `# secrets:`. In the line
    `#   llm_key: {file: ./secrets/llm_key}`, remove the `#` and one space, so that two spaces stay
    before `llm_key`. Leave the line of `oidc_client_secret` as it is.
    ```text
    secrets:
    #   oidc_client_secret: {file: ./secrets/oidc_client_secret}
      llm_key: {file: ./secrets/llm_key}
    ```
    If the script of chapter 1 wrote the file for a second installation, its first line says
    `written by deploy/up.sh` and it has seven lines. It already holds the line `    secrets: []`
    under `app:`. Change that line; do not add a second one. Then add two lines at the end of the
    file. The whole file then reads, with your own port and subnet:
    ```text
    # This installation's changes to docker-compose.yml, written by deploy/up.sh for its own port and subnet;
    # deploy/docker-compose.site.example.yml shows what else goes here.
    services:
      app:
        secrets: [llm_key]
        ports: !override ["127.0.0.1:18000:8000"]
    networks: {default: {ipam: {config: !override [{subnet: 172.31.10.0/24, gateway: 172.31.10.1}]}}}
    secrets:
      llm_key: {file: ./secrets/llm_key}
    ```
16. Let Docker Compose read the site file. This command starts nothing.
    ```bash
    docker compose config --quiet
    ```
    It prints nothing when the file is in order. A mistake shows as one line that starts
    `failed to parse`; "If something goes wrong" has it.
17. Open `deploy/config/deltasleuth.yaml` and remove the `# ` at the start of these three lines. If
    you did step 5, write `gateway` back in the second line instead.
    ```text
    llm_profiles_file: llm_profiles.yaml
    llm_profile: gateway
    llm_environment: prod
    ```
18. Probe the profile, as in step 6.
    ```bash
    ds llm-probe
    ```
    You should see:
    ```text
    profile gateway: tool_roundtrip=pass, usage_cached_tokens=fail
    ```
    Go on when `tool_roundtrip` says `pass`. If you see `stamina.retry_scheduled` and a line that
    starts `error: Connection:` instead, and your endpoint's certificate comes from an authority of
    your own, go to step 22.
19. Apply the site file and start the app on the new settings. `restart` would not show the key file
    to the container; this command makes the app container anew, and the data stays.
    ```bash
    docker compose --profile mocks up -d
    ```
    You should see:
    ```text
     Container deltasleuth-db-1  Running
     Container deltasleuth-mocks-1  Running
     Container deltasleuth-app-1  Recreated
     Container deltasleuth-db-1  Healthy
     Container deltasleuth-app-1  Started
    ```
20. Wait five seconds, then ask the app.
    ```bash
    curl -fsS http://localhost:8000/v1/health
    ```
    You should see `"ai": true`:
    ```text
    {"status": "ok", "ai": true, "auth": "tokens"}
    ```
21. List the profiles the app loaded.
    ```bash
    dscurl first-run http://localhost:8000/v1/llm/profiles
    ```
    You should see `"default": "gateway"`, and your profile with your model and
    `"trust": "internal"`:
    ```text
    {"default": "gateway", "items": [..., {"profile_id": "gateway", "dialect": "openai_chat", "model": "<model>", "trust": "internal"}, {"profile_id": "claude", "dialect": "anthropic_messages", "model": "claude-opus-5", "trust": "external"}, {"profile_id": "openai", "dialect": "openai_chat", "model": "gpt-5", "trust": "external"}]}
    ```

**When the certificate comes from an authority of your own.** An endpoint inside an organisation
often has a certificate that the organisation's own certificate authority signed. The app's image
does not know that authority, so the probe of step 18 fails with `error: Connection: ...`, in the same
words as for a host that is down. Steps 22 to 28 find out which it is and make the app trust your
authority. `<port>` is the endpoint's port, 443 if `base_url` names none. `<authority file>` is your
authority's certificate as a text file that begins `-----BEGIN CERTIFICATE-----`; whoever runs the
endpoint has it.

22. Ask the app's image to open an https connection to the endpoint. The command sends nothing.
    ```bash
    docker compose run --rm -T --no-deps app python -c "import socket, ssl; ssl.create_default_context().wrap_socket(socket.create_connection(('<model host>', <port>), 10), server_hostname='<model host>'); print('the certificate is trusted')"
    ```
    You should see about twenty lines that Python prints about an error. The last one names the
    cause, and this one means the authority is not trusted:
    ```text
    ssl.SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate (_ssl.c:1032)
    ```
    A last line `ConnectionRefusedError: [Errno 111] Connection refused` means that nothing listens
    on that port, and `socket.gaierror: [Errno -2] Name or service not known` that the container
    cannot find the host name. Those two are not about a certificate: correct `base_url`.
23. Make a folder for the list of authorities.
    ```bash
    mkdir -p deploy/ca
    ```
24. Copy the image's own list of authorities into it. In a second installation the image is
    `<project>:source`.
    ```bash
    docker run --rm deltasleuth:source cat /etc/ssl/certs/ca-certificates.crt > deploy/ca/ca-bundle.crt
    ```
    It prints nothing.
25. Add your authority's certificate to the end of the copy.
    ```bash
    cat <authority file> >> deploy/ca/ca-bundle.crt
    ```
    Count the certificates in the copy:
    ```bash
    grep -c 'BEGIN CERTIFICATE' deploy/ca/ca-bundle.crt
    ```
    You should see one more than the image's own number, here:
    ```text
    151
    ```
26. In `deploy/docker-compose.site.yml`, add this line under `app:`, with four spaces before it, like
    the line `secrets:` of step 15. It puts your copy in the place of the image's list, read-only.
    ```text
        volumes: ["./ca/ca-bundle.crt:/etc/ssl/certs/ca-certificates.crt:ro"]
    ```
    If `app:` already has a line `volumes:` without a `#` before it, for example from the chapter on
    files, do not add a second one: add the entry to its list, after a comma and inside the brackets.
    Then check the file as in step 16.
    ```bash
    docker compose config --quiet
    ```
    It prints nothing when the file is in order.
27. Repeat the command of step 22. You should now see:
    ```text
    the certificate is trusted
    ```
28. Probe again as in step 18. You should see the line with `tool_roundtrip=pass`. Then go on with
    steps 19 to 21.

The copy holds the image's list as it was on the day of step 24, so repeat steps 24 and 25 and
restart the app after an update of the installation (chapter 1, section 7). Step 24 reads the image
itself, not your copy: run again with the copy in place, it still gave the image's own 150
certificates. Not verified here: an update with the copy in place.

Not verified here: the route the deployment document gives under "TLS", which builds a layer on top
of the image. That layer's tag, `deltasleuth:<version>-ca`, carries no project name, so every
installation on a machine shares it.

**A model server on this machine, without a key.** A model server that runs on this machine itself,
not in a container, and takes no key needs no key file and no change to the site file. Add its host
and a profile of its own as in step 4. On Docker Desktop the name `host.docker.internal` reaches this
machine's own ports from inside a container. `<port>` is the port the model server listens on.

```text
hosts:
  host.docker.internal: internal               # a model server on this machine, reached from the container

profiles:
  local-model:                                 # a model server on this machine, plain http, no key
    dialect: openai_chat
    base_url: http://host.docker.internal:<port>/v1
    model: <model>
    data_classes: [synthetic, golden, schema_summary, aggregates, production_tokenised]
    environments: [dev, test, eval, staging, prod]
    capabilities: {tool_calling: true}
```

Write `llm_profile: local-model` in the three lines of step 5, then do steps 6 to 9. Tried with the
practice model run as a program on this machine: the probe printed
`profile local-model: tool_roundtrip=pass, usage_cached_tokens=fail`, and the chat answered. Not
verified here: Docker on Linux, where `host.docker.internal` is not defined unless the installation
adds it. DeltaSleuth sends a key only over https, so a server that wants a key needs https too.

**Switching the chat off again.** Put the `# ` back before the three lines of step 5 and restart the
app with the command of step 7. The health address then says `"ai": false`. The key file, the site
file and the practice model may stay.

**Stopping and removing the practice model.** The practice model is not part of the installation:
`docker compose` commands do not list it, stop it or remove it. These three commands stop it, start
it again, and remove it for good:

```bash
docker stop deltasleuth-practice-model
```

```bash
docker start deltasleuth-practice-model
```

```bash
docker rm -f deltasleuth-practice-model
```

Each prints the container's name. While the practice model is stopped the chat stores
`No answer: the AI call was refused or failed (Connection: connection failed).` for every question.
Not verified here: a restart of Docker or of the machine. The command of step 2 asks Docker for no
restart, so expect the practice model to be stopped afterwards, and start it with the second command.

Remove the practice model before you remove the installation (chapter 1, section 8); "If something
goes wrong" says what happens otherwise. Its program is one file, which
`rm .deltasleuth/practice-model.py` deletes.

### How to read the result

**The probe's line** has one verdict for each of two checks: `pass`, `fail` or `unsupported`.

- `tool_roundtrip` asks the model to call a tool, hands it the tool's answer, and expects that answer
  back. The chat cannot work without it.
- `usage_cached_tokens` looks whether the endpoint reports how many tokens it took from a cache. It
  matters only for a profile whose `capabilities` declare `prompt_caching`. The profiles of steps 4
  and 10 do not, so `fail` there is harmless: it only says the endpoint reported no such count. The
  practice model reports none unless told to; told to, the line read `usage_cached_tokens=pass`.

The probe is a check and changes nothing. If a capability you declared comes back as `fail` or
`unsupported`, remove it from the profile and probe again.

**A passing probe is not the whole story.** Named with `--profile <id>`, the probe passes for a
profile that the line `llm_profile` does not name. And the probe runs in a short-lived container of
its own, which sees a key file as soon as the site file names it; the running app sees it only after
step 19. The health address and the profile list, asked after the restart, are the check of the app
itself.

**The probe's report.** With `--out` the probe also writes what it found as a file. The container can
write only below `/var/lib/deltasleuth/p0`, the folder for handing files in and out, which lives in a
Docker volume and not in the installation's folder.

1. Probe and write the report.
   ```bash
   ds llm-probe --out /var/lib/deltasleuth/p0/probe
   ```
   You should see the verdict line, then the file's name:
   ```text
   profile practice: tool_roundtrip=pass, usage_cached_tokens=fail
   /var/lib/deltasleuth/p0/probe/llm_probe_practice.json
   ```
2. Read the file out of the volume.
   ```bash
   docker compose run --rm -T app cat /var/lib/deltasleuth/p0/probe/llm_probe_practice.json
   ```
   You should see about forty lines. Shortened, with lines joined:
   ```text
     "declared": {"max_context": null, "max_output": null, "prompt_caching": "none", "tool_calling": true},
     "effective": {"max_context": null, "max_output": null, "prompt_caching": "none", "tool_calling": true},
     "endpoint": "http://practice-model:18441/v1",
     "model_configured": "practice-1",
     "model_reported": "practice-1",
     "results": [
       {"evidence": ["call 1: stop=tool_use, get_probe_value called, arguments_valid=True",
                     "call 2: stop=end_turn, reply echoes the tool result: True"],
        "name": "tool_roundtrip", "status": "pass"},
       {"evidence": ["usage reported on 3/3 responses", "no cached-token counts in any response"],
        "name": "usage_cached_tokens", "status": "fail"}]
   ```

The file holds no prompt and no answer, only what was seen. `declared` is what the profile claims,
`probed` what the checks found, and `effective` what the chat will rely on.

**`"ai": true`** from the health address means only that the profiles file was read and that
`llm_profile` names one of its profiles. It does not say that the model answers or that the key is
right. The probe says that.

**`"trust"`** in the profile list is `internal` for a host listed under `hosts` and `external` for
every other.

### Limits

- **What may leave.** Every request to a model is labelled with a data class. The probe sends
  `synthetic` data. The chat always sends `production_tokenised`: the run's masked results, and the
  question as it was typed. A profile on your own network serves the chat if its host is listed
  `internal`, its `data_classes` list `production_tokenised`, and its `environments` list the value
  of `llm_environment`. A host that is not listed is an outside host, with the stricter rules of
  section 2. A request that fails a rule is refused before anything is sent.
- **What the practice model received** for a chat question: the model's name, a fixed instruction
  text, the earlier questions and answers of the conversation, the new question under a line that
  names the run and, in a conversation about a key, the masked key, the results of the tools called
  for this question, and the description of the tools. The request also said `store: false`, which
  asks the endpoint not to keep it. Masked is not the same as empty: the values that the spec declares
  clear, such as a currency or a status, travel as they are.
- A profile with a key (`auth_ref`) must use https. Plain http is for an endpoint without a key.
- The settings are read at start. A change to `llm_profiles.yaml` or `deltasleuth.yaml` needs
  `docker compose restart app`; a change to the site file needs `docker compose --profile mocks up -d`.
- The line `budgets` of a profile bounds one question: at most 8 tool calls and 60 seconds, as the
  example has it.
- Not verified here: any real model, the Anthropic way of speaking to a model
  (`dialect: anthropic_messages`), and the limits of a profile under load.

### If something goes wrong

Each line was met or provoked here. The probe prints it as one line that starts `error:`.

**`error: AI is off: the settings name no llm_profiles_file`**
The three lines of step 5 still start with `# `.

**`error: llm_profile 'practise' is not a profile of /etc/deltasleuth/llm_profiles.yaml`**
`llm_profile` in the settings file names a profile the profiles file does not have, here a mistyped
`practise`. Every other `ds` command stops with this line too, and an app restarted with it does not
stay up: `docker compose ps` shows its status as `Restarting`, and curl says `Failed to connect`.
Correct the name and restart the app. Two things hide the mistake. The probe without an option says
it in other words, `error: /profiles/practise: no such profile`. And the probe named with the right
profile, `ds llm-probe --profile practice`, still passes, because it does not read that line.

**`error: /profiles/nosuch: no such profile`**
The probe's `--profile` names a profile the file does not have.

**`error: /profiles/practice/model: Field required`**
A member of the profile is missing or misspelt, here `model`. The line names the place.

**`error: environment must be one of ('dev', 'test', 'eval', 'staging', 'prod')`**
`--environment` or `llm_environment` holds another word.

**`stamina.retry_scheduled`**, twice, then **`error: Connection: the baseline request (plain text, no
optional features) failed, so no feature was probed; check base_url and model, and for a strict
OpenAI-compatible gateway the profile's wire options`**
No connection was made. The line does not say why. It was seen in two cases. In the first the
endpoint was not running: for the practice model, `docker start deltasleuth-practice-model`. In the
second the endpoint's certificate came from an authority the image does not know: step 22 tells the
two apart, and steps 23 to 28 are the cure. The chat shows the same trouble as
`No answer: the AI call was refused or failed (Connection: connection failed).`

**`error: PolicyDenied: profile practice (external, host practice-model) may not receive [synthetic]
in prod: external_environment`**
The host of `base_url` is not listed under `hosts`, so it counts as outside your network, and an
outside host is never used in `prod`. List the host as `internal` if it is yours, written exactly as
in `base_url`, in lower case and without a port.

**`error: /hosts/practice-model:18441: String should match pattern ...`**, in full:

```text
error: /hosts/practice-model:18441: String should match pattern '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$'
```

A name under `hosts` holds a port or a capital letter. Write the host name alone, in lower case.

**`error: cannot read secret file /run/secrets/llm_key: No such file or directory`** from the probe
The site file does not show the key to the container: do step 15. The chat answers the same text
with status 503 when the app was started before the key was mounted, or after the key file was
deleted and written anew: after step 15 run step 19, and after replacing the key file run
`docker compose restart app`. Nothing is stored for a question refused this way.

**`failed to parse <folder>/deploy/docker-compose.site.yml: line 6: mapping key "secrets" already
defined at line 5`**, or the same with `"volumes"`
The site file holds two lines `secrets:`, or two lines `volumes:`, under `app:`. Keep one, with every
name inside its brackets: steps 15 and 26.

**`permission denied: deploy/secrets/llm_key`** (zsh) or **`bash: deploy/secrets/llm_key: Permission
denied`** (bash) from step 11
The file exists and is read-only, and the command left it unchanged. To replace a key, delete the file
with `rm -f deploy/secrets/llm_key`, do steps 11 to 14 again, and restart the app.

**`error: /profiles/practice/base_url: https is required when auth_ref is set`**
The profile has a key and an `http://` address. Use the endpoint's https address, or remove
`auth_ref` if the endpoint takes no key. The setting `allow_insecure_localhost` does not help in
Docker: it is refused with **`error: /profiles/practice/allow_insecure_localhost: only loopback hosts
may opt out of https`**, and inside the container the loopback is the container itself.

**`error: Auth: authentication failed (status=401, code=invalid_api_key)`**
The endpoint refused the key. Replace the key file as described above. The chat shows
`No answer: the AI call was refused or failed (Auth: authentication failed (status=401,
code=invalid_api_key)).`

**`error: BadRequest: the baseline request (plain text, no optional features) failed, so no feature
was probed; check base_url and model, and for a strict OpenAI-compatible gateway the profile's wire
options (status=400, code=unknown_parameter)`**
The endpoint answered 400. Provoked with the practice model told to refuse request fields it does not
know, as some gateways do. Add this line to the profile, at the same depth as `capabilities`, and
probe again; it then passed.

```text
    wire: {max_tokens_param: max_tokens, send_store: false, send_parallel_tool_calls: false}
```

**`error: Read-only file system: /app/probe`**
`--out` named a folder the container cannot write. Use a folder below `/var/lib/deltasleuth/p0`.

**`wc -l` in step 1 shows another number than 155**
The paste was cut short. Delete the file with `rm .deltasleuth/practice-model.py` and do step 1
again.

**`docker: Error response from daemon: invalid mount config for type "bind": bind source path does
not exist: ...`** from step 2
The program's file is not there: do step 1 first, in the installation's folder.

**`docker: Error response from daemon: Conflict. The container name "/deltasleuth-practice-model" is
already in use by container "..."`** from step 2
The practice model exists already. If it is stopped, start it with
`docker start deltasleuth-practice-model`. To make it anew, remove it first with
`docker rm -f deltasleuth-practice-model`.

**`docker: Error response from daemon: failed to set up container networking: network
deltasleuth_default not found`** from step 2
The command names another project than this installation's. A container that cannot start is left
behind: remove it with `docker rm -f deltasleuth-practice-model`, then run step 2 with your project
name in the three places.

**` Network deltasleuth_default  Resource is still in use`** from `docker compose down -v` (chapter 1,
section 8)
The practice model still stands on the installation's network, so Docker kept the network. The
containers and the data of the installation were deleted as asked. Remove the practice model, then
the network. Each command prints the name it removed.

```bash
docker rm -f deltasleuth-practice-model
```

```bash
docker network rm deltasleuth_default
```

## 2. Use a hosted model, on a test installation only

### What it is for

A hosted model, such as one of Anthropic or OpenAI, is quick to try because there is nothing to run
yourself. Its host is outside your network, so DeltaSleuth gives it the chat's data only after three
deliberate settings and never in an installation that calls itself `staging` or `prod`. This is for
trying the chat on practice data, by whoever administers the installation.

### Before you start

- Your organisation's agreement. The chat's data is masked, but it still describes the records that
  were compared.
- A key of the hosted service, and a way out to the internet for the app container.
- An installation for trying things: `llm_environment` must be `dev`, `test` or `eval`.
- Section 1 read once: the files and the restart are the same.

Without a hosted service you cannot do steps 1 to 8. "Trying the rules without a hosted service",
after them, shows the same rules at work on the practice model.

### In the web UI

The web UI does not offer this. As in section 1, it only shows "AI is off" or the "Ask AI" button.

### On the command line

The example file `deploy/config/llm_profiles.yaml` already holds two profiles for hosted models,
`claude` and `openai`. The steps use `openai`.

Not verified here: a call to a hosted service. The outputs of steps 5 to 8 came from the practice
model in the hosted service's place: served over https with a key of its own, and with its host left
out of `hosts`.

1. In `deploy/config/llm_profiles.yaml`, in the profile `openai`, write the model your account offers
   and add `production_tokenised` to its `data_classes`. Do not list its host under `hosts`.
   ```text
     openai:
       dialect: openai_chat
       base_url: https://api.openai.com/v1
       model: <model>
       auth_ref: file:/run/secrets/openai_key
       data_classes: [synthetic, golden, schema_summary, production_tokenised]
       environments: [dev, test, eval]
   ```
2. Put the key into `deploy/secrets/openai_key` as in section 1, steps 11 to 14, with that file name.
3. In `deploy/docker-compose.site.yml`, show the file to the app container as in section 1, step 15:
   name it in the brackets of the line `secrets:` under `app:`, and give it a line under `secrets:`
   near the end. If the key of section 1 is there too, both names stand in the brackets and both
   lines under `secrets:`.
   ```text
       secrets: [llm_key, openai_key]
   ```
   ```text
   secrets:
     llm_key: {file: ./secrets/llm_key}
     openai_key: {file: ./secrets/openai_key}
   ```
   Check the file. The command prints nothing when the file is in order.
   ```bash
   docker compose config --quiet
   ```
4. In `deploy/config/deltasleuth.yaml`, name the profile and the environment, and approve the chat's
   data class for outside hosts with a fourth line.
   ```text
   llm_profiles_file: llm_profiles.yaml
   llm_profile: openai
   llm_environment: test
   llm_external_data_classes: [production_tokenised]
   ```
5. Probe the profile. The probe takes the profile and the environment from the settings.
   ```bash
   ds llm-probe
   ```
   You should see, as in section 1:
   ```text
   profile openai: tool_roundtrip=pass, usage_cached_tokens=fail
   ```
6. Apply the site file and start the app on the new settings.
   ```bash
   docker compose --profile mocks up -d
   ```
   You should see the app made anew, as in section 1, step 19.
7. Wait five seconds, then ask the app.
   ```bash
   curl -fsS http://localhost:8000/v1/health
   ```
   You should see `"ai": true`:
   ```text
   {"status": "ok", "ai": true, "auth": "tokens"}
   ```
8. List the profiles the app loaded.
   ```bash
   dscurl first-run http://localhost:8000/v1/llm/profiles
   ```
   You should see `"default": "openai"`, and that profile with your model and `"trust": "external"`:
   ```text
   {"default": "openai", "items": [..., {"profile_id": "claude", "dialect": "anthropic_messages", "model": "claude-opus-5", "trust": "external"}, {"profile_id": "openai", "dialect": "openai_chat", "model": "<model>", "trust": "external"}]}
   ```

The chat then answers as section 3 shows. Not verified here: the profile `claude`, whose key file is
`anthropic_key`, beyond the message for its missing key.

**Trying the rules without a hosted service.** The practice model of section 1 becomes an outside
host as soon as its line is taken out of `hosts`. Do section 3, steps 1 and 2, first: this exercise
asks that question again, in the same conversation, after each change. `<session id>` is the id of
that conversation.

9. In `deploy/config/llm_profiles.yaml`, put `# ` before the line of the practice model under
   `hosts:`.
   ```text
   #   practice-model: internal                     # the practice model of the guide, chapter 10
   ```
   Restart the app.
   ```bash
   docker compose restart app
   ```
   Wait five seconds, then ask.
   ```bash
   dscurl first-run -H 'Content-Type: application/json' -d '{"content": "Which keys differ and why?"}' http://localhost:8000/v1/chat/sessions/<session id>/messages
   ```
   The last message of the output is a refusal, because the settings of section 1 say `prod`:
   ```text
   No answer: the AI call was refused or failed (PolicyDenied: profile practice (external, host practice-model) may not receive [production_tokenised] in prod: external_environment).
   ```
10. In `deploy/config/deltasleuth.yaml`, write `llm_environment: test`. Run the two commands of step 9
    again. Now the data class is refused:
    ```text
    No answer: the AI call was refused or failed (PolicyDenied: profile practice (external, host practice-model) may not receive [production_tokenised] in test: external_class).
    ```
    The probe does not warn of this. Its made-up data may always go to an outside host in `test`, so
    at this point it passes:
    ```bash
    ds llm-probe
    ```
    ```text
    profile practice: tool_roundtrip=pass, usage_cached_tokens=fail
    ```
11. Add the fourth line of step 4, `llm_external_data_classes: [production_tokenised]`, under the
    three. Run the two commands of step 9 again. The practice model answers as in section 3.
12. Undo the exercise: remove the `# ` of step 9, write `llm_environment: prod` again, remove the
    fourth line, and restart the app with the first command of step 9.

### How to read the result

The probe's line reads as in section 1. The example profile `openai` declares
`prompt_caching: automatic`, so here a `fail` for `usage_cached_tokens` means the endpoint did not
report what the profile claims: remove `prompt_caching: automatic` from the profile's `capabilities`
if you see it against the real service.

### Limits

- An outside host serves the chat only in `dev`, `test` and `eval`. `staging` and `prod` are refused
  whatever the files say.
- Three things must all be there: `production_tokenised` in the profile's `data_classes`, the
  environment in the profile's `environments`, and `llm_external_data_classes` in the settings.
- The questions are sent as typed, and what the hosted service keeps of a request is its own matter.

### If something goes wrong

A refused question is stored as an answer that starts `No answer: the AI call was refused or failed`
and gives the reason in brackets. Each of these four was provoked with the profile `openai` and the
practice model as the outside host; `<model host>` stands for its host name.

**`PolicyDenied: profile openai (external, host <model host>) may not receive [production_tokenised]
in test: class_not_allowed`**
The profile's `data_classes` do not list `production_tokenised`: step 1.

**`... in test: external_class`**
The settings do not approve that class for outside hosts: the fourth line of step 4.

**`... in prod: environment_not_allowed`**
The profile's `environments` do not list the value of `llm_environment`.

**`... in prod: external_environment`**, also with `staging`
The installation calls itself `prod` or `staging`, where no outside host is used, even when the
profile lists that environment. The probe says the same of its own data:
`error: PolicyDenied: profile openai (external, host <model host>) may not receive [synthetic] in
staging: external_environment`.

**`error: cannot read secret file /run/secrets/anthropic_key: No such file or directory`**
Met by probing the profile `claude` without its key file. Create the file the profile's `auth_ref`
names and mount it as in steps 2 and 3.

## 3. Ask about a run or a key

### What it is for

Reading a run means reading tables of outcomes, root causes and findings. The chat lets a reader ask
in words, "which keys differ and why", and get sentences that point back into those tables. It is for
anyone who may read results: the role `viewer` is enough.

### Before you start

- The chat is on (section 1): the health address says `"ai": true`.
- A finished run. Chapter 3 makes one. If you have none, these two commands store the spec of the
  customer mocks and start a run of its 40 keys:
  ```bash
  dscurl first-run -H 'Content-Type: application/yaml' --data-binary @mocks/customer/spec.yaml http://localhost:8000/v1/specs
  ```
  You should see the stored spec's id:
  ```text
  {"spec_id": 1, "name": "mock-customer-v1-vs-v2", "version": "1.0.0", "content_hash": "dadc627b...", "needs": ["clear", "known_diffs"], "approval_id": null}
  ```
  ```bash
  dscurl first-run -H 'Content-Type: application/json' -d '{"spec_id": 1, "mode": "batch"}' http://localhost:8000/v1/runs
  ```
  You should see the run's id. The run ends within a few seconds.
  ```text
  {"run_id": "R-35T7EGYX442G9KT2QMW4M0NN0S", "warnings": []}
  ```
- The run's id, written `<run id>` below. `ds runs` lists the newest runs, each with its id first:
  ```bash
  ds runs
  ```
  ```text
  R-35T7EGYX442G9KT2QMW4M0NN0S  mock-customer-v1-vs-v2 1.0.0  batch  completed  cycle 1  different 4 of 40
    Unapproved spec version: clear, known_diffs were not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
  ```

### In the web UI

1. Sign in as in chapter 1, section 4, and click "Runs" in the bar at the top. You see a page headed
   "Runs" with a table of runs.
2. In the column "Run", click the id of a finished run. You see a page headed "Run", followed by the
   id and the run's status, with the sections "Root causes", "Results" and "Findings".
3. Look at the bottom right corner of the window. The button "Ask AI about this run" stays there while
   you scroll. If the grey label "AI is off" stands there instead, the chat is not switched on.
4. Click "Ask AI about this run". A panel opens at the right edge. It shows the title "Chat about run"
   with the run's id, the buttons "New" and "Close", the line "AI-generated, not evidence: check what
   it cites.", an empty box that says "Ask about these results. Questions are kept and sent as typed:
   use masked keys, no personal data.", and the button "Ask".
5. Type a question, for example `Which keys differ and why?`, and click "Ask". The Enter key only
   starts a new line. Your question appears in the panel and the button reads "Thinking…" until the
   answer is there.
6. Read the answer under your question. Each id the answer cites is a small rounded label.
7. Click a label.
   - One that starts with `K:` opens the page of that key, headed "Key" with the masked key.
   - One that starts with `RC-` shows the run's page with the "Findings" table narrowed to that root
     cause. The other two tables, "Root causes" and "Results", stay as they were. The box
     "root_cause_id" above the tables holds the id, and the link "clear" beside the button "Filter"
     shows everything again.
   - One that starts with `F-` scrolls to that row of the "Findings" table and outlines it for a
     moment, if the page you are on shows that row. One that starts with `O-` does the same in the
     "Observations" table, which only a key's page has. Otherwise nothing happens.
8. To ask about one key, click a key in the column "Key" of the "Results" table. On the key's page
   the button reads "Ask AI about this key", and the panel's title is "Chat about key" with the masked
   key. This is a conversation of its own.
9. "New" empties the panel; your next question starts a new conversation. "Close" hides the panel, and
   the conversation is there again when you open it in the same browser tab.
10. For a draft without typing a question, go back to the run's page and its section "Root causes".
    The button "Summarise this run" stands beside the heading, and each row ends with the button
    "Explain". Click one. It reads "Thinking…", and all of them are grey until the answer is there.
    Each click starts a conversation of its own and asks it one question that DeltaSleuth wrote.
11. Read the panel under the table, headed "AI draft: root cause" with the id, or "AI draft: run
    summary". Under "Engine facts (the comparison decided these, not the AI)" it repeats what the
    comparison stored: the root cause's row, or the run's status, counters and number of root causes.
    Check each sentence of the draft against them. In a draft, a label that starts with `F-` or `O-`
    opens the key's page and outlines that row for a moment.
12. A panel headed "No AI draft" is a refusal: the facts, then the reason where the draft would be.
    The practice model names the first root cause whatever it is asked, so "Explain" in any other
    row ends as `No verified answer: ... (the answer does not cite [RC-...], the root cause asked
    about).` "Ask again" asks anew, and "Close" removes the panel.

A refusal appears as a line above the panel's box, for example the 503 text of section 1.

### On the command line

`ds` has no chat command. The requests below are the ones the panel sends. The answers printed are
the practice model's.

1. Start a conversation about a run. The answer is the conversation's id, written `<session id>`
   below.
   ```bash
   dscurl first-run -H 'Content-Type: application/json' -d '{"run_id": "<run id>"}' http://localhost:8000/v1/chat/sessions
   ```
   You should see:
   ```text
   {"session_id": "S-ceef2d40873d79eb"}
   ```
2. Ask a question.
   ```bash
   dscurl first-run -H 'Content-Type: application/json' -d '{"content": "Which keys differ and why?"}' http://localhost:8000/v1/chat/sessions/<session id>/messages
   ```
   You should see the whole conversation in one line. Laid out and shortened:
   ```text
   {"session_id": "S-ceef2d40873d79eb", "run_id": "R-35T7EGYX442G9KT2QMW4M0NN0S", "key_masked": null, ..., "created_by": "token:T-7986935c349d2a1a", "messages": [
    {"seq": 1, "role": "user", "citations": [], ..., "content": "Which keys differ and why?", "tool_calls": []},
    {"seq": 2, "role": "tool", ..., "content": "{\"tool\": \"get_run_summary\", \"data\": {\"run_id\": ..., \"status\": \"completed\", ...}}", "tool_calls": [{"id": "call_841795631", "name": "get_run_summary", "arguments": {}}]},
    {"seq": 3, "role": "tool", ..., "content": "{\"tool\": \"get_findings\", \"data\": {\"findings\": [{\"finding_id\": \"F-142e23cf4bd2f996\", ...}], \"more\": false}}", "tool_calls": [{"id": "call_850696131", "name": "get_findings", "arguments": {"root_cause_id": "RC-4b68f1b25392", "limit": 5}}]},
    {"seq": 4, "role": "assistant", "citations": ["RC-4b68f1b25392", "K:tok:identifier:v1:KB7XB764PVDAZMIHUMMHBVQY33", "F-142e23cf4bd2f996"], ..., "content": "The run is completed: of 40 keys, 4 differ. The first root cause is [RC-4b68f1b25392]: VALUE_MISMATCH at $.consents[*].granted, severity CRITICAL, on 1 key(s), for example [K:tok:identifier:v1:KB7XB764PVDAZMIHUMMHBVQY33]. Finding [F-142e23cf4bd2f996] shows it: side A holds true and side B holds false.", "tool_calls": []}]}
   ```
3. With the practice model, see what DeltaSleuth asked of it. Its log has one line for each request,
   with the tools it was offered and what it did.
   ```bash
   docker logs --tail 3 deltasleuth-practice-model
   ```
   You should see three requests for the one question: two tool calls, then the answer.
   ```text
   20:35:10 POST /v1/chat/completions 200 model=practice-1 tools offered: get_run_summary,get_findings,get_observations,compare_key; calls get_run_summary
   20:35:10 POST /v1/chat/completions 200 model=practice-1 tools offered: get_run_summary,get_findings,get_observations,compare_key; calls get_findings
   20:35:10 POST /v1/chat/completions 200 model=practice-1 tools offered: get_run_summary,get_findings,get_observations,compare_key; answers
   ```
4. Read the conversation again later.
   ```bash
   dscurl first-run http://localhost:8000/v1/chat/sessions/<session id>
   ```
   You should see the same line as in step 2, with every question asked since.
5. Start a conversation about one key. `<masked key>` is a key as an answer cites it, without the
   `K:` in front, for example `tok:identifier:v1:KB7XB764PVDAZMIHUMMHBVQY33`.
   ```bash
   dscurl first-run -H 'Content-Type: application/json' -d '{"run_id": "<run id>", "key": "<masked key>"}' http://localhost:8000/v1/chat/sessions
   ```
   You should see a new id:
   ```text
   {"session_id": "S-91c09b53575e0494"}
   ```
6. Ask about the key, with the id of step 5.
   ```bash
   dscurl first-run -H 'Content-Type: application/json' -d '{"content": "What differs on this key?"}' http://localhost:8000/v1/chat/sessions/<session id>/messages
   ```
   The practice model called the tool `get_observations` and answered:
   ```text
   Key [K:tok:identifier:v1:KB7XB764PVDAZMIHUMMHBVQY33] ended as MISMATCH (rechecked). Its first finding is [F-142e23cf4bd2f996]: VALUE_MISMATCH at $.consents[*].granted, severity CRITICAL. It was read as [O-952b10549c1402e6] on side A and [O-82e404fce00abac4] on side B.
   ```
7. See the check withhold an answer. The practice model answers every question that holds the word
   `guess` with the sentence "About 99 keys differ.", which cites nothing and states a number no tool
   returned. Ask in the conversation of step 1.
   ```bash
   dscurl first-run -H 'Content-Type: application/json' -d '{"content": "Just guess: how many keys differ?"}' http://localhost:8000/v1/chat/sessions/<session id>/messages
   ```
   The last message of the output holds this in the place of the model's sentence:
   ```text
   No verified answer: the model's reply could not be checked against this session's tool results (99 appears in no tool result; the answer cites no tool result).
   ```
8. See what the chat left in the audit log.
   ```bash
   ds audit --action chat.ask --limit 3
   ```
   You should see a copy of the row for your own reading, then one row for each question, newest
   first:
   ```text
   {"id": 20, ... "action": "audit.read", ...}
   19  2026-10-05T20:35:11+00:00  token:T-7986935c349d2a1a (first-run)  chat.ask  S-ceef2d40873d79eb  {}  request Q-7f981a3670a8fcb8  from 172.30.0.1
   18  2026-10-05T20:35:11+00:00  token:T-7986935c349d2a1a (first-run)  chat.ask  S-91c09b53575e0494  {}  request Q-1df03f67000b9c8c  from 172.30.0.1
   16  2026-10-05T20:35:10+00:00  token:T-7986935c349d2a1a (first-run)  chat.ask  S-ceef2d40873d79eb  {}  request Q-ddb32db9b86f73bf  from 172.30.0.1
   ```
9. Fetch the run's PDF report, which prints the answers.
   ```bash
   dscurl first-run -o report.pdf http://localhost:8000/v1/runs/<run id>/reports/report.pdf
   ```
   It prints nothing and writes the file `report.pdf` into the folder you are in. Open it with a PDF
   viewer and go to its last part. Delete the file when you have read it:
   ```bash
   rm report.pdf
   ```
10. Ask for a draft, as the buttons of step 10 above do. Without `root_cause_id` it summarises the run.
    ```bash
    dscurl first-run -H 'Content-Type: application/json' -d '{"run_id": "<run id>", "root_cause_id": "<RC id>"}' http://localhost:8000/v1/chat/sessions/explain
    ```
    You should see a new conversation, laid out as in step 2, whose last message is the draft.

### How to read the result

**The citations.** An answer names what it used in square brackets, one id in each:

| Form | Is |
|---|---|
| `[RC-...]` | A root cause of the run |
| `[F-...]` | A finding |
| `[O-...]` | An observation: one stored read of one side for one key |
| `[K:...]` | A key, in its masked form. The `K:` belongs to the citation, not to the key |

The member `citations` of the answer lists them in the order of first use.

**The messages.** `role` is `user` for your question, `assistant` for the answer, and `tool` for each
lookup the model asked for: `tool_calls` names the tool and its arguments, and `content` holds what
DeltaSleuth returned. The tool messages are what the answer was checked against. The panel in the web
UI hides them; this request shows them.

**The check.** Before an answer is shown, DeltaSleuth checks three things: the answer cites at least
one id; every cited id was returned by a tool in this conversation; and every number written in
digits stands in a tool result. A count written short, such as `2.4M`, `37k`, `1e6` or `1.250.000`,
is refused as written, unless a tool result shows that very text. An answer that fails gets one
chance: the model is told what was wrong and answers again. If that fails too, the answer is withheld
and the line of step 7 stands in its place. That line holds nothing the model wrote between
brackets: a cited id that no tool returned is named by its kind, `a [K:...] citation`, unless it
has the form of a stored id. The check does not read the sentences for sense. It shows that the ids
and the numbers are real, not that the explanation is right, which is why the panel says
"AI-generated, not evidence: check what it cites."

**`No answer: the AI call was refused or failed (...)`** means the model was not reached or the
request was refused before it was sent. The reason is in the brackets; sections 1 and 2 list them.

**In the report.** The last part of the PDF of step 9 is headed "8. Annex: AI-generated, not
evidence". It prints every answer about the run, the withheld and the failed ones too, each under a
line with its time, `run` or the masked key it was about, and the conversation's id, and each
followed by a line "Cites:" with its ids. The questions are not printed. This is what the annex held
for step 2, read here with the program `pdftotext`:

```text
2026-10-05 20:35 · run · S-ceef2d40873d79eb:
The run is completed: of 40 keys, 4 differ. The first root cause is [RC-4b68f1b25392]: VALUE_MISMATCH at $.consents[*].granted, severity CRITICAL, on
1 key(s), for example [K:tok:identifier:v1:KB7XB764PVDAZMIHUMMHBVQY33]. Finding [F-142e23cf4bd2f996] shows it: side A holds true and side B holds
false.
Cites: RC-4b68f1b25392, K:tok:identifier:v1:KB7XB764PVDAZMIHUMMHBVQY33, F-142e23cf4bd2f996
```

In the web UI the same file comes from the button "report.pdf" on the run's page
([chapter 3](03-first-comparison.md), "Download the reports").

### Limits

What the chat never does:

- **It decides nothing and changes no result.** Outcomes, findings and root causes come from the
  comparison engine. The model reads them through three tools: `get_run_summary`, `get_findings` and
  `get_observations`.
- **It never leaves its run.** A conversation belongs to one run, the tools read that run only, and
  the answer can cite only what those tools returned.
- **For a viewer it never calls your services.** A fourth tool, `compare_key`, compares one key again
  now, which reads both sides and stores a run of one key. The model is offered it only when the
  person asking holds the role `operator` or higher: the practice model's log showed three tools
  offered for a question asked with a `viewer` token, and four with the `first-run` token. Asked
  "Please compare this key again now." with the `first-run` token in the conversation of step 5, it
  called `compare_key`, a run of mode `one_shot` appeared in the run list, and the audit log holds a
  `compare_one` row whose detail says `"via": "chat:S-91c09b53575e0494"`.
- **It takes no raw key.** A conversation about a key is opened with the masked key; a raw one is
  answered 404, `key C00012019 not found`.
- **It does not mask your question.** Questions are stored and sent to the model as typed. Refer to a
  record by its masked key and type no personal data.
- **It shows no one else's conversation.** A conversation can be read and continued only by the token
  or person that started it; anyone else is answered 404. Its answers are still printed in the run's
  PDF report, which every reader of the report sees: the file fetched with the `first-run` token held
  the answer to a question asked with another token.

Other limits:

- The web UI does not show which model answers, does not show the tool calls, and keeps the
  conversation only for the browser tab: no page and no request lists earlier conversations.
- The answer arrives whole, not word by word.
- A draft reads the run's current cycle only. A summary needs at least one root cause and reads the
  first 10. The two buttons stand on the run's page alone: not on a key's page, not in `ds`, not over
  MCP. The model is never offered `compare_key` for a draft, whoever asks.
- A draft passes the same check, ids and numbers, never the sense: a wrong count passes when the same
  number stands anywhere in what the tools returned for it. On a small run, where every count is 1 to
  3, the number check protects nothing. A number written in words, such as "two million", or with
  another abbreviation, such as `5MM`, is not read at all. The facts beside the draft are your check.
- Every click, "Ask again" too, adds one entry to the annex of `report.pdf` for every reader. The page
  finds a draft again only in the same browser tab, and not after a reload during "Thinking…".
- Once the run's stored evidence is deleted, at the end of its retention or by an erasure, the chat
  about it is closed and its conversations are gone.
- Not verified here: everything a real model does better or worse than the practice model, an answer
  that passes after its second chance, and a reply that the model cuts off or refuses. The two
  questions behind the buttons are English and were never put to a real model.

### If something goes wrong

**`{"detail": "AI disabled"}`**, status 503
The chat is off: section 1.

**`{"detail": "cannot read secret file /run/secrets/llm_key: No such file or directory"}`**, status 503
The app does not see the key file: section 1, "If something goes wrong". The question was not stored.

**An answer that starts `No answer: the AI call was refused or failed`**
The model was not reached, or the request was refused. The brackets hold one of the reasons of
sections 1 and 2. The question and this answer are stored.

**An answer that starts `No verified answer:`**
The model's reply failed the check twice. Ask again, or ask more narrowly, for example about one key.

**A panel "No AI draft" whose text starts `No answer: the AI call was refused or failed`**
A draft needs at least three calls to the model within the profile's `max_wall_s`, 60 seconds in the
example. If the brackets name `max_wall_s` or a timeout, raise that number in the line `budgets` of
the profile (section 1, step 10) and restart the app.

**`{"detail": "key K:tok:identifier:v1:KB7XB764PVDAZMIHUMMHBVQY33 not found"}`**, status 404
The key in step 5 was copied with the `K:` of its citation. Leave the `K:` out.

**`{"detail": "run R-NOSUCH not found"}`** or **`{"detail": "key C00012019 not found"}`**, status 404
The run's id is wrong, or the key is not a masked key of that run.

**`{"detail": "chat session S-ceef2d40873d79eb not found"}`**, status 404
One of three things. The id is wrong. Or the conversation was started with another token: a
conversation is its creator's. Or the run's evidence was deleted since, which deletes its
conversations with it.

**`{"detail": "the run's evidence was purged on 2026-10-05; AI answers need it"}`**, status 410
You tried to start a new conversation about a run whose evidence is deleted. Provoked by erasing a
run with `ds purge --run <run id> --reason "<why>"`; without `--reason` that command answers
`error: --run needs --reason (an erasure is audited)`. A conversation started before the erasure is
answered 404, as above. In the web UI the panel then shows "The run's evidence was purged; AI answers
need it." and takes no question.

**`{"detail": "invalid request", "issues": [{"pointer": "/body/content", "message": "String should
have at least 1 character", "code": "string_too_short"}]}`**, status 422
The question was empty.

**`{"detail": "not signed in"}`**, status 401
The request carried no token: use `dscurl` with a token's name.

## Part two: the MCP server for your own agent

Sections 4 to 7. The model here belongs to your agent, and DeltaSleuth never calls it. These sections
need no model endpoint and no practice model.

## 4. Switch the MCP server on

### What it is for

A team that already works with an AI agent, in an editor or a terminal, wants that agent to read
comparison results without copying them by hand. The MCP server gives such an agent eleven tools over
the runs this installation holds. Whoever administers the installation switches it on and decides,
in writing, what the tools may hand out.

The tools, and what each needs in the settings:

| Tool | Gives | Needs |
|---|---|---|
| `list_runs` | The stored runs, newest first: ids, spec names, modes, statuses, counters and times | `aggregates` |
| `get_run_summary` | One run: status, counters, progress and its first ten root causes | `production_tokenised` |
| `get_root_causes` | The root causes of a run, a page at a time | `production_tokenised` |
| `get_findings` | The findings of a run, all or narrowed by key, root cause, kind, path or severity | `production_tokenised` |
| `get_observations` | One key of a run: its outcome, both sides' masked documents, its findings | `production_tokenised` |
| `get_readiness` | Where every compared pair stands on the way to a cut-over | `aggregates` |
| `get_readiness_pair` | The same for one pair, with each condition's status and figures | `aggregates` |
| `list_schedules` | The schedules that start runs by the clock | `aggregates` |
| `get_schedule` | One schedule, with its next times and newest starts | `aggregates` |
| `list_prechecks` | The table pre-checks, each with counts by status and verdict | `aggregates` |
| `compare_key` | Compares one key of a run again now; it reads both sides | `production_tokenised`, the setting `work`, and the role `operator` |

What it never does: no tool starts, pauses, resumes or cancels a run, and none reads a report, an
evidence pack, a chat, a spec, a target, the audit log, a token or a user. The server sends an agent
no request of its own. DeltaSleuth checks nothing an agent writes from these results.

### Before you start

Nothing but the `ds` short form. The steps change one file, `deploy/config/deltasleuth.yaml`, and
take effect at the restart of step 3.

### In the web UI

The web UI has no page for the MCP server, by design: it is switched on in the settings file, and
`ds mcp check` is the only view of what it serves.

### On the command line

1. Open `deploy/config/deltasleuth.yaml` and remove the `# ` at the start of the last six lines. Keep
   the two spaces before the five lines under `mcp:`.
   ```text
   mcp:
     data_classes: [aggregates, production_tokenised]   # required: what tool results may carry
     http: true                                     # serve POST /v1/mcp in ds serve (ds mcp needs only the block)
     work: []                                       # [compare_key]: an operator's agent may compare one key again
     calls_per_minute: 60                           # per caller, 1..600
     concurrency: 4                                 # calls at once per process, 1..16
   ```
   `data_classes` is the written decision of what tool results may carry. It has no default.
   - `aggregates`: names, ids, states, times and counts.
   - `production_tokenised`: a run's masked data key by key, for runs whose spec declares no value
     clear.
   - `production_clear_declared`: the other runs too. Their results hold the values the spec declares
     clear and, where a spec says its keys are plain business codes, the raw keys.
2. Check the block. This command reads the settings only.
   ```bash
   ds mcp check
   ```
   You should see what is served, the tools each role is offered, each tool not served with the
   reason, and nine sentences on what MCP is not, of which the first is shown:
   ```text
   mcp: data classes aggregates, production_tokenised; work tools none; POST /v1/mcp served; 60 calls a minute per caller, 4 at once
   viewer: list_runs, get_run_summary, get_root_causes, get_findings, get_observations, get_readiness, get_readiness_pair, list_schedules, get_schedule, list_prechecks
   operator: list_runs, get_run_summary, get_root_causes, get_findings, get_observations, get_readiness, get_readiness_pair, list_schedules, get_schedule, list_prechecks
   not served: compare_key (mcp.work)
   runs whose spec declares clear values or a business-code key are not served
   DeltaSleuth checks no answer an external agent writes (only the built-in chat's answers are checked): such an answer is no run-level evidence and enters no report or pack.
   ...
   ```
3. Restart the app so that it reads the block.
   ```bash
   docker compose restart app
   ```
4. Wait five seconds, then look for the line the app prints each time it starts with the block.
   ```bash
   docker compose logs app | grep 'mcp: POST'
   ```
   You should see one line for each such start. The last one is the start that runs now:
   ```text
   app-1  | mcp: POST /v1/mcp is served; data classes aggregates, production_tokenised; work tools none
   ```

**The practice runs need the third class.** Every spec that ships with DeltaSleuth, for the mocks and
in `examples/`, declares some values clear or its keys plain business codes. So with the block as
above each tool that reads a run answers `run R-... is not served by this installation`; seen here
for a run of the customer mocks. To read the practice runs through MCP, write the third class into
the line and repeat steps 2 to 4:

```text
  data_classes: [aggregates, production_tokenised, production_clear_declared]   # required: what tool results may carry
```

`ds mcp check` then prints this warning and no longer the sentence about runs that are not served.
The warning does not travel with the other lines, so it can stand before them, among them or after
them. The app prints the same warning at its start.

```text
warning: mcp: values a spec declares clear and business-code keys leave the installation in tool results
```

The mocks hold made-up data, so nothing is lost here. On real data that line is a decision to let
those values reach the agent's model.

**The compare tool.** To serve `compare_key`, write `work: [compare_key]` and repeat steps 2 to 4.
The check then lists it for the operator only, and the start line ends `work tools compare_key`:

```text
operator: list_runs, get_run_summary, get_root_causes, get_findings, get_observations, get_readiness, get_readiness_pair, list_schedules, get_schedule, list_prechecks, compare_key
```

**Switching it off again.** Put the `# ` back before the six lines, or write `http: false`, and
restart the app. With `http: false` the check says `POST /v1/mcp off`, the app prints no start line,
and the address answers 404.

### How to read the result

The first line of `ds mcp check` repeats the block as the app will apply it. The lines `viewer:` and
`operator:` are the tools an agent sees with a token of that role. Each `not served:` line names a
tool and, in brackets, the setting that would serve it: `(mcp.work)`, or
`(mcp.data_classes lacks production_tokenised)`.

### Limits

- The block is read at start: every change needs `docker compose restart app`.
- `production_clear_declared` cannot stand without `production_tokenised`, and `compare_key` cannot
  be served without it either.
- The block bounds what the eleven tools answer. It does not bound the agent's token: section 7,
  "The stated limit".

### If something goes wrong

**`error: MCP is off: the settings have no mcp block (docs/mvp/MCP.md §4)`**
The six lines still start with `# `.

**`error: invalid settings /etc/deltasleuth/deltasleuth.yaml: mcp.data_classes: Field required`**
The line `data_classes` is missing or still commented. It has no default.

**`error: invalid settings ...: mcp.data_classes.1: Input should be 'aggregates',
'production_tokenised' or 'production_clear_declared'`**
A class is misspelt. The number counts the entries from 0.

**`error: invalid settings ...: mcp: Value error, production_clear_declared widens
production_tokenised: add it to data_classes`**, or **`... mcp: Value error, work names compare_key,
whose results are production_tokenised: add it to data_classes`**
The line lists the third class, or `work` lists `compare_key`, without `production_tokenised`.

**`error: invalid settings ...: mcp.work.0: Input should be 'compare_key'`**
`work` takes that one name only.

**`error: put the options after 'check'`**
An option stood between `mcp` and `check`.

**Step 4 prints nothing**
The app did not start with the block and `http: true`. Run step 2: it says what the settings hold.
Then restart the app and wait five seconds.

## 5. Connect an agent over HTTP

### What it is for

Over HTTP an agent reaches the MCP server at one address of the app, `/v1/mcp`, and proves who it is
with an API token of its own. This is the way for every agent that does not run on the installation's
own machine account, and the way to try the tools by hand with curl.

### Before you start

- Section 4 is done, with `http: true`, and with the third data class if you want to read the
  practice runs.
- A token for the agent, named for it, with the role `viewer`, in a file.
  [Appendix A1](A1-install-by-hand.md), section 4, explains the command; here the name is `my-agent`
  and the token lives 30 days.
  ```bash
  (umask 077; set -C; docker compose run --rm -T -e DS_OPERATOR="$(id -un)" app ds token create --name my-agent --owner "$(id -un)" --role viewer --days 30 > .deltasleuth/tokens/my-agent)
  ```
  You should see the token's id and role, but not the token:
  ```text
  {"id": 55, ... "action": "token.create", "object": "T-aace1b4c15c64e30", ...}
  token T-aace1b4c15c64e30 (my-agent), viewer, expires 2026-11-04T20:39:03+00:00; it is shown only now
  ```
- A run's id (section 3, "Before you start").

### In the web UI

The web UI does not offer this. In an installation with token sign-in it cannot create a token
either: tokens come from the command line.

### On the command line

An MCP request is a small JSON message sent with POST. Every request stands alone: the server keeps
no session, so each one carries the token.

1. Say hello. This step is optional: the server answers the later ones without it.
   ```bash
   dscurl my-agent -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}' http://localhost:8000/v1/mcp
   ```
   You should see the server's name and the instructions it gives every agent:
   ```text
   {"jsonrpc":"2.0","id":1,"result":{"capabilities":{"experimental":{},"tools":{"listChanged":false}},"instructions":"These tools read DeltaSleuth comparison runs. ... Every figure is observed: quote numbers as tool results show them. DeltaSleuth does not check what you write.","protocolVersion":"2025-06-18","serverInfo":{"name":"deltasleuth","title":"DeltaSleuth","version":"0.1.0.dev0"}}}
   ```
2. Ask which tools this token is offered.
   ```bash
   dscurl my-agent -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' http://localhost:8000/v1/mcp
   ```
   You should see ten tools, each with a description and the arguments it takes:
   ```text
   {"jsonrpc":"2.0","id":2,"result":{"tools":[{"annotations":{"readOnlyHint":true},"description":"The stored comparison runs, newest first: ...","inputSchema":{"properties":{"spec_id":{"type":"integer","minimum":1},"status":{"enum":["queued","running",...]},...,"limit":{"type":"integer","minimum":1,"maximum":12,"description":"rows, 10 unless given"},...},"required":[],"type":"object","additionalProperties":false},"name":"list_runs"},...]}}
   ```
3. Call a tool: the two newest runs.
   ```bash
   dscurl my-agent -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_runs","arguments":{"limit":2}}}' http://localhost:8000/v1/mcp
   ```
   You should see one text that is itself JSON, with `"isError":false` at the end. Here the newer of
   the two runs is the run of one key that the compare question of section 3, "Limits", left:
   ```text
   {"jsonrpc":"2.0","id":3,"result":{"content":[{"text":"{\"tool\": \"list_runs\", \"data\": {\"runs\": [{\"run_id\": \"R-35T7EJGBXAJMQMDHQ16EZ7JVHQ\", \"spec_id\": 1, \"spec_name\": \"mock-customer-v1-vs-v2\", \"spec_version\": \"1.0.0\", \"mode\": \"one_shot\", \"status\": \"completed\", ...}, {\"run_id\": \"R-35T7EGYX442G9KT2QMW4M0NN0S\", ..., \"mode\": \"batch\", \"status\": \"completed\", ...}], \"next_cursor\": null}}","type":"text"}],"isError":false}}
   ```
4. Read one run. For a practice run this needs the third data class of section 4; without it the
   answer is the refusal `run R-... is not served by this installation`, listed below.
   ```bash
   dscurl my-agent -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"get_run_summary","arguments":{"run_id":"<run id>"}}}' http://localhost:8000/v1/mcp
   ```
   You should see the run's counters and root causes:
   ```text
   {"jsonrpc":"2.0","id":4,"result":{"content":[{"text":"{\"tool\": \"get_run_summary\", \"data\": {\"run_id\": \"R-35T7EGYX442G9KT2QMW4M0NN0S\", ..., \"status\": \"completed\", \"cycle\": 1, ..., \"counters\": {..., \"total\": 40, ..., \"different\": 4, \"by_outcome\": {\"DRIFT\": 0, \"MATCH\": 35, \"MISMATCH\": 2, \"KNOWN_DIFF\": 2, ..., \"NOT_FOUND_B\": 1, ...}, ...}, ..., \"root_causes\": [{\"root_cause_id\": \"RC-4b68f1b25392\", ..., \"taxonomy_code\": \"VALUE_MISMATCH\", \"max_severity\": \"CRITICAL\", \"path_pattern\": \"$.consents[*].granted\", ..., \"sample_keys\": [\"tok:identifier:v1:KB7XB764PVDAZMIHUMMHBVQY33\"]}, ...], \"accuracy\": null, \"labels\": {\"licence\": null, \"approval\": \"unapproved\"}}}","type":"text"}],"isError":false}}
   ```
5. Read the findings of one severity.
   ```bash
   dscurl my-agent -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"get_findings","arguments":{"run_id":"<run id>","severity":"CRITICAL"}}}' http://localhost:8000/v1/mcp
   ```
   The text of the answer, laid out and shortened:
   ```text
   {"tool": "get_findings", "data": {"findings": [
     {"finding_id": "F-142e23cf4bd2f996", "root_cause_id": "RC-4b68f1b25392", "taxonomy_code": "VALUE_MISMATCH", "severity": "CRITICAL", ..., "path_pattern": "$.consents[*].granted", "json_pointer": "/consents/MARKETING/granted", "value_a": true, "value_b": false, ..., "key_masked": "tok:identifier:v1:KB7XB764PVDAZMIHUMMHBVQY33"},
     {"finding_id": "F-c1d8c38f01a8bd40", "root_cause_id": "RC-cd785916a3ea", "taxonomy_code": "MISSING_DOCUMENT", "severity": "CRITICAL", ..., "key_masked": "tok:identifier:v1:6ZKQ7ECENFLQAUSW6AL3RQELL4"}],
    "more": false, "cycle": 1}}
   ```
6. Read one key, with a masked key from an earlier answer.
   ```bash
   dscurl my-agent -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"get_observations","arguments":{"run_id":"<run id>","key":"<masked key>"}}}' http://localhost:8000/v1/mcp
   ```
   The text holds the key's outcome, both sides' masked documents, its findings and its observations:
   ```text
   {"tool": "get_observations", "data": {"key_masked": "tok:identifier:v1:KB7XB764PVDAZMIHUMMHBVQY33", "cycle": 1, "state": "done", "result": {"outcome": "MISMATCH", "confirmation": "rechecked", ..., "obs_a_id": "O-952b10549c1402e6", "obs_b_id": "O-82e404fce00abac4"}, "side_a": {"obs_id": "O-952b10549c1402e6", "state": "COMPLETE", "reason": null, "canonical": "{\"accounts\": {\"tok:account_number:v1:P36D...\": [{\"accountNo\": \"tok:account_number:v1:3TIO...\", ..., \"currency\": \"GBP\", ...}]}, ...}", "truncated": false}, "side_b": {...}, "findings": [...], "more_findings": false, "observations": [...]}}
   ```

**Wiring a desktop or editor client.** The blocks below are the runbook's, with this installation's
address. No block holds a token: the token stays in a file, or in the editor's own store for
secrets. Not verified here: Claude Code, VS Code or any other client program; the runbook says the
same of itself. The files were made and checked as shown.

For Claude Code, a small program prints the header with the token, and a file `.mcp.json` in your
project names that program. Steps 7 to 12 make the program in the folder `~/.config/deltasleuth` of
your home folder, with a copy of the token beside it. Run them in the installation's folder.

7. Make the folder and close it to every other account.
   ```bash
   mkdir -p ~/.config/deltasleuth
   ```
   ```bash
   chmod 700 ~/.config/deltasleuth
   ```
8. Copy the token's file there, readable by you alone.
   ```bash
   (umask 077; cp .deltasleuth/tokens/my-agent ~/.config/deltasleuth/mcp-token)
   ```
9. Write the program. Copy the five lines of the block together; the last one, `EOF`, must start at
   the left edge.

```bash
cat > ~/.config/deltasleuth/mcp-headers <<'EOF'
#!/bin/sh
# ~/.config/deltasleuth/mcp-headers (0700); the token alone in mcp-token (0600)
printf '{"Authorization": "Bearer %s"}\n' "$(cat "$HOME/.config/deltasleuth/mcp-token")"
EOF
```

10. Let yourself, and nobody else, run it.
    ```bash
    chmod 700 ~/.config/deltasleuth/mcp-headers
    ```
11. Check that the program prints one header with a token, without showing the token.
    ```bash
    ~/.config/deltasleuth/mcp-headers | grep -c '^{"Authorization": "Bearer dst_'
    ```
    You should see:
    ```text
    1
    ```
12. In your project's folder, write the file `.mcp.json` with a text editor. `<home>` is your home
    folder, written out in full; `echo ~` prints it.
    ```json
    {"mcpServers": {"deltasleuth": {"type": "http", "url": "http://localhost:8000/v1/mcp",
      "headersHelper": "<home>/.config/deltasleuth/mcp-headers"}}}
    ```

For VS Code, a file `.vscode/mcp.json`. The editor asks for the token once and keeps it in its store
for secrets, so steps 7 to 11 are not needed:

```json
{"inputs": [{"type": "promptString", "id": "ds-token", "description": "DeltaSleuth API token", "password": true}],
 "servers": {"deltasleuth": {"type": "http", "url": "http://localhost:8000/v1/mcp",
  "headers": {"Authorization": "Bearer ${input:ds-token}"}}}}
```

### How to read the result

An answer has two layers. The outer one is the protocol's: `result.content[0].text` holds a text, and
`result.isError` says whether the tool refused. The text is DeltaSleuth's own JSON, with its quotes
escaped as `\"`: either `{"tool": ..., "data": ...}` or `{"tool": ..., "error": ...}`.

- `cycle`, in every answer about a run, is the cycle that was read: a run that repeats has several,
  and only the current one is served.
- `key_masked` and `sample_keys` hold keys in the form to pass back as `key`.
- `more` and `more_findings` say that the list was cut. `next_cursor` is `null` at the end of a list;
  otherwise it holds the value to pass as `cursor` for the next page. With more than two runs stored,
  step 3 ends `\"next_cursor\": \"R-35T7EZD23916ZNNHHER5R17DH8\"`, the id of the last run it showed.
- A masked value starts with `tok:` or `class:`. Other values, such as `"currency": "GBP"` above, are
  ones the spec declares clear.
- The runbook gives 65,536 bytes as the most one answer holds.

If Python 3 is on your machine, this lays the text of an answer out, put after any command of steps
3 to 6:

```bash
dscurl my-agent -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_runs","arguments":{"limit":1}}}' http://localhost:8000/v1/mcp | python3 -c 'import json,sys; print(json.dumps(json.loads(json.load(sys.stdin)["result"]["content"][0]["text"]), indent=1))'
```

### Limits

- POST only. The server offers no stream and no session, and no sign-in of its own: it takes an API
  token and nothing else.
- A page in a browser on another site cannot call it, and a browser's signed-in session is refused.
- A tool offered to no role in `ds mcp check` cannot be called, whatever the token.
- `list_runs` gives at most 12 runs a page, the other lists 20, and `get_findings` 50.

### If something goes wrong

The first group are answers of the address itself, each with a status.

**`{"detail": "MCP over HTTP is off (mcp.http, docs/mvp/MCP.md §4)"}`**, status 404
The settings have no `mcp` block, or `http` is not `true`, or the app was not restarted: section 4.

**`{"detail": "not signed in"}`**, status 401
The request carried no token.

**`{"detail": "invalid or expired API token"}`**, status 401
The token is wrong, expired or revoked. `ds token list` shows its state.

**`{"detail": "MCP is served by POST only: no stream and no session"}`**, status 405
The client sent GET or DELETE. A client that then works with POST alone is fine.

**`{"detail": "cross-origin request refused"}`**, status 403
The request carried the `Origin` header of another site: the client is a page in a browser.

**`{"detail": "unexpected Host header"}`**, status 400
The address names the app otherwise than the settings' `public_url`. Provoked with the header
`Host: deltasleuth.example`. On this machine both `localhost` and `127.0.0.1` were served.

**`{"detail": "missing or invalid CSRF token"}`** and **`{"detail": "MCP takes an API token
(Authorization: Bearer dst_…), not a browser session"}`**, both status 403
The request came with a browser's sign-in cookie in place of a token. Provoked with the cookie of a
token sign-in, without and with the page's protection header.

**`Invalid Content-Type header`**, status 400
The request did not say `Content-Type: application/json`. This answer is plain text.

**`{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"Not Acceptable: Client must accept
application/json"}}`**, status 406
The request's `Accept` header left JSON out. curl sends `Accept: */*` by itself, which is accepted.

**`{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"Parse error: ..."}}`**, status 400
The request's body is not complete JSON, for example after a quote was lost in the shell.

**`{"jsonrpc":"2.0","id":8,"error":{"code":-32601,"message":"Method not found","data":"resources/list"}}`**
The server has tools only: no resources and no prompts.

The second group are refusals of a tool. They arrive with status 200, inside the answer's text, with
`"isError":true`.

**`run R-... is not served by this installation: its spec declares clear values or a business-code
key (mcp.data_classes lacks production_clear_declared)`**
The run's spec declares values clear, and the settings do not serve such runs: section 4, "The
practice runs need the third class".

**`compare_key is not served by this installation (mcp.work)`**
The settings do not serve the compare tool: section 4.

**`compare_key needs the operator role`**
The settings serve it, and the token is a viewer's.

**`unknown tool 'start_run'`**
There is no such tool. No tool starts a run.

**`invalid arguments: 50 is greater than the maximum of 12`**, or **`invalid arguments: Additional
properties are not allowed ('verbose' was unexpected)`**
An argument is outside what `tools/list` describes.

**`run R-NOSUCH not found`**, **`no key C00012019 in run R-...`**, **`schedule nightly not found`**
The id does not exist. A key must be given masked.

**`cycle 1 of run R-... was purged on 2026-10-05 (erasure): its keys, results, findings and evidence
are deleted`**
The run's evidence is deleted; only `list_runs` still shows the run.

**`rate limited: ...`**
Section 7.

## 6. Connect over stdio with `ds mcp`

### What it is for

Some agent programs start a tool server themselves, as a program on the same machine, and talk to it
over its input and output. `ds mcp` serves the same tools that way. It is meant for a developer's
workstation that runs DeltaSleuth from source without sign-in ([chapter 2](02-run-from-source.md)).

In an installation with sign-in, such as the one of chapter 1, it is the administrator's own channel.
Whoever starts it acts with the machine account that holds the database and the keys, under a name
that is only asserted, not signed in. **Do not give this channel to an agent that can run commands.**
Agents belong on the HTTP address of section 5 with a `viewer` token.

### Before you start

- Section 4 is done. `http` may be `true` or `false`: `ds mcp` needs only the block.
- Your user name is passed in `DS_OPERATOR`; it is a name, not a secret.

### In the web UI

The web UI does not offer this.

### On the command line

1. Write the messages of a short conversation into a file, one to a line: a greeting, the note that
   ends it, a request for the tools, and one call.
   ```bash
   printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"by-hand","version":"0"}}}' '{"jsonrpc":"2.0","method":"notifications/initialized"}' '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_runs","arguments":{"limit":1}}}' > frames.jsonl
   ```
2. Start the server, hand it the file, and keep its input open for four seconds so that it can
   answer. `-T` matters: a terminal would garble the messages. `2> mcp-err.txt` keeps what the server
   reports about itself apart from its answers.
   ```bash
   { cat frames.jsonl; sleep 4; } | docker compose run --rm -T -e DS_OPERATOR="$(id -un)" app ds mcp --host-channel 2> mcp-err.txt
   ```
   You should see three answers, one to a line, to the messages with an `id`:
   ```text
   {"jsonrpc":"2.0","id":1,"result":{"capabilities":{"experimental":{},"tools":{"listChanged":false}},"instructions":"These tools read DeltaSleuth comparison runs. ...","protocolVersion":"2025-06-18","serverInfo":{...}}}
   {"jsonrpc":"2.0","id":2,"result":{"tools":[{"annotations":{"readOnlyHint":true},"description":"The stored comparison runs, newest first: ...","name":"list_runs"},...]}}
   {"jsonrpc":"2.0","id":3,"result":{"content":[{"text":"{\"tool\": \"list_runs\", \"data\": {\"runs\": [{\"run_id\": \"R-35T7F16XFJ21WTZ1KG0F4VSCV7\", ...}], ...}}","type":"text"}],"isError":false}}
   ```
3. Read what the server reported.
   ```bash
   cat mcp-err.txt
   ```
   You should see Docker's line, who the server served as, the warning about this channel, and one
   line for the call:
   ```text
    Container deltasleuth-db-1  Running
   mcp: serving stdio as local:<you>@deltasleuth-app, role viewer; data classes aggregates, production_tokenised, production_clear_declared; work tools none
   warning: ds mcp is the host-administrator channel: its caller is asserted (DS_OPERATOR), not signed in, and an agent that runs it acts with this host account, not with a role; agents belong on /v1/mcp with a named token
   tool=list_runs actor=local:<you>@deltasleuth-app run=- bytes=2206 ms=3 error=0 request_id=C-cdfa1c7f71bd13d9
   ```
4. Remove the two files.
   ```bash
   rm frames.jsonl mcp-err.txt
   ```

`--role operator` after `--host-channel` offers the operator's tools; without it the role is
`viewer`.

**Wiring a client.** For a person at the installation's machine, the runbook gives this block for a
client that starts the server itself. `<folder>` is the installation's folder, written out in full:
the line changes into it first, because Docker Compose reads its settings there.

```json
{"mcpServers": {"deltasleuth": {"command": "sh",
  "args": ["-c", "cd <folder> && exec docker compose run --rm -T -e DS_OPERATOR app ds mcp --host-channel"],
  "env": {"DS_OPERATOR": "<you>"}}}}
```

The `sh -c` line of the block was run from another folder with `DS_OPERATOR` set and the file of
step 1 on its input, and gave the three answers. Not verified here: a client program that starts it,
and the runbook's block for a workstation without sign-in, which starts `uv run ds mcp` in a source
checkout.

### How to read the result

The answers read as in section 5. The server's own lines, in `mcp-err.txt` here, are for whoever
started it: the `mcp: serving stdio as ...` line names the caller every call is recorded under, and
each `tool=...` line is one call, with its size in bytes, its time in milliseconds and `error=1` for
a refusal. A call that reads a key also leaves a copy of its audit row there.

### Limits

- The caller is `local:<you>@deltasleuth-app`: the name in `DS_OPERATOR`, taken on trust.
- `--role` shapes what is offered. It does not limit what the account that runs the command can do
  with the installation.
- The server ends when its input ends. A call still running then is cancelled.
- A client that starts `ds mcp` must read the server's own lines or let them through. Not verified
  here: the runbook says a server whose lines nobody reads stops answering after some hundreds of
  calls.

### If something goes wrong

**`error: ds mcp under auth is the host-administrator channel: use /v1/mcp with a named token, or
pass --host-channel (docs/mvp/MCP.md §5.2)`**
`ds mcp` was started without `--host-channel` in an installation with sign-in.

**`error: set DS_OPERATOR to your user name (docs/mvp/AUTH.md §4.2)`**
The command lacks `-e DS_OPERATOR="$(id -un)"`.

**`{"jsonrpc":"2.0","id":3,"error":{"code":-32000,"message":"Connection closed"}}`**, with the lines
`psycopg WARNING` and `psycopg.pool WARNING` and a `tool=... error=1` line among the server's own
The input ended before the call was answered, and the call was cancelled. Met by handing the file
over without the `sleep 4` of step 2.

**The client reports garbled messages**
Not verified here. The runbook names the cause: the server was started with a terminal. Keep `-T`.

## 7. Follow, limit and stop an agent

### What it is for

An agent acts on its own, fast and often. Whoever administers the installation needs to see what an
agent read, to know what slows it down, and to be able to stop it at once.

### Before you start

The id of the agent's token, written `<token id>` below. `ds token list` shows it at the start of the
token's line, for example `T-aace1b4c15c64e30`:

```bash
ds token list
```

```text
T-aace1b4c15c64e30 (my-agent)  viewer  active  expires 2026-11-04T20:39:03+00:00  owner '<you>'  created by local:<you>@deltasleuth-app  last used 2026-10-05T20:39:03+00:00
T-7986935c349d2a1a (first-run)  spec_author  active  expires 2027-01-03T20:34:02+00:00  owner '<you>'  created by local:<you>@deltasleuth-app  last used 2026-10-05T20:39:03+00:00
```

### In the web UI

The web UI does not offer this in an installation with token sign-in: its page for tokens and the
audit log is shown only under single sign-on, to an administrator.

### On the command line

1. Read what the agent's token left in the audit log, newest first.
   ```bash
   ds audit --actor token:<token id> --limit 10
   ```
   You should see a copy of the row for your own reading, then the agent's rows:
   ```text
   {"id": 72, ... "action": "audit.read", ...}
   63  2026-10-05T20:39:26+00:00  token:T-aace1b4c15c64e30 (my-agent)  key.view  R-35T7EGYX442G9KT2QMW4M0NN0S  {"cycle": 1, "key_masked": "tok:identifier:v1:KB7XB764PVDAZMIHUMMHBVQY33", "via": "mcp:http"}  request Q-b00cb9c0a8efec7d  from 172.30.0.1
   62  2026-10-05T20:39:26+00:00  token:T-aace1b4c15c64e30 (my-agent)  mcp.read  R-35T7EGYX442G9KT2QMW4M0NN0S  {"calls": 1, "tool": "get_findings", "via": "mcp:http"}  request Q-f4bf2cfcb7fb3ce4  from 172.30.0.1
   61  2026-10-05T20:39:26+00:00  token:T-aace1b4c15c64e30 (my-agent)  mcp.read  R-35T7EGYX442G9KT2QMW4M0NN0S  {"calls": 1, "tool": "get_run_summary", "via": "mcp:http"}  request Q-1fb81e70949c0090  from 172.30.0.1
   ```
2. Read the single calls in the app's log. Every call leaves one line, whatever it was answered.
   ```bash
   docker compose logs --since 30m app | grep 'tool='
   ```
   You should see lines like these:
   ```text
   app-1  | tool=list_runs actor=token:T-aace1b4c15c64e30 run=- bytes=4342 ms=1 error=0 request_id=Q-3cb8eea40b6141b1
   app-1  | tool=get_run_summary actor=token:T-aace1b4c15c64e30 run=R-35T7EGYX442G9KT2QMW4M0NN0S bytes=213 ms=11 error=1 request_id=Q-610f570341b4f668
   app-1  | tool=get_run_summary actor=token:T-aace1b4c15c64e30 run=R-35T7EGYX442G9KT2QMW4M0NN0S bytes=3735 ms=21 error=0 request_id=Q-1fb81e70949c0090
   app-1  | tool=get_findings actor=token:T-aace1b4c15c64e30 run=R-35T7EGYX442G9KT2QMW4M0NN0S bytes=1051 ms=13 error=0 request_id=Q-f4bf2cfcb7fb3ce4
   app-1  | tool=get_observations actor=token:T-aace1b4c15c64e30 run=R-35T7EGYX442G9KT2QMW4M0NN0S bytes=5154 ms=29 error=0 request_id=Q-b00cb9c0a8efec7d
   ```
   The second line is the refusal of a practice run under the example block; the third is the same
   call after the third data class was added.
3. Stop an agent: take its token away. This is the last step for that token: after it, the requests
   of section 5 are refused until the agent has a new one.
   ```bash
   ds token revoke <token id>
   ```
   You should see an audit line, then:
   ```text
   revoked token T-aace1b4c15c64e30 (my-agent)
   ```
4. Check that the token is refused.
   ```bash
   dscurl my-agent -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' http://localhost:8000/v1/mcp
   ```
   You should see:
   ```text
   {"detail": "invalid or expired API token"}
   ```
5. Delete the token's file, which is now useless.
   ```bash
   rm .deltasleuth/tokens/my-agent
   ```
   If you made the copy of section 5, step 8, delete that too.
   ```bash
   rm ~/.config/deltasleuth/mcp-token
   ```

To stop every agent at once, switch the server off (section 4). For a new token under the same name,
run the command of section 5, "Before you start", again.

### How to read the result

**The audit log** names the agent by its token, never by the person behind it, and marks each row
with `"via": "mcp:http"`, or `"mcp:stdio"` for `ds mcp`.

| Row | Written for |
|---|---|
| `key.view` | Each `get_observations`: the key that was read |
| `mcp.read` | `get_run_summary`, `get_root_causes` and `get_findings`: one row for each token, tool and run in an hour; `calls` counts the calls since the row before |
| `compare_one`, `run.finished` | Each `compare_key`, on the one-key run it made |
| `authz.denied` | A tool refused for the role, at most once a minute: `MCP compare_key  {"count": 1, "needed": "operator", "via": "mcp:http"}` |

The lists (`list_runs`, readiness, schedules, pre-checks) and `tools/list` leave no audit row. They
are in the log only.

**A log line** holds the tool, the caller, the run once it named a stored one, the size of the
answer, the time and `error=1` for a refusal. It never holds an argument or a result. `tool=?` is a
call to a tool that does not exist.

### Limits

**The limits on calls.** They are counted for each token, in the app's memory, and start again at a
restart.

- 60 tool calls a minute, the `calls_per_minute` of the block. Over it:
  `rate limited: at most 60 MCP tool calls a minute per caller`. Refused calls count too: after 5
  calls with a wrong argument and 55 served ones, the next call was refused. `tools/list` is not
  counted, and another token is not held back.
- 10 calls a minute each of `get_readiness` and `compare_key`. Over it:
  `rate limited: at most 10 get_readiness calls a minute per caller`.
- 4 calls at once in the whole app, the `concurrency` of the block. Not verified here: the runbook
  says a call that waits 10 seconds for its turn is answered `busy: ... call again`.

Both `rate limited` lines were provoked with a loop of calls. An agent that meets one waits and calls
again.

**The stated limit: the token is a general API token.** Everything the `mcp` block cuts, it cuts for
calls through `/v1/mcp` only. The agent's token is an ordinary API token, and every other address of
the API takes it with its full role. Seen here, with the block as section 4, step 1, has it:

- The `viewer` token's `get_observations` was refused for a practice run, `run R-... is not served by
  this installation ...`. The same token then read that key, with its clear values, from the address
  the web UI's key page uses:
  ```bash
  dscurl my-agent "http://localhost:8000/v1/runs/<run id>/keys/<masked key>"
  ```
  It was answered 200 with the key's findings and both documents. The audit log holds a `key.view`
  row for it without `"via"`, and no `mcp.read`.
- An `operator` token's `compare_key` was refused, `compare_key is not served by this installation
  (mcp.work)`. The same token then started a whole run with the request of section 3, "Before you
  start", and was answered 202 with a run id.

So an agent that can read its token file, and can send requests of its own, is bounded by the
token's role and by nothing in the `mcp` block. Give an agent a `viewer` token. Give an `operator`
token only to an agent you would let start, pause and cancel runs. Where the client has a store for
secrets, as VS Code does, prefer it to a file.

Three more things the settings do not change. DeltaSleuth checks no sentence an agent writes: only
the built-in chat's answers are checked. A tool result has left DeltaSleuth once it is answered: the
agent's program decides where it goes, and deleting a run later does not reach the agent's copies.
And masked is not anonymous: the same value always gets the same `tok:` form, and names and counts
are visible.

### If something goes wrong

**The audit log shows no row for an agent you know was busy**
It called lists only, or its hour's `mcp.read` row is older than your `--limit`. The log of step 2
has every call.

**`refused: request Q-... names the reason in the server log`** or **`internal error: request Q-...
names it in the server log`** in a tool's answer
Not met here. The runbook says the reason is withheld from the agent because it may name an
address; find the request id in `docker compose logs app`.

**A token expired and nothing warned**
Not verified here. A token lives 90 days at most, and the deployment document says no alert is raised
when one ends: the agent's requests are answered 401 from then on. Create a new token under the same
name and replace the file.

## How this chapter was checked

Every command above was run on 2026-10-05, on macOS (Apple silicon) with Docker
Desktop, curl and OpenSSL 3.6.2, on an installation made as chapter 1 describes, each path in the
order of its steps.

- **The practice model.** Every chat answer above came from the program of the next section, started
  as section 1, steps 1 to 3, show. For the path with a key (section 1, steps 10 to 28) and for the
  hosted model (section 2, steps 1 to 8) a second copy of the same program ran in a container of its
  own, over https with a key, with a certificate from a test authority made with OpenSSL. For "A
  model server on this machine" it ran as a program on the machine itself.
- **The clipboard.** The `pbpaste` and `pbcopy` lines were run on a side pasteboard
  (`-pboard ruler`) with a made-up value. The value arrived in a file of mode 600 without its line
  end.
- **The files in the home folder** (section 5, steps 7 to 11, and section 7, step 5) were made in
  another folder that stood in for the home folder. A request with the header the program printed
  was answered 200.
- **The draft buttons** (section 3) were checked on 2026-10-07 on a copy in developer mode: their
  request with this practice model, their clicks in a browser with a scripted stand-in.

Not verified here:

- Any real language model, and any call to a host on the internet.
- Any page in a browser: every web UI step is written from the page's source.
- Any agent program: the MCP requests were sent with curl, and the client blocks were only read as
  JSON.
- Anything on Linux, including the four clipboard commands of section 1.

## The practice model's program

This is the command of section 1, step 1. Copy the whole block, from the line that starts with `cat`
to the line `PRACTICE`, paste it into a terminal in the installation's folder, and press Enter. It
writes the file `.deltasleuth/practice-model.py`, which git ignores, and prints nothing.

The program needs only Python's own library. It answers on the address `/v1/chat/completions` and
writes one line to its log for each request. It recognises the probe by the probe's fixed
instruction, and a chat question by the line DeltaSleuth puts before it. For a question about a run
it calls `get_run_summary`, then `get_findings` for the first root cause, and fills one sentence
pattern. For a question about a key it calls `get_observations`. Two words in a question change what
it does: `guess` makes it answer without evidence, and `compare`, in a conversation about a key,
makes it call `compare_key` when that tool is offered.

```bash
cat > .deltasleuth/practice-model.py <<'PRACTICE'
"""A practice model for DeltaSleuth's chat. It speaks the OpenAI chat API and understands nothing.

It always asks for the same tools, and it fills fixed sentences with ids and numbers copied from
the tool results it is sent. It proves the wiring. It says nothing about what a real model answers.

Environment: PRACTICE_PORT (18441 unless given) and PRACTICE_BIND (every address unless given).
PRACTICE_KEY_FILE: callers must send the key in that file. PRACTICE_CERT and PRACTICE_CERT_KEY:
serve https. PRACTICE_STRICT=1: refuse optional request fields, as some gateways do.
PRACTICE_CACHED=1: report cached-token counts.
"""
import json
import os
import ssl
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENV = os.environ.get


def reply(text=None, tool=None, args=None):
    message = {"role": "assistant", "content": text}
    if tool:
        function = {"name": tool, "arguments": json.dumps(args or {})}
        call = {"id": f"call_{time.time_ns() % 10**9}", "type": "function", "function": function}
        message["tool_calls"] = [call]
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    if ENV("PRACTICE_CACHED"):
        usage["prompt_tokens_details"] = {"cached_tokens": 0}
    choice = {"index": 0, "message": message, "finish_reason": "tool_calls" if tool else "stop"}
    return {"id": "chatcmpl-practice", "object": "chat.completion", "created": int(time.time()),
            "model": "practice-1", "choices": [choice], "usage": usage}


def text(message):
    return message["content"] if isinstance(message.get("content"), str) else ""


def answer(body):
    messages = body["messages"]
    if text(messages[0]).startswith("You are a conformance probe"):  # ds llm-probe
        results = [m for m in messages if m["role"] == "tool"]
        if results:
            return reply(text(results[-1]))
        if body.get("tools"):
            return reply(tool="get_probe_value", args={"key": "alpha"})
        return reply("ready")
    asked = [i for i, m in enumerate(messages) if m["role"] == "user" and text(m).startswith("Scope: ")]
    scope, _, question = text(messages[asked[-1]]).partition("\n\n")
    key = scope.split(", key ", 1)[1].rstrip(".") if ", key " in scope else None
    got = {}  # the tool results of this question, by tool name
    for m in messages[asked[-1]:]:
        if m["role"] == "tool":
            frame = json.loads(text(m).removeprefix("ERROR: "))
            got[frame["tool"]] = frame
    offered = [t["function"]["name"] for t in body.get("tools") or []]
    may_call = body.get("tool_choice") != "none"
    question = question.lower()
    if "guess" in question:  # an answer that cites nothing and states a number no tool returned
        return reply("About 99 keys differ.")
    if key and "compare" in question and "compare_key" in offered:
        tool, args = "compare_key", {"key": key}
    elif key:
        tool, args = "get_observations", {"key": key}
    else:
        tool, args = "get_run_summary", {}
    if tool not in got:
        return reply(tool=tool, args=args) if may_call else reply("I may call no tool now.")
    data = got[tool].get("data")
    if not data:
        return reply(f"The tool {tool} gave no data: {got[tool].get('error')}")
    if tool == "compare_key":
        return reply(f"Compared again just now, key [K:{data['key_masked']}] is {data['outcome']} "
                     f"({data['confirmation']}). The new reads are [{data['obs_a_id']}] on side A and "
                     f"[{data['obs_b_id']}] on side B.")
    if tool == "get_observations":
        result, said = data.get("result") or {}, ""
        if data.get("findings"):
            first = data["findings"][0]
            said = (f" Its first finding is [{first['finding_id']}]: {first['taxonomy_code']} at "
                    f"{first['path_pattern']}, severity {first['severity']}.")
        return reply(f"Key [K:{data['key_masked']}] ended as {result.get('outcome')} "
                     f"({result.get('confirmation')}).{said} It was read as [{result.get('obs_a_id')}] "
                     f"on side A and [{result.get('obs_b_id')}] on side B.")
    causes, counters = data.get("root_causes") or [], data["counters"]
    said = f"The run is {data['status']}: of {counters['total']} keys, {counters['different']} differ."
    if not causes:
        return reply(said + " It has no root cause.")
    cause = causes[0]
    if "get_findings" not in got and may_call:
        return reply(tool="get_findings", args={"root_cause_id": cause["root_cause_id"], "limit": 5})
    said += (f" The first root cause is [{cause['root_cause_id']}]: {cause['taxonomy_code']} at "
             f"{cause['path_pattern']}, severity {cause['max_severity']}, on {cause['affected_keys']} "
             f"key(s), for example [K:{cause['sample_keys'][0]}].")
    rows = (got.get("get_findings", {}).get("data") or {}).get("findings") or []
    if rows:
        said += (f" Finding [{rows[0]['finding_id']}] shows it: side A holds "
                 f"{json.dumps(rows[0]['value_a'])} and side B holds {json.dumps(rows[0]['value_b'])}.")
    return reply(said)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):  # one line of its own for each request instead
        return

    def send(self, status, payload, note):
        print(time.strftime("%H:%M:%S"), self.command, self.path, status, note, flush=True)
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        self.send(200, {"status": "ok", "model": "practice-1"}, "hello")

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        if ENV("PRACTICE_KEY_FILE"):
            with open(ENV("PRACTICE_KEY_FILE"), encoding="utf-8") as handle:
                if self.headers.get("Authorization") != f"Bearer {handle.read().strip()}":
                    error = {"message": "Incorrect API key provided.", "code": "invalid_api_key"}
                    return self.send(401, {"error": error}, "the key is missing or wrong")
        strict = [f for f in ("store", "parallel_tool_calls", "max_completion_tokens") if f in body]
        if ENV("PRACTICE_STRICT") and strict:
            error = {"message": f"Unrecognized request argument: {strict[0]}", "code": "unknown_parameter"}
            return self.send(400, {"error": error}, f"refused the field {strict[0]}")
        if not self.path.endswith("/chat/completions"):
            return self.send(404, {"error": {"message": "no such address"}}, "not the chat address")
        try:
            result = answer(body)
        except Exception as exc:  # a request this program was not written for
            return self.send(500, {"error": {"message": "the practice model failed"}}, repr(exc))
        calls = result["choices"][0]["message"].get("tool_calls")
        tools = ",".join(t["function"]["name"] for t in body.get("tools") or []) or "none"
        does = f"calls {calls[0]['function']['name']}" if calls else "answers"
        self.send(200, result, f"model={body.get('model')} tools offered: {tools}; {does}")


def main():
    port = int(ENV("PRACTICE_PORT", "18441"))
    server = ThreadingHTTPServer((ENV("PRACTICE_BIND", "0.0.0.0"), port), Handler)
    if ENV("PRACTICE_CERT"):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(ENV("PRACTICE_CERT"), ENV("PRACTICE_CERT_KEY"))
        server.socket = context.wrap_socket(server.socket, server_side=True)
    scheme = "https" if ENV("PRACTICE_CERT") else "http"
    print(f"practice model listening on {scheme}://{server.server_address[0]}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
PRACTICE
```

## Reference

- [MCP.md](../mvp/MCP.md): the MCP server in full: every tool's fields, every message, what is
  recorded.
- [DEPLOY.md](../mvp/DEPLOY.md): section 8 for the chat's settings; "The MCP server" under
  "Configure" and under "Troubleshooting"; "TLS" for a certificate authority of your own.
- [RUNBOOK.md](../mvp/RUNBOOK.md): "Your own agent (MCP)", with the client blocks; "Security and
  privacy"; "OpenAI-compatible gateways" for the `wire` line.
- [CONTRACTS.md](../mvp/CONTRACTS.md): the chat's tools and the check of an answer, under "Agent".
- [AUTH.md](../mvp/AUTH.md): roles, tokens and the audit log.
- [RETENTION.md](../mvp/RETENTION.md): how long a run's evidence and its conversations are kept.
- [Chapter 1](01-install-docker.md) and its appendix [A1](A1-install-by-hand.md): the installation,
  tokens, the settings files and the site file. [Chapter 3](03-first-comparison.md): runs, keys and
  findings.
