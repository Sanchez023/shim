# SSH Remote plugin

Adds an `ssh_exec` tool that runs remote commands over SSH with OpenSSH
ControlMaster/ControlPersist connection reuse.

## Why this exists

Native OpenSSH on Windows does not support ControlMaster, and the MSYS/Git Bash
build creates a control socket but resets every multiplexed session. On
Windows this plugin therefore routes through **WSL's Linux OpenSSH**, which
supports multiplexing (verified: first command ~2 s, reused connection ~0.08 s).
On macOS/Linux it uses the native `ssh` directly.

## Requirements

- Key-based SSH authentication (the tool runs with `BatchMode=yes`).
- Windows: a WSL distribution with the target host key/known_hosts configured.
  The first connection uses `StrictHostKeyChecking=accept-new` by default.
- The WSL user's default keys are used unless `identity_file` is supplied as a
  **WSL path** (for example `/home/you/.ssh/id_ed25519`).

## Install

```bash
oh plugin install ./plugins/ssh-remote
```

## Usage

Ask the model to use `ssh_exec`, or call it directly with:

```json
{
  "host": "example.com",
  "user": "root",
  "port": 22,
  "command": "hostname && uptime"
}
```

The master for a given `user@host:port` stays alive for `persist_seconds`
(default 600), so subsequent calls skip TCP + authentication. Set it to `0` to
disable persistence.

Inspect or stop a master from WSL. Wrap the command in the WSL shell so Git
Bash does not rewrite `/tmp/...` into a Windows path:

```bash
wsl -e bash -lc 'ssh -O check -o ControlPath=/tmp/oh-ssh-%C -p 22 root@example.com'
wsl -e bash -lc 'ssh -O exit  -o ControlPath=/tmp/oh-ssh-%C -p 22 root@example.com'
```

The tool is not read-only and follows the normal permission prompt flow.
