# Security Policy

## Supported Versions

| Version | Supported          |
|:--------|:-------------------|
| 3.x     | Yes                |
| < 3.0   | No                 |

## Reporting a Vulnerability

If you discover a security vulnerability in NEXUS, please report it responsibly.

**Email:** pruthvig1998@gmail.com

Please include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

## Responsible Disclosure Timeline

- **Acknowledgment:** Within 48 hours of report
- **Initial assessment:** Within 7 days
- **Fix target:** Within 30 days for critical issues, 90 days for non-critical
- **Public disclosure:** After fix is released, or after 90 days (whichever comes first)

We ask that you do not publicly disclose the vulnerability until we have had a chance to address it.

## Scope

The following areas are in scope for security reports:

- Broker API integration and credential handling
- Signal processing and order execution logic
- Data handling and storage (trade logs, databases)
- Configuration and environment variable management
- Dependencies with known vulnerabilities

## Out of Scope

The following are out of scope:

- Third-party broker platforms themselves (Alpaca, Moomoo, IBKR, Webull)
- Third-party services (Discord, Twitter/Nitter)
- Issues in upstream dependencies that are not exploitable through NEXUS
- Theoretical attacks without a proof of concept
