# Security

The pipeline reads documents, photos and emails that someone outside Contractor Co.
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
| A fetched web page or PDF steers the Codes & Regs or Materials agent, or reaches a host it should not | Only those two principals may fetch pages (the broker checks and logs each fetch; the Auditor's link check sends a HEAD to each fetched row's URL, see known gaps); only `.gov` hosts, `.us` state portals (`*.state.xx.us`) and the domains the page table names (`named_domains`: manufacturers, code publishers and ICC-ES, a city site on .com, Florida's rules site, a legal mirror and one product aggregator, each listed in review); every redirect hop is checked against the same list and `guard.public_url`; an 8 MB cap and a 30-second socket timeout; the page is shown to the model as data; code keeps an answer only if its quote is on the page and its sentence adds no figure the quote lacks (a range is one figure; only the page's listed identifiers are exempt), and keeps differing readings as unverified rows rather than picking one | `pipeline/web.py`, `pipeline/webread.py`, `pipeline/broker.py` |
| A malformed PDF exploits poppler | Absolute paths (no option injection), timeouts, page caps, non-root container user | `pipeline/intake.py`, `Dockerfile` |
| A PR's text or diff steers the advisor | The model is given only Read, Glob and Grep (`--tools`, with the rest also disallowed), so no shell or network; its job holds a read-only token; its reply must match `docs/advisor-schema.json`; code settles severity, and a blocking finding must name a real architecture page, a standing rule or a failing input; a separate job with no model posts the comment | `.github/workflows/advisor.yml`, `tools/advisor.py` |
| The API key leaks | Read only from the environment, named in `broker.SECRET_ENV`; `.env` is git-ignored; gitleaks scans every push | `pipeline/broker.py`, CI |

## What CI checks

- **bandit**: Python static analysis; medium or high severity fails the build. Runs on every PR push, on main and weekly; a branch push with no PR is not scanned until its PR opens.
- **pip-audit**: known vulnerabilities in `requirements.txt`. Same triggers as bandit.
- **gitleaks**: secrets in the code or its git history, on every push to any branch.
- **Dependabot**: weekly update PRs for pip packages, GitHub Actions and the Docker base image.

## Known gaps

- `anthropics/claude-code-action`, the one third-party action, holds
  `ANTHROPIC_API_KEY` in the advisor's review job and is pinned by its `v1`
  tag, which its owner can move. Pinning it to a commit SHA would close this.
- The broker is a library in one process, so the access matrix holds against
  bugs and model output, not against code that constructs its own `Broker`.
  The hosting plan (per-role containers, broker as the only process holding the
  ledger and the key) closes this.
- Real names are kept out of the repository (owner, 2026-10-09). The
  contractor, its staff, the paint rep and the engineer appear only as
  stand-ins ("Contractor Co.", "the estimator", "SW Rep", 555 numbers); the
  engineer's title block and seal and the rep's contact lines are redacted in
  the packet PDFs; `tests/test_no_real_names.py` checks every tracked file,
  PDF text and PDF metadata against hashed names. Commits before 2026-10-09
  still carry the real names, and the repository has been public since
  2026-10-08, so they remain readable in its history. The job sites' names and addresses
  remain, since the code and permit lookups depend on them. Neither workflow
  that holds `ANTHROPIC_API_KEY` runs for a fork: the advisor skips fork PRs
  and live runs only when started by hand by someone with write access.
- The wider `named_domains` list (code publishers and ICC-ES, a city site on
  .com, Florida's rules site, a legal mirror and one product aggregator) goes
  beyond the architecture's manufacturer domains (p.11); the owner approved it
  on 2026-10-09.
- The Auditor's link check (`auditor.link_live`) follows redirects through `guard.public_url` only, not the domain allowlist; it sends a HEAD request and reads no body.
- The per-domain allowlist for fetched pages is enforced in code
  (`pipeline/web.py`), in the same process as everything else; the network
  proxy that hosting adds would enforce it outside the process. Until then a
  bug in that process could reach any public host.
- `guard.public_url` resolves a host's name to check that it is public, and
  urllib resolves it again to connect, so a DNS answer that changes between the
  two (DNS rebinding) could reach a private address. Connecting to the checked
  address, or the hosting proxy, closes this.
- `pdftotext` (poppler) runs on PDFs fetched from the internet, with a
  120-second timeout but no page cap, in the pipeline's own process user. The
  non-root container is the only containment until hosting gives the web agents
  their own container.
- An email's sender is what the message says. Until intake reads sender and
  date from the message headers, a forwarded or spoofed message can produce a
  `customer` row; the Auditor and the estimator's release are the check.

Report a problem to the repository owner.
