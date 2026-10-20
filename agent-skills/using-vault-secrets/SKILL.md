---
name: using-vault-secrets
description: Load this BEFORE using a password, card number, security code or document number from the user's vault (the `vault` MCP server) in a script or shell command, such as filling a web form with Playwright or logging in with curl. It shows how to get the values into the command without them entering the conversation.
---

# Using vault secrets without reading them

When the `vault` MCP server's `request_secret` is called with `delivery: "env"`, you get a
**handle** instead of the values. `vault-run` trades the handle for the values and gives them
to one command as environment variables, then removes them from that command's output. The
values never appear in your conversation, so the AI provider never sees them and they are not
stored with the chat.

Use it for opaque values that a program types in for you: passwords, card numbers, security
codes, document numbers. For free text you need to read (an address, company details, notes),
use the default `delivery: "text"`.

## Steps

1. **Write and test the script first**, with placeholder values. It must read the values from
   its environment, for example `process.env.VAULT_NUMBER` in Node or
   `os.environ["VAULT_PASSWORD"]` in Python.
2. **Request the secret** with `delivery: "env"` and a clear purpose. Once the user approves,
   `get_result` answers with a `handle`, the variable names in `env` (one `VAULT_<FIELD>` per
   field, e.g. `VAULT_NUMBER`, `VAULT_SECURITY_CODE`, `VAULT_PASSWORD`) and a `run` example.
3. **Run it within 5 minutes.** The handle works once:

```bash
python3 /root/.letta/skills/using-vault-secrets/vault-run <handle> -- node /work/$AGENT_ID/checkout/fill.js
```

To use a value in an argument, put the whole command in single quotes behind `sh -c`. Without
the quotes your shell expands `$VAULT_PASSWORD` to nothing before `vault-run` sets it:

```bash
python3 /root/.letta/skills/using-vault-secrets/vault-run <handle> -- sh -c 'curl -fsS -u "me:$VAULT_PASSWORD" https://api.example.com/orders'
```

## Rules

- Run the command in the foreground. Output from anything it leaves running in the background
  is lost when `vault-run` exits.
- In the output, a value shows as `[VAULT_NUMBER]` and so on. That means it was used, not that
  it is missing. Short values such as a 3-digit security code are not hidden, so don't print them.
- Use the values only for the purpose the user approved. Never print, save, log or send them
  anywhere else, and never write them into files, memory or messages.
- "unknown or expired handle": the handle was already used or is older than 5 minutes. Ask for
  the secret again.
- One handle per `vault-run`. To use two secrets in one command, nest them:
  `python3 $V <h1> -- python3 $V <h2> -- <command>`, with
  `V=/root/.letta/skills/using-vault-secrets/vault-run`. Two secrets with the same field (two
  cards) cannot be combined this way.
