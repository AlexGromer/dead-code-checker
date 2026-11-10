# Security Policy

## Reporting a vulnerability

Please report security issues **privately**, not through public GitHub issues, pull requests or
discussions.

Use **GitHub private vulnerability reporting**: open the repository's *Security* tab and choose
*Report a vulnerability*, or go straight to
<https://github.com/AlexGromer/dead-code-checker/security/advisories/new>.

Please include:

- the affected component, and a version or commit SHA;
- steps to reproduce, with a minimal example if you can;
- the impact as you understand it (what an attacker gains);
- a suggested fix, if you have one.

Do not include real secrets, credentials or personal data in the report; redact them.

## What to expect

- We aim to acknowledge a report within a few business days.
- We will work with you to confirm and understand the problem, and keep you informed about the fix.
- The fix is prepared privately and released together with an advisory; you are credited in the
  advisory unless you prefer to stay anonymous.
- Please give us reasonable time to release a fix before any public disclosure.

## Scope

In scope: the code in this repository, the dependencies it ships with, its build and release
pipeline, and the artifacts published from it.

Out of scope: third-party services and infrastructure, social engineering, and denial of service
by sheer traffic volume.

## Supported versions

Only the latest release and the `main` branch receive security fixes. If the project has not made a
stable release yet, pin a commit SHA for reproducible use.
