# Security policy

## Reporting a vulnerability

Please **do not open a public issue**. Report it privately through GitHub:
**Security → Report a vulnerability** on this repository (a private security advisory). Include what you found,
how to reproduce it and what an attacker gains. You get an answer within a week; a fix follows once it is
confirmed, with credit if you want it.

## Supported versions

Only the latest version on `main` gets fixes.

## What is in scope

Anything that exposes your data or lets someone else run code in the container: reaching the API without the
dashboard's token, reading secrets (webhook URLs, CV, search settings), or injection through scraped ad content.

Out of scope: portals changing or blocking access, and sites' terms of service. This code scrapes third-party
sites; use it at your own risk (see the README).
