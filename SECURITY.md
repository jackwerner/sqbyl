# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Report privately via GitHub's **[Report a vulnerability](https://github.com/jackwerner/sqbyl/security/advisories/new)**
button (repository **Security** tab → **Advisories**). This opens a private advisory
visible only to the maintainers. Include, where you can: affected version/commit, a
minimal reproduction, and the impact you observed.

You can expect an initial acknowledgement within a few days. Once a fix is available
we'll coordinate disclosure and credit reporters who want it.

## Supported versions

sqbyl is pre-`1.0`; only the latest tagged release (and `main`) receive security fixes.

| Version | Supported |
| ------- | --------- |
| latest `0.x` / `main` | ✅ |
| older `0.x` | ❌ |

## Scope and design notes

sqbyl's threat model is shaped by a few deliberate invariants — worth knowing before
reporting:

- **Read-only by default.** The SQL layer refuses non-`SELECT`; on connect, sqbyl
  inspects the credential's privileges and warns if it can write. The agent and the
  Coach can never issue DDL/DML. A report showing a write path reaching the database
  is in scope and high-severity.
- **Credentials are never literals.** Connection strings and API keys use `env:`
  indirection and are never written to project files, releases, or traces.
- **No row data leaves the machine unbidden.** Query results are not persisted to
  committed files or traces; imported SQL that carries literals is flagged for review.
- **CI never spends API tokens.** Every LLM path is exercised through a mock /
  record-replay seam.

Findings that undermine any of these are exactly what we want to hear about.
