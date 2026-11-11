# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub:
**Security → Report a vulnerability** on this repository
([private vulnerability reporting](https://github.com/superkaliiii/terminal-conteneur/security/advisories/new)).
Do not open a public issue for a security problem.

Include the terminal-ct version (`terminal-ct version`), the engine and OS, the steps that show the problem, and what
an attacker gains. `terminal-ct doctor --report` writes a report with secrets, the home folder, user and host names
masked: attach it if it helps.

You should get a first answer within 7 days. A fix is released as soon as it is ready, and the advisory is
published with it, crediting you unless you prefer otherwise.

## Supported versions

Only the latest release receives security fixes.

## Scope

In scope: what terminal-ct itself enforces, for example

- the sandbox fences (dropped capabilities, `no-new-privileges`, process limit, non-root user, internet off);
- the authority rules that decide what an agent may run through MCP without asking;
- the agent, relay and web dashboard: TLS, fingerprints, tokens, users and their roles;
- files terminal-ct writes (tokens, keys, users) and their permissions.

Out of scope: escapes through the container engine or the kernel themselves (report them to Podman, Docker or the
kernel), and anything that needs an account already allowed to run terminal-ct or the engine on that PC.

A container is not a virtual machine: for untrusted code that must not reach the host kernel, use a VM of
[terminal-vm](https://github.com/superkaliiii/terminal-vm).
