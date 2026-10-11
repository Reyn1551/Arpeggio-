# Security Policy

Arpeggio holds provider API keys, runs commands, and edits real repositories. Security reports
are taken seriously and answered.

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Use GitHub's private vulnerability reporting: on the repository, go to **Security → Advisories →
Report a vulnerability**. That channel is private between you and the maintainer.

If you cannot use it, contact the maintainer through the GitHub profile
[@Reyn1551](https://github.com/Reyn1551) and ask for a private channel. Do not include the
details of the vulnerability in a public message.

Please include:

- what an attacker can do, and what they need to start,
- the affected version or commit, and your platform,
- the smallest reproduction you have,
- whether the issue is already public anywhere.

## What to expect

| Stage | Target |
|---|---|
| Acknowledgement of your report | 3 working days |
| First assessment (accepted / needs info / out of scope) | 10 working days |
| Fix or a documented mitigation for accepted issues | 90 days, sooner for anything critical |

This is a personal project maintained by one person, so these are honest targets rather than a
contractual SLA. If a report is accepted, you are credited in the advisory and the changelog
unless you ask not to be.

Please give a reasonable window to ship a fix before disclosing publicly. If a fix is going to
take longer than 90 days, you will be told why and when to expect it.

## Scope

**In scope.** Anything that breaks a guarantee the project makes in
[docs/07-SECURITY-AND-PRIVACY.md](docs/07-SECURITY-AND-PRIVACY.md), in particular:

- a secret (API key, token, `.env` value) reaching a model, a log, an artifact, or a report that
  should have been redacted (SAF-02),
- an attempt changing anything outside its own worktree, or a patch escaping the worktree,
  `.git`, or the grounded path list (EXE-04, EXE-08),
- an action that should have required approval happening without it (SAF-01, SAF-03, SAF-05),
- a repo with privacy class `private` or `client` routing to a provider that is not allowed to
  see it (SAF-06, SAF-07),
- content from a web page, issue, README, or tool output being treated as an instruction rather
  than as data (SAF-04),
- a command-policy bypass: a refused command shape that runs anyway (EXE-09),
- a holdout task, hidden test, or reference diff reaching a prompt or a report (EVL-04).

**Out of scope.**

- A model producing wrong or insecure code that the checks did not catch. That is a quality bug,
  and the right place is a normal issue with the task and the run ID.
- Cost from running a paid provider, or a free tier being rate-limited. Budget behaviour is in
  [docs/05-ROUTING-AND-COST.md](docs/05-ROUTING-AND-COST.md).
- A local attacker who already has your user account, your filesystem, or the ability to edit
  Arpeggio's own source or config. Arpeggio does not defend against its own operator.
- Vulnerabilities in pytest, ruff, mypy, or another dev dependency that never ships in a release.
  Report those upstream.
- Anything requiring the operator to be tricked into pasting a secret into a prompt by hand.

## Supported versions

Arpeggio is pre-1.0 and moves on `main`. Security fixes land on `main` and in the next release;
there are no maintained back-port branches yet. This section will list supported release lines
once there is a 1.0.

## Handling secrets

Never paste a real key into an issue, a pull request, or an advisory. Config refers to a key by
name (`env:VAR` or `keychain:NAME`) and never stores the value. If you think you have leaked a
real credential, rotate it first and then tell the maintainer.
