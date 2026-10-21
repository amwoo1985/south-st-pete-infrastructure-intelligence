# Secrets-Handling Rules

Binding for anyone — orchestrator, any specialist, any reviewer, or a direct shell command run in this session — touching `.env` or any file that carries a live credential (API keys, DB passwords, connection strings), and to deciding where a credential is stored.

Two real exposures have already happened this way: DECISIONS #131 (`grep -v PASSWORD` missed `OPENAI_API_KEY`, which doesn't contain that substring) and a second one the same session as this rule was written (a `grep -n "OPENAI_API_KEY" .env` matched the whole line, value included, printing the live key into a session transcript). Both were denylist-shaped mistakes — filtering out the pattern you expect a secret to match, instead of only ever allowing in what you've confirmed is safe.

## Rules

- **Never grep, cat, or echo a secrets file (`.env` or equivalent) without redirecting output away from a value.** Denylist filtering (`grep -v PASSWORD`, `grep -v SECRET`) is not safe — any real key/token that doesn't happen to contain the filtered substring prints in full. This applies equally to a specialist agent and to a direct Bash/PowerShell command run in the orchestrating session itself.
- **To check whether a key exists or find its line number, match on the key name and print only the name, not the line:** `grep -c "^OPENAI_API_KEY=" .env` (count, no value) or `grep -o "^OPENAI_API_KEY" .env` (name only, `-o` truncates before the value). Never plain `grep -n` or `grep` with no output-shaping flag against a secrets file.
- **To use a secret's value in a command, source it into an environment variable and reference the variable — never print it.** E.g. `set -a; source .env; set +a` then use `$OPENAI_API_KEY` directly in a command, with no intermediate `echo`/`cat`/`print` of that variable.
- **Never store a credential in resource metadata.** Tags, resource names, descriptions, and similar labels on any AWS resource (or any other platform) are readable by anyone with the matching describe/get permission and show up in routine read-only output. Credentials go only in `.env` or the local AWS CLI credentials file (`~/.aws/credentials`) on a developer machine, or a secrets manager at deploy time (DECISIONS #140, #168). If you find credential-shaped metadata, identify a key ID by its last 4 characters only, and write any secret value as `[redacted]`. Even 4 characters of a short password leak part of it.
- **If a secret does print into a transcript or log despite the above, don't try to fix it by deleting the message — that doesn't retract it from wherever the session transcript is retained.** Say so plainly (own it, name what leaked and where), and recommend rotation immediately, same as DECISIONS #131 and #135 did.

Why: a transcript is not a retractable medium — the only real mitigation once a secret prints is rotation, so the point of this rule is to make printing structurally hard to do by accident, not to rely on remembering to be careful every time.
