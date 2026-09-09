# Security Policy

## Reporting a vulnerability

Please do not open a public issue for vulnerabilities that may expose Chrome CDP sessions, credentials, local files, or remote command execution.

Use GitHub's private vulnerability reporting for this repository. Include:

- affected commit/version
- reproduction steps
- expected and observed impact
- whether a credential, Chrome profile, or Web UI endpoint is involved

Do not include real Cookies, Tokens, passwords, Keychain values, or Chrome profile files in the report.

## Security boundaries

### Chrome CDP

Chrome Remote Debugging provides high-privilege control over the selected browser profile.

- bind CDP only to `127.0.0.1`
- use a dedicated `--user-data-dir`
- never expose the CDP port through a public reverse proxy
- do not share the check-in profile with untrusted automation

### Web UI

The Web UI is intended for local use only.

- it refuses non-loopback bind addresses
- it validates the Host header
- state-changing requests require a random process-local CSRF token
- it can trigger real check-in actions; do not expose it publicly
- the static frontend (`scripts/serve-frontend.py`) is a read-only file server with
  no API/credential/runner access and applies CSP, `nosniff` and `no-store`;
  it serves only a whitelist of exact filenames and rejects traversal paths
- the API enforces a strict CORS origin allow-list (`DAILY_CHECKIN_WEB_ORIGINS`,
  default loopback 127.0.0.1:8766 / localhost:8766); wildcard origins are never
  allowed, and unlisted origins are rejected without leaking auth or CSRF state
- both processes bind only loopback; do not expose either through a public reverse proxy

### Web login

- login uses `DAILY_CHECKIN_WEB_PASSWORD` (`X-DailyCheckin-Password`), read only
  from the environment — never from code, the database, logs, or the repository
- the legacy bearer token (`web.token`) remains accepted for compatibility; both
  credentials are compared with constant-time `secrets.compare_digest`
- the password/secret and 0600 token file must never be committed or logged

### Credentials

On macOS, credential secrets are stored in Keychain under service:

```text
com.mango.daily-checkin
```

SQLite stores references and metadata only. Provider implementations must never log credential values, request authorization headers, raw Cookies, or full sensitive response bodies.

### Runtime data

The following are local runtime artifacts and must not be committed:

```text
system.db
*.db-wal
*.db-shm
*.jsonl
last-run.json
cron-*.log
Chrome user-data directories
Obsidian private task files
```

## Supported versions

Security fixes are applied to the latest `main` branch. There is currently no long-term support branch.
