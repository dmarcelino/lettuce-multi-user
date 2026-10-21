# Operations

## Everyday
```bash
./lettucectl list                 # stacks, hostnames, allowed users
./lettucectl status               # containers and health per stack
./lettucectl logs alfred          # follow all services; or: logs alfred cloudflared
./lettucectl up all               # check + build + start (idempotent; also applies config changes)
./lettucectl restart alfred       # down + up
./lettucectl compose alfred ps    # any docker compose command for one stack
```

- Use `restart`, never `compose <name> restart app-server`. The BFF shares the app-server's network namespace and dies when the app-server is recreated on its own. `up` and `restart` always act on the whole stack.
- After a host reboot, Docker brings the stacks back by itself (`restart: unless-stopped`).

## People
| Task | Command |
|---|---|
| Add a person | `./lettucectl add <name> <email>`. One stack per person; extra addresses are comma-separated. |
| Change who may sign in to a stack | `./lettucectl users <name> a@x.com,b@y.com`. This replaces the list. |
| Remove a stack, keep its data | `./lettucectl remove <name>` |
| Remove a stack and its data | `./lettucectl remove <name> --purge` |

`remove` asks you to type the name. It stops the stack, then deletes its DNS record, tunnel, Access application and policy. Anything it cannot delete stays recorded in `stack.env`, so you can run `remove` again.

When `add` stops half-way (a network error, a missing token permission), run the same command again. It resumes from the ids recorded in `stacks/<name>/stack.env`.

## Model providers and agents
Each person sets up their own stack in the Lettuce UI:
- **Settings -> Providers & models** to connect a provider.
- The sidebar to create agents.

Provider keys live in that stack's `state/letta-data`, never in this repo.

To look around as an agent does, open a shell in its workspace:
```bash
./lettucectl shell alfred lucius   # agent name (any case) or id; without one, /work
```
Agents run their Bash tool as root in the app-server with no sandbox. Each agent's tool starts in `/work/<agent-id>` with `AGENT_ID`, `AGENT_NAME` and `MEMORY_DIR` set, and `shell` sets the same. Like an agent, the shell can read every agent's workspace, memory and conversations in that stack, plus `/root/.letta` (provider and Claude/Codex credentials). It is close to what the agent sees but not identical: letta-code also sets the conversation id, the agent's secrets and a few `PATH` entries. Commands can be piped in: `./lettucectl shell alfred lucius <<<'ls'`.

### Working on an agent's workspace with your own tools
`shell` is the wrong place for your own credentials: agents share that container as root, so they could read anything you bring in while the shell is open. `toolbox` instead starts a throwaway container from the stack's app-server image with only that agent's workspace mounted:
```bash
./lettucectl toolbox alfred lucius          # bash in /work/<agent-id>
./lettucectl toolbox alfred lucius claude   # straight into Claude Code
```
It brings in whatever of these the host has, read-only, and says which:
- your ssh-agent (`SSH_AUTH_SOCK`; the key itself never enters) and `~/.ssh/known_hosts`;
- `~/.gitconfig`, and with a logged-in `gh`, a GitHub token git uses for `https://github.com`;
- Claude Code's `CLAUDE.md`, `settings.json`, `skills`, `agents` and `commands` (from `~/.claude`, or `CLAUDE_CONFIG_DIR`), and a Claude token: `CLAUDE_CODE_OAUTH_TOKEN` if you set it, otherwise the keyring entry `service claude-toolbox`. Set that entry up once with `claude setup-token`, then `secret-tool store --label='Claude toolbox' service claude-toolbox` (Ubuntu package `libsecret-tools`).

`/root` and `/tmp` are in memory, so history, caches and logins made inside are gone on exit; only what you write to the workspace stays, and the agent sees it. Tokens reach the container through docker's environment, never its command line. The container is not on the stack's network and is labelled `lettucectl.toolbox=<name>`.

For anything else, write `~/.config/lettucectl/toolbox` (mode 600; it is parsed, never sourced, and words are split on spaces):
```
# shared skills that ~/.claude/skills links to keep their host path
mount ~/team/shared-skills
mount ~/.aws/config ~/.aws/config
mount ~/scratch /scratch rw
secret AWS_SESSION_TOKEN secret-tool lookup service aws-session
env AWS_PROFILE=dev
```
`mount SRC [DEST] [rw]` is read-only unless `rw`, and `DEST` defaults to `SRC`; `~` is your home in `SRC` and `/root` in `DEST`. `secret NAME COMMAND...` runs the command without a shell and passes its output as `NAME`; a secret overrides a built-in of the same name.

