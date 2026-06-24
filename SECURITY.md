# Security policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

To report a vulnerability privately, use GitHub's private vulnerability reporting:
open the repository's **Security** tab and click **Report a vulnerability** (or use
[this link](https://github.com/richardoros/threadline-core/security/advisories/new)).
This routes the report privately to the maintainers. Please do not open a public issue.

Include:
- Description of the vulnerability and potential impact
- Steps to reproduce
- Any suggested remediation

You will receive acknowledgement within 72 hours and a status update within 7 days.

## Scope

threadline-core is a local-first server. The primary attack surface is:

- **API authentication bypass:** the `require_token` middleware when `THREADLINE_API_TOKEN` is set
- **SQL injection via FTS5 queries:** the `sanitize_query` function in `services/search.py`
- **Secrets leaked into stored memory:** the `_detect_secret` and PII redaction pipeline in `services/sanitize.py`
- **Arbitrary file read via export path:** the `export` CLI command and `--vault` flag

Out of scope: vulnerabilities that require local (same-user) access to the SQLite file, or require modifying installed package code.
