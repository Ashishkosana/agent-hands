# Security

## Scope of this project

Agent Hands is a research prototype. It drives real user interfaces, handles
operator credentials, and can perform consequential actions (transfers,
holds, account changes) on whatever system it is pointed at.

- **Do not point it at a production financial system.** The only targets it
  has been run against are the local fixtures in this repository and a
  third-party hosted demo supplied for that purpose.
- Credentials are read from the environment (`HANDS_PARAM_*`, `HANDS_API_KEY`)
  or a local `.env` that is gitignored. They are masked in traces and never
  persisted in artifacts; do not commit them.
- The capability API (`hands.api`) binds to localhost and has no
  authentication of its own. It is a demo surface, not a service boundary.
- The audit trace is a keyless hash chain: tamper-evident, not
  non-repudiable, and tail truncation is undetectable without an external
  anchor. Treat it accordingly.

## Reporting a vulnerability

If you find a security problem in the code — a way to bypass the network
allowlist or the risk-review gate, a path by which a model could influence
replay, a leak of masked parameters into traces or prompts, or a way to make
the reconciler return a confident verdict on insufficient evidence — please
report it privately rather than in a public issue.

Use GitHub's private vulnerability reporting on this repository
(**Security → Report a vulnerability**) if it is enabled, or contact the
author through the details on their GitHub profile. Include the commit, the
steps to reproduce, and what you observed. You should hear back within a
week; fixes are published as ordinary commits with a note in the commit
message.

There is no bug bounty.
