---
name: scheduling-scripts
description: Load this BEFORE scheduling any script or command to run on its own at set times (cron, "every morning", "on weekdays at 7") without an agent turn. It says where a schedule survives container restarts, the file format, and how to list and remove jobs. Never use `crontab` here: it is wiped on every restart.
---

# Scheduling scripts

You run as root inside a Docker container that is recreated from time to time. Its
startup script **wipes root's crontab** (`crontab -e`, `crontab <file>`) every time, so a job
installed that way silently stops. What survives is a cron file in
`/root/.letta/system-cron/`: on every start it is copied into `/etc/cron.d/`.

A cron job runs your script with no agent turn and no model cost. If each run needs your own
judgment, use your scheduled tasks instead; those cost a turn every time.

## Add a job

1. Put the script and its log in your own workspace, `/work/$AGENT_ID`. Test it by hand first.
2. Name the file `<your-name>-<job>`, using only lowercase letters, digits, `-` and `_`.
   **Cron ignores a file whose name contains a dot**, so no `.cron` or `.txt`.
3. Write it to `/root/.letta/system-cron/`, then copy it to `/etc/cron.d/` so it runs now
   without a restart. Cron rereads `/etc/cron.d` within a minute.

```bash
me=$(printf '%s' "$AGENT_NAME" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/^-*//; s/-*$//')
job=$me-daily-report
cat >/root/.letta/system-cron/$job <<EOF
PATH=/usr/local/bin:/usr/bin:/bin
MAILTO=""
# m h dom mon dow user command
0 7 * * 1-5 root /usr/bin/python3 /work/$AGENT_ID/daily-report/run.py >> /work/$AGENT_ID/daily-report/run.log 2>&1
EOF
install -m 0644 /root/.letta/system-cron/$job /etc/cron.d/$job
```

The unquoted `EOF` makes the shell fill in `$AGENT_ID` as it writes the file. Cron itself
expands no variables, so every path in the file must be literal and absolute.

## File format

- **Every job line has a user field** (`root`) between the schedule and the command.
  A line without it never runs.
- Start with `PATH=/usr/local/bin:/usr/bin:/bin`. Cron's default `PATH` is
  `/usr/bin:/bin`, which lacks `node` and other tools in `/usr/local/bin`.
- `MAILTO=""` and `>> log 2>&1` on every line. There is no mail and no syslog, so output
  you do not redirect is lost, and so is any error.
- The schedule uses the container's time zone (`echo $TZ`). Jobs do not get `TZ`, so add a
  `TZ=<same value>` line if your script needs local time.

## List, change, remove

```bash
ls -l /root/.letta/system-cron/ /etc/cron.d/       # every job, yours and other agents'
```

- **Change:** edit the file in `/root/.letta/system-cron/` and copy it again with `install`.
- **Remove:** delete it from **both** places, or it comes back at the next restart:
  `rm /root/.letta/system-cron/<file> /etc/cron.d/<file>`.
- Only touch files that start with your own name. The others belong to other agents.
- Check a job ran by reading its log. Cron logs nothing of its own here.
