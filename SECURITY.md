# Security policy

**This repository is under pre-release security review.** Do not submit credentials, private screenshots, personally identifiable data, unpublished media or raw Quran evaluator outputs in issues or pull requests.

## Report vulnerabilities responsibly

Use GitHub's private vulnerability reporting or a private security advisory for this repository **if that feature is enabled**. If no private channel is configured, ask a maintainer to enable one before submitting sensitive reproduction details. Do not post exploitable payloads or secrets in public issues.

## Developer responsibilities

- Keep real `.env` values out of Git; use scoped server-only service-role secrets, least privilege and production separation.
- Do not ship private local `.codex`, `.ai-bridge`, `.local`, downloaded provider datasets, recordings or browser state.
- Verify source provenance, translation version, source hashes, passage boundaries and media rights before any public output.
- Separate preview and test execution from publication. A `test` run may upload to B2, so use isolated fakes for a genuinely offline audit.
- The owner-frozen 2,211 IDs are the final v1 selection; do not silently substitute older verse-selection lists or relax Quran text/audio/context, rights, platform confirmation or visual safety checks.
- Confirmed publication and cycle rollover require authenticated, idempotent primary-platform receipts, not generation/upload success.

## Status

A preliminary source/secret-pattern review is not an exhaustive security audit. No security certification is claimed until the complete repository and any historical commits are reviewed in an isolated, repeatable audit environment.