Two limits:
- Only someone with Docker access on the host can run `toolbox`. Docker access is root on the host, so a stack's web-only users cannot, by design.
- The workspace is the agent's to write, and what you run there runs with your credentials: git hooks, `.git/config` (`core.sshCommand`, credential helpers), package scripts, Makefiles. Look before you push, and prefer short-lived, narrowly scoped tokens.

### Agent skills
`agent-skills/` holds skills this repo gives every agent, for example `scheduling-scripts` (cron jobs that survive restarts, instead of `crontab`). After adding or changing one, push them to the running stacks:
```bash
./lettucectl skills all           # or one stack: skills alfred
```
They go into each stack's global skills (`/root/.letta/skills`), which every agent of that stack loads. `skills` replaces or removes only what it installed before (listed in `.lettucectl-skills` there), so upstream's skills and the ones enabled in Settings stay. A name upstream Lettuce also ships is refused, because its BFF would overwrite ours. `up` does not install skills; run `skills` after setting up a new stack. Skill text reaches every operator's stacks, so keep it generic.

## Optional features
Optional features are compose profiles, the same list for **all** stacks (upstream `docs/CONFIGURATION.md` lists them):
- `search`: SearXNG + DuckDuckGo page reader for the agents' web tools.
- `claude`: Claude Code CLI workers. `codex` does the same for Codex.
- `google`: Gmail/Calendar/Tasks/Contacts sidecar. It needs a Google OAuth client and each person connects their own account: see **[google-setup.md](google-setup.md)**.
- `telegram`: Telegram channel gateway.
- `secrets`: a personal vault per stack (address, cards, ID documents, logins...), encrypted with the person's own passphrase and shared with agents only when they agree. Each person uses it at `https://<stack>/secrets/`: see **[secret-broker.md](secret-broker.md)**.

```bash
./lettucectl profiles                              # show config.env and every stack
./lettucectl profiles cloudflared,search,google    # set the list everywhere, then up all
```
`profiles` first checks every stack, so one broken stack stops the change before anything is written. It then writes `PROFILES` in `config.env` and `COMPOSE_PROFILES` in every `stack.env` together, and runs `up` on every stack. It also removes the containers of profiles that are off, which Compose would leave running. That last step runs every time, so a re-run cleans up after an interrupted one. When the list is already set everywhere, it changes and restarts nothing. Every run also re-applies each stack's tunnel routes (`/secrets/` goes to the vault only with `secrets`), before any stack starts; if that fails, the stacks still start and the command asks you to run it again. `codex` and `claude` change the app-server image, and `up` rebuilds it. The list must contain `cloudflared`.

## Config changes
`config.env` values are copied into each `stack.env` when the stack is created. Use `profiles` for the profile list. To change `TZ` for existing stacks, edit `config.env` and every `stack.env`, then run `./lettucectl up all`.

`check` (run by every `up`) refuses a stack whose profiles differ from `config.env`.

## Upgrading Lettuce
Lettuce tags look like `vX.Y.Z-letta_<letta-code version>`. Each one pins the server version its UI was compiled against. Never change versions any other way.
```bash
git -C upstream fetch --tags && git -C upstream tag --sort=-creatordate | head
./lettucectl upgrade vX.Y.Z-letta_A.B.C     # checkout, record in config.env, rebuild + restart all
```
Back up first (below), and read upstream's CHANGELOG.md.

## Backups
```bash
./lettucectl backup alfred /mnt/backup             # a snapshot in /mnt/backup/alfred/<UTC time>/
./lettucectl backup all /mnt/backup --compress     # a .tar.gz per stack instead
```
`backup` copies what the images cannot recreate:
- agents, conversations and memory (`state/letta-data`);
- settings, schedules, transcripts, Claude Code/Codex/gh logins and skills installed from Settings (`state/letta-home`);
- agents' workspaces (`state/workspaces`) and the tools agents installed (`state/tools`, left out with `--no-tools`);
- `stack.env`, and the vault, whenever its volume exists (turning the `secrets` profile off keeps it).

It leaves out caches and the skills that come back by themselves: upstream's, which the BFF reinstalls on every connect, and those from `agent-skills/`, which `lettucectl skills` reinstalls. The state is root-owned, so `backup` reads it from a container of a small image it builds (`backup/Dockerfile`, `lettuce-backup:local`), with no network and read-only mounts. No sudo is needed.

