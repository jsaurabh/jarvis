# Security policy

Jarvis is a local agent with potentially broad access to files and macOS automation. It uses an OS-restricted agent, a trusted supervisor, and sandboxed workers. It is not a replacement for macOS access controls or protection against a compromised supervisor or other malicious software running as the same user.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting or a private security advisory. Do not include real API keys, personal traces, database files, or other sensitive user data in an issue.

## Security model

- Credentials are stored in macOS Keychain during normal app use.
- Writes, commands, clipboard access, Calendar access, and computer-control actions require explicit approval.
- macOS privacy controls remain authoritative.
- Runtime databases, traces, exports, user skills, and model artifacts are excluded from source control.
- Raw microphone audio and hidden model reasoning are not written to training datasets.
- File and native command workers use nono; Linux commands use Shuru VMs, offline by default. Native commands can execute project code and modify their approved working folder.
- The agent can read and write its runtime state, but is restricted from directly accessing other user files, launching programs, or connecting to the network. The supervisor enforces configured roots and binds approvals to exact, one-use requests. The supervisor and app are trusted and run as the same macOS user.
- The local model gateway keeps the real OpenAI key outside the restricted agent and forwards Responses API calls directly to OpenAI over verified TLS. Literal-key response filtering cannot guarantee removal of transformed secrets.
- VM snapshots exclude common credential names, but cannot identify all secrets. The supervisor builds previews from a size-limited temporary disk image. Text changes are applied one file at a time after separate review and a preimage check. A non-cooperating local writer can race the final check. Proposals expire after an hour or a supervisor restart.
- Approved VM HTTPS traffic is locally intercepted by Squid, with the Shuru host process restricted by nono to the proxy port. Shuru 0.11.0's own domain filter does not enforce the private-address restrictions Jarvis requires.
- Explicitly approved OpenAI credential access passes the app's existing in-memory key over a private pipe to a one-request credential gate; the guest receives only a temporary token. The gate limits endpoints, request size and response tokens, and sends through the local HTTPS proxy. Literal-key filtering does not prevent an upstream response from exposing transformed credentials.

Before enabling machine-wide roots or Full Disk Access, review the tool policy and run Jarvis only from source you trust.
