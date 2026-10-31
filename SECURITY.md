# Security policy

Jarvis is a local agent with potentially broad access to files and macOS automation. It is not a sandbox or a replacement for operating-system access controls.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting or a private security advisory. Do not include real API keys, personal traces, database files, or other sensitive user data in an issue.

## Security model

- Credentials are stored in macOS Keychain during normal app use.
- Writes, commands, clipboard access, Calendar access, and computer-control actions require explicit approval.
- macOS privacy controls remain authoritative.
- Runtime databases, traces, exports, user skills, and model artifacts are excluded from source control.
- Raw microphone audio and hidden model reasoning are not written to training datasets.

Before enabling machine-wide roots or Full Disk Access, review the tool policy and run Jarvis only from source you trust.