- **Snapshots (the default).** Each run writes `<dir>/<name>/<UTC time>/`, owned by you with the original file modes. The original owners are not kept: a restore makes the state root's again, which is what nearly all of it is, so a file an agent gave to another user (say, a database directory owned by uid 999) comes back owned by root. Use `--compress` when those owners matter. Files unchanged since the previous snapshot are hard links to it, so a run costs only what changed, and each snapshot is still a complete copy. Hard links need a Linux filesystem at the destination (ext4, xfs, btrfs; not FAT or exFAT). `<dir>` is a local directory outside this repo: for another machine, mount it (NAS, USB disk) or copy the snapshots onward with your own tools.
- **`--compress`.** Writes `<dir>/<name>/<UTC time>.tar.gz` instead, mode 600. Inside the archive, files keep their real owners. Each archive holds everything again, so this suits one-off copies and moving to another host better than regular backups.
- **A running stack.** Copying while agents write can catch memory, conversations or the vault mid-write and leave a corrupt backup. `backup` asks whether to pause the stack while it copies (agents and the UI freeze until it finishes; recommended), copy live anyway, or abort. `--pause` or `--live` answers ahead of time; without either and without anyone to answer, it aborts. A stopped stack is copied without asking.
- **`--keep N`.** After a successful run, removes all but the newest N snapshots (or, with `--compress`, archives) of each stack. Snapshots can hold read-only directories, such as Go's module cache, so to delete one by hand use `chmod -R u+w <snapshot>` first.
- A run that is interrupted (Ctrl-C stops the copy and unpauses the stack) leaves a `.partial-*` entry, which the next run removes. Only one backup of a stack runs at a time; a second one stops with an error.
- With `all`, a stack that fails (or that nobody answered for) does not stop the others; `backup` lists the failed ones at the end and exits with an error.

A nightly cron job (`crontab -e`) for every stack that keeps two weeks:
```
30 3 * * * /path/to/lettucectl backup all /mnt/backup --pause --keep 14
```
A backup holds credentials: provider keys, Claude/Codex/gh logins and `stack.env`. Keep the destination private, and encrypt any copy that leaves the host. The vault inside it stays encrypted with the person's passphrase, which is stored nowhere, so nobody (you included) can recover a vault whose passphrase is forgotten. `config.env` and `.secrets/` are not included; keep a copy of them too.

To restore, stop the stack (`./lettucectl down alfred`), or on a new host create its directory: `(umask 077 && mkdir -p stacks/alfred/state)`. Then, from a snapshot `S` (the state goes back to root, the vault to its uid 10001, and `state/` itself stays yours):
```bash
S=/mnt/backup/alfred/20261018T033000Z
install -m 600 "$S/stack.env" stacks/alfred/stack.env
docker run --rm -v "$S/state:/b:ro" -v "$PWD/stacks/alfred/state:/d" lettuce-backup:local \
  sh -c "rsync -aH --numeric-ids --delete --chown=0:0 /b/ /d/ && chown $(id -u):$(id -g) /d"
# if the vault is in the snapshot:
docker run --rm -v "$S/vault:/b:ro" -v lettuce-alfred_secret-broker-data:/d lettuce-backup:local \
  rsync -a --numeric-ids --delete --chown=10001:10001 /b/ /d/
./lettucectl up alfred
```
`--delete` makes the state match the snapshot, so it also deletes what the backup left out. The caches and skills come back by themselves, but for a snapshot taken with `--no-tools`, add `--exclude=/tools/` to the first `rsync` to keep the tools already there.

From an archive `A`, extract the same parts in the same image instead (the archive keeps the owners), then `up`:
```bash
docker run --rm -v "$A:/a.tar.gz:ro" -v "$PWD/stacks/alfred/state:/d" lettuce-backup:local \
  tar -xzpf /a.tar.gz --numeric-owner -C /d --strip-components=2 ./state
docker run --rm -v "$A:/a.tar.gz:ro" -v lettuce-alfred_secret-broker-data:/d lettuce-backup:local \
  sh -c 'tar -xzpf /a.tar.gz --numeric-owner -C /d --strip-components=2 ./vault && chown 10001:10001 /d && chmod 700 /d'
(umask 077 && tar -xzOf "$A" ./stack.env >stacks/alfred/stack.env)
```
On another host, first clone this repo, copy `config.env` and `.secrets/`, run `./lettucectl init`, and build the image: `docker build -t lettuce-backup:local backup`. If the repo is at a different path there, fix `LETTA_STATE_DIR` in the restored `stack.env`. Cloudflare needs no change: the tunnel token travels inside `stack.env`. Never run the same stack on two hosts at once.

