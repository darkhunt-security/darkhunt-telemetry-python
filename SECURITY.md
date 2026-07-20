# Security Policy

## Reporting a vulnerability

**Do NOT open a public GitHub issue.** Security issues are disclosed privately so they can be patched before details become public.

Email **security@darkhunt.ai** with:

- A clear description of the issue
- Steps to reproduce (the smallest possible repro is most useful)
- Affected SDK versions, if known
- Your assessment of impact (e.g. "leaks PII to dashboards", "RCE via crafted span attribute", etc.)
- Whether you'd like public credit in the resulting advisory

We acknowledge reports within **48 hours** and aim to ship a fix within **14 days** for high-severity issues. Longer timelines may apply for complex issues — we'll keep you informed.

PGP key for sensitive reports: available on request.

## Scope

In scope:

- The SDK code itself — data leaks, unsafe defaults, misuse of crypto primitives
- The bundled masking ruleset (`darkhunt_telemetry/masking/rules/rules.json`) — false-negatives that fail to redact sensitive data the rule claims to cover
- The validator implementations (Luhn, credit-card/IIN, ABA, IBAN mod-97, base58check, bech32, EIP-55) — incorrect validation that masks invalid inputs (false positives) or fails to mask valid inputs (false negatives)
- The vendored Keccak-256 fallback (`darkhunt_telemetry/masking/validators/keccak.py`) — used by the EIP-55 validator when `pycryptodome` is absent

Out of scope:

- Vulnerabilities in the upstream Darkhunt platform (trace-hub, attack-discovery, dashboards) — those have their own disclosure channel; email `security@darkhunt.ai` and we'll route appropriately
- Vulnerabilities in third-party dependencies — report upstream first (`opentelemetry-*`, `requests`, `pycryptodome`, etc.); we'll ship a dep bump once they fix
- Theoretical issues with no demonstrated exploit path
- Best-practice suggestions without an associated security impact (file as a regular issue)

## Supported versions

| Version              | Status                                                          |
| -------------------- | --------------------------------------------------------------- |
| Latest minor (`0.x`) | Security fixes shipped on demand                                |
| Older minors         | Best-effort; we'll pull patches forward if it's straightforward |

We don't currently maintain LTS branches. Because releases are continuous (every merge to `main` publishes `0.5.<build>` to PyPI), a fix reaches users on the next merge.

## Disclosure timeline

Default coordinated disclosure window: **90 days** from initial report, OR until a fix ships and downstream consumers have had reasonable time to upgrade — whichever comes first.

If the issue is being actively exploited in the wild, we'll move faster and may publish without waiting for the full window.

## Hall of fame

Security researchers who responsibly disclose issues are credited in the resulting GitHub Security Advisory and the [`CHANGELOG.md`](./CHANGELOG.md), unless they request anonymity.

Thank you for keeping the ecosystem safer.
