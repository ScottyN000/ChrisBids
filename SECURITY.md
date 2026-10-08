# Security

The pipeline reads documents, photos and emails that someone outside Mersco
wrote, and a bid it gets wrong costs money. So everything a document or a model
produces is treated as data: it can fill a schema field, never steer the code.

## What enforces that

| Threat | Control | Where |
|---|---|---|
| A document or email tells a reader to do something | Readers return schema-only JSON; the method on a row is set by rule, not by the model; prompts state that page text is data | `pipeline/readers/` |
| A reader writes outside its role | The broker checks every row against the access matrix; only the Auditor sets the audit field | `pipeline/broker.py`, `pipeline/roles.py` |
| History is edited after the fact | SQLite triggers refuse DELETE, and UPDATE of anything but the audit field, on claims, register and log | `pipeline/ledger.py` |
| A row claims to be another agent's, or backdates itself | The broker stamps agent and timestamp; callers cannot | `pipeline/broker.py` |
| A crafted `calc` runs code or hangs the broker | Parsed as `+ - * /` over numbers only; no `eval`, no `**` | `pipeline/schema.py` `arith` |
| A crafted file name or register path reads outside the job | Symlinks skipped at intake; every register path is resolved inside the packet | `pipeline/guard.py` `inside` |
| A crafted link reaches the host's own network | http(s) only, no credentials, no loopback, private or cloud-metadata address, redirects re-checked | `pipeline/guard.py` `public_url` |
| A malformed PDF exploits poppler | Absolute paths (no option injection), timeouts, page caps, non-root container user | `pipeline/intake.py`, `Dockerfile` |
| A PR's text or diff steers the advisor | Claude Code gets Read, Glob and Grep only, no network and no GitHub write; its reply must match `docs/advisor-schema.json`; code settles severity and posts the comment | `.github/workflows/advisor.yml`, `tools/advisor.py` |
| The API key leaks | Read only from the environment, named in `broker.SECRET_ENV`; `.env` is git-ignored; gitleaks scans every push | `pipeline/broker.py`, CI |

## What CI checks on every push

- **bandit**: Python static analysis; medium or high severity fails the build.
- **pip-audit**: known vulnerabilities in `requirements.txt`.
- **gitleaks**: secrets in the code or its git history.
- **Dependabot**: weekly update PRs for pip packages, GitHub Actions and the Docker base image.

## Known gaps

- The broker is a library in one process, so the access matrix holds against
  bugs and model output, not against code that constructs its own `Broker`.
  The hosting plan (per-role containers, broker as the only process holding the
  ledger and the key) closes this.
- The per-domain egress allowlist lives in the proxy that hosting adds;
  `guard.public_url` only refuses what no allowlist should pass.
- An email's sender is what the message says. Until intake reads sender and
  date from the message headers, a forwarded or spoofed message can produce a
  `customer` row; the Auditor and Chris's release are the check.

Report a problem to Scott Turner (GitHub `ScottyN000`).