The named volume `lettuce-<name>_bff-data` holds push-notification subscriptions and pinned-agent lists. It is not backed up; losing it is harmless. `remove` deletes the vault's volume along with the stack.

Each stack's push-notification keypairs (`PUSH_VAPID_*` for Lettuce, `BROKER_VAPID_*` for the vault, in `stack.env`) are generated by the first `up` and kept from then on. Do not change or delete it: every device that enabled notifications would have to enable them again.

## Troubleshooting
| Symptom | Look at |
|---|---|
| Cloudflare error 1033 / "tunnel not connected" | `./lettucectl logs <name> cloudflared`. The stack must be up; the token must match the tunnel (`stack.env`). |
| 502 / 504 after sign-in | The app-server is still starting or unhealthy: `./lettucectl status`, `./lettucectl logs <name> app-server`. |
| Signed in, Lettuce says forbidden or unknown user | The email isn't in `ALLOWED_USERS`. Use `lettucectl users`, which updates both places. |
| Google sign-in error at Cloudflare | Zero Trust -> Integrations -> Identity providers -> Google -> Test. Check the redirect URI in Google Cloud. |
| `add`: "both ... are taken" | Choose another stack name, or set `HOSTNAME_FALLBACK` (e.g. `ai-%s`). |
| `add`: Cloudflare API error 10000 / authentication | The token lacks a permission listed in cloudflare-setup.md step 5. |
| `init`: zone not found | The domain isn't on this Cloudflare account yet, or the token lacks Zone Read for it. |
| `check`: profiles differ | See "Config changes". |
| Image attachment fails: "the image processing worker is missing from this installation" | letta-code's self-updater ran `npm install -g` inside the app-server and moved the install aside. `compose/hardening.yml` sets `DISABLE_AUTOUPDATER=1` and `check` enforces it; recreate the stack with `./lettucectl up <name>`. Do not reinstall letta-code inside the container. |
| Enabling notifications fails: "Could not load the push public key" | The stack has no VAPID keypair, so the BFF turns push off. Set `PUSH_CONTACT_EMAIL` in `config.env` and run `./lettucectl up <name>`, which generates the keys. `check` reports a stack without them. |
| `/secrets/` shows Lettuce or a 404 | The `secrets` profile is off, or the route update failed: run `./lettucectl profiles <list>` again. |
| `/secrets/` gives 502 | The vault is not running: `./lettucectl status`, `./lettucectl logs <name> secret-broker`. |
| Someone forgot their vault passphrase | It cannot be recovered. They can erase the vault in Settings after unlocking; if they cannot unlock, stop the service and delete its volume: `./lettucectl compose <name> rm -sf secret-broker`, `docker volume rm lettuce-<name>_secret-broker-data`, `./lettucectl up <name>`. |
| `/secrets/` says "direct access is not allowed" or "may not use this stack" | The request did not come through Cloudflare Access, or the email is not in `ALLOWED_USERS`. |
| A wildcard warning during `add` | The domain has a `*` record, so public DNS can't show which names are free. Only the Cloudflare zone is checked. |

## Security notes
- Keep `config.env`, `.secrets/` and `stacks/` mode 600/700. `lettucectl` creates them that way and `check` enforces it for `stack.env`.
- Never add `DEV_BYPASS_*` to a stack. It disables authentication, and `check` refuses to start a stack that has it.
- The Cloudflare API token can change your DNS. Scope it to one zone and one account, and consider an IP restriction.
- Agents in one stack can read everything else in that stack, including its provider keys. That is why each person gets their own stack. The vault is the exception: it lives on a volume only the vault service mounts (`check` enforces that), encrypted with each person's passphrase. Root on the host can still read the vault service's memory while it is unlocked, and anything a person shares with an agent is stored in that conversation like the rest of it.
- Run `tests/run.sh` and `shellcheck -x lettucectl lib/*.sh scripts/*.sh tests/run.sh tests/run-broker.sh tests/stubs/*` before changing the CLI, and `tests/run-broker.sh` before changing `broker/`.
