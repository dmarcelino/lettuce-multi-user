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

## Optional features
Optional features are compose profiles, set in `PROFILES` in `config.env` for **all** stacks (upstream `docs/CONFIGURATION.md` lists them):
- `search`: SearXNG + DuckDuckGo page reader for the agents' web tools.
- `claude`: Claude Code CLI workers. `codex` does the same for Codex.
- `google`: Gmail/Calendar/Tasks/Contacts sidecar.
  - Each person connects it in Settings -> Google.
  - It needs a Google OAuth client with redirect `https://<host>/api/google/oauth/callback`, one redirect URI per stack.
- `telegram`: Telegram channel gateway.

After changing `PROFILES`, rewrite it in every stack (see "Config changes") and run `./lettucectl up all`. `codex` and `claude` change the app-server image, and `up` rebuilds it.

## Config changes
`config.env` values are copied into each `stack.env` when the stack is created. To change `TZ` or `PROFILES` for existing stacks, edit both files, then run `./lettucectl up all`. For example:
```bash
sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=cloudflared,search,claude/' stacks/*/stack.env
```
`check` (run by every `up`) refuses a stack whose profiles differ from `config.env`.

## Upgrading Lettuce
Lettuce tags look like `vX.Y.Z-letta_<letta-code version>`. Each one pins the server version its UI was compiled against. Never change versions any other way.
```bash
git -C upstream fetch --tags && git -C upstream tag --sort=-creatordate | head
./lettucectl upgrade vX.Y.Z-letta_A.B.C     # checkout, record in config.env, rebuild + restart all
```
Back up first (below), and read upstream's CHANGELOG.md.

## Backups
Everything a stack needs is in `stacks/<name>/` (its secrets and its state). Back up with the stack stopped. The state is root-owned, so read it as root:
```bash
./lettucectl down alfred
sudo tar -C stacks -czf ~/lettuce-alfred-$(date +%F).tgz alfred
./lettucectl up alfred
```
To restore on another host:
1. Clone this repo.
2. Copy `config.env` and `.secrets/`, and extract the archive into `stacks/`.
3. Run `./lettucectl init`.
4. Run `./lettucectl up all`.

Cloudflare needs no change: the tunnel token travels inside `stack.env`. Never run the same stack on two hosts at once.

The named volume `lettuce-<name>_bff-data` holds push-notification subscriptions and pinned-agent lists. Losing it is harmless.

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
| A wildcard warning during `add` | The domain has a `*` record, so public DNS can't show which names are free. Only the Cloudflare zone is checked. |

## Security notes
- Keep `config.env`, `.secrets/` and `stacks/` mode 600/700. `lettucectl` creates them that way and `check` enforces it for `stack.env`.
- Never add `DEV_BYPASS_*` to a stack. It disables authentication, and `check` refuses to start a stack that has it.
- The Cloudflare API token can change your DNS. Scope it to one zone and one account, and consider an IP restriction.
- Agents in one stack can read everything else in that stack, including its provider keys. That is why each person gets their own stack.
- Run `tests/run.sh` and `shellcheck -x lettucectl lib/*.sh scripts/*.sh tests/run.sh` before changing the CLI.
