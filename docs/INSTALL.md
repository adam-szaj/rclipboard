# Installing the rclipboard user service

This guide covers the private, per-user service installation on Linux and
macOS. The installer never needs `sudo` and does not install a system-wide
service.

## Requirements

- Python 3.11 or newer, with `venv` and `pip`
- Git and a non-bare Git checkout of this repository
- Bash
- Linux: a working systemd user manager and `systemctl --user`
- macOS: `launchd`, `launchctl`, and the system-provided `/usr/bin/pbcopy`
  and `/usr/bin/pbpaste`

The source checkout must have an attached branch. Git is required after
installation because updates are installed directly from that checkout.
`xsel` is optional and supports Linux/X11 clipboard publishing only.

## Install from a Git checkout

From the repository root, run:

```bash
./scripts/install.sh
```

This installs, registers, enables, and starts the user service. To install all
artifacts without starting it, run:

```bash
./scripts/install.sh --no-start
```

The initial update source defaults to remote `origin` and the currently
checked-out branch. They can be selected explicitly during installation:

```bash
./scripts/install.sh --remote REMOTE --branch BRANCH
```

Re-running the command refreshes only installer-managed runtime and service
files. It preserves configuration, encryption keys, and unknown files. The
installer also refuses to overwrite an existing service definition that does
not carry its generated-file marker.

## Installed layout and PATH

The private application directory is:

```text
${XDG_CONFIG_HOME:-$HOME/.config}/rclipboard
```

It contains `bin/` (public commands), `venv/` (the dedicated Python
environment), `installer/` (lifecycle scripts and service templates),
`install.conf` (the recorded checkout, remote, and branch), and `config.toml`.
Only the documented public commands are copied into `bin/`; no second binary
directory or symlink set is created.

Add the installed commands to the current shell's `PATH`:

```bash
RCLIPBOARD_BIN="${XDG_CONFIG_HOME:-$HOME/.config}/rclipboard/bin"
export PATH="$RCLIPBOARD_BIN:$PATH"
```

Add the `export` to the appropriate shell startup file for future sessions.
The application directory, `bin/`, and `installer/` are mode `0700`; new
configuration and metadata files are mode `0600`; installed executables are
mode `0755`. Runtime files are created under
`${XDG_RUNTIME_DIR:-$HOME/.local/run}/rclipboard`; that directory is mode
`0700`, its generated environment file is mode `0600`, and the service runs
with umask `077`.

## Linux systemd user service

Linux installs the marked units `rclipboard.service` and
`rclipboard-display.service` below
`${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user`. The main service is enabled
and started by default. The display publisher is enabled only when X11
integration is configured.

Inspect status and logs with:

```bash
systemctl --user status rclipboard.service
journalctl --user -u rclipboard.service -f
```

Restart after editing `config.toml`:

```bash
systemctl --user restart rclipboard.service
```

## macOS LaunchAgent

macOS installs the per-user LaunchAgent at:

```text
~/Library/LaunchAgents/com.rclipboard.service.plist
```

Inspect it in the current graphical login domain with:

```bash
launchctl print gui/$(id -u)/com.rclipboard.service
```

Standard output and error go to:

```text
~/Library/Logs/rclipboard/stdout.log
~/Library/Logs/rclipboard/stderr.log
```

An install performed through SSH can complete when the graphical launchd
domain is unavailable. In that case the installer warns that it could not load
the job immediately; the LaunchAgent is available at the next graphical login.
After editing `config.toml`, restart a loaded job with:

```bash
launchctl kickstart -k gui/$(id -u)/com.rclipboard.service
```

The same LaunchAgent also synchronizes rclipboard topic `c` through
`/usr/bin/pbcopy` and `/usr/bin/pbpaste`. The adapter handles text-compatible
pasteboard content only. It is enabled for a new macOS configuration and needs
no extra package. Rich text, images, file references, and other pasteboard
types are outside this adapter's scope. The xsel publisher remains
Linux/X11-only.

Re-running installation does not rewrite an existing `config.toml`. To enable
the adapter in an older installation, add or edit:

```toml
[pasteboard]
enabled = true
pbcopy_path = "/usr/bin/pbcopy"
pbpaste_path = "/usr/bin/pbpaste"
interval_ms = 250
```

Then restart the LaunchAgent and verify `pasteboard_enabled` and
`pasteboard_good` with `rclipctl health`.

## Default UDS transport

A new installation binds the server to the private Unix Domain Socket:

```text
uds://${XDG_RUNTIME_DIR}/rclipboard/uds.sock
```

The client transport is also `uds`. The private runtime directory and umask
policy described above restrict access to the installing user. Check the
configured endpoint with:

```bash
rclipctl health
```

There is no automatic UDS-to-TCP fallback. A silent fallback could expose a
different listener or connect the client to an unintended service.

## Loopback TCP fallback for SSH tunneling

If the local or remote SSH configuration cannot forward a UDS, explicitly
select TCP while creating the initial configuration:

```bash
./scripts/install.sh --transport tcp
```

This binds only `127.0.0.1:8989`, never a non-loopback interface. It can be
carried by an SSH local forward such as:

```bash
ssh -L 8989:127.0.0.1:8989 user@host
```

Transport selection is initialization-only: a later transport flag does not
rewrite an existing configuration. In particular, it does not rewrite an
existing `config.toml`. Change that file deliberately, then restart the
platform service, when an existing installation must switch transports.

## Update remote and branch

The installed command is:

```text
rclipboard-update [--remote REMOTE] [--branch BRANCH]
```

Without options it uses the checkout, remote, and branch recorded at the last
successful installation or update; the initial remote is `origin`. Overrides
are recorded only after the complete update succeeds.

An update requires the attached selected branch to be checked out and a
completely clean worktree, including no untracked files. It fetches the
selected remote branch and advances the checkout by fast-forward only. It does
not stash, rebase, reset, or switch branches, and it refuses diverged history.
After the fast-forward, it refreshes the installed package, scripts, metadata,
and service definition, then restarts the service.

The Git checkout and service refresh cannot form one transaction. If refresh
fails after Git advances, the checkout remains updated and the running service
is not deliberately restarted. The command reports `checkout was updated but
installation refresh failed`; correct the problem and retry
`rclipboard-update` or reset the installation.

## Reset installation

Use:

```text
./scripts/install.sh --reset [--purge-user-data]
```

Ordinary reset stops and unregisters the service, recreates all managed runtime
artifacts from the current checkout, and starts the service again. It preserves
`config.toml`, `age_key.txt`, `age_key.pub`, and `known_keys`.

`--purge-user-data` requests removal of those four files before installation.
After confirmation, reset creates a fresh default `config.toml`; the key files
remain absent.

## Uninstall

Use the installed command:

```text
rclipboard-uninstall [--purge-user-data]
```

Ordinary uninstall stops and unregisters the platform service and removes only
the managed runtime, installed commands, installer payload, and metadata. It
preserves `config.toml`, `age_key.txt`, `age_key.pub`, `known_keys`, and any
unknown files, so a later install can reuse them.

## Configuration/key deletion safeguard

For reset or uninstall, `--purge-user-data` first lists the exact existing
paths among:

```text
${XDG_CONFIG_HOME:-$HOME/.config}/rclipboard/config.toml
${XDG_CONFIG_HOME:-$HOME/.config}/rclipboard/age_key.txt
${XDG_CONFIG_HOME:-$HOME/.config}/rclipboard/age_key.pub
${XDG_CONFIG_HOME:-$HOME/.config}/rclipboard/known_keys
```

Deletion requires typing the literal lowercase `yes` through an interactive
terminal. `Yes`, `YES`, any other text, end-of-file, input through a pipe, and
non-interactive execution all cancel before user data or managed installation
artifacts are removed. There is no bypass flag or environment variable.

## Troubleshooting

- `update requires a clean worktree`: commit, discard, or relocate every
  tracked and untracked change yourself, then retry. The updater will not stash
  it for you.
- `update requires branch ... to be checked out`: attach and check out the
  selected branch explicitly before updating.
- `local and remote history cannot be fast-forwarded`: resolve the Git history
  manually. Update deliberately does not merge or reset it.
- `refusing to overwrite unmanaged file`: inspect the existing systemd unit or
  LaunchAgent plist. The installer will not claim a file without its marker.
- Linux: use `systemctl --user status rclipboard.service` and
  `journalctl --user -u rclipboard.service`.
- macOS: use `launchctl print gui/$(id -u)/com.rclipboard.service` and inspect
  `~/Library/Logs/rclipboard/stdout.log` and
  `~/Library/Logs/rclipboard/stderr.log`.
- If UDS forwarding is prohibited by SSH server policy, use the explicit
  loopback TCP setup above. The service will not switch automatically.

## Real macOS smoke procedure

Run this procedure on a disposable real macOS user account from an attached,
clean test branch. It is required before claiming real launchd validation. It
has not been run as part of the Linux automated test suite; portable tests only
validate plist structure and mocked `launchctl` commands.

1. Install with `./scripts/install.sh` and confirm
   `launchctl print gui/$(id -u)/com.rclipboard.service` succeeds.
2. Run `rclipctl health` and confirm it reaches the default UDS endpoint below
   `${XDG_RUNTIME_DIR:-$HOME/.local/run}/rclipboard/uds.sock`, with
   `pasteboard_enabled` and `pasteboard_good` both true.
3. Run `printf 'mac-local' | pbcopy`, wait one second, and confirm
   `rclipctl get -c` returns `mac-local`. Then run
   `printf 'rclipboard-remote' | rclipctl put -c`, wait one second, and confirm
   `pbpaste` returns `rclipboard-remote`.
4. Restart with
   `launchctl kickstart -k gui/$(id -u)/com.rclipboard.service`, then repeat the
   health check and inspect both files in `~/Library/Logs/rclipboard/`.
5. Point a temporary named Git remote and branch at a controlled test commit,
   then run `rclipboard-update --remote REMOTE --branch BRANCH`. Confirm the
   checkout fast-forwards, the service restarts, and `rclipctl health` succeeds.
6. Run `./scripts/install.sh --reset`; confirm user configuration and keys are
   unchanged, the LaunchAgent is loaded, and health succeeds.
7. Run `rclipboard-uninstall --purge-user-data`, type text other than lowercase
   `yes`, and confirm cancellation leaves the service and all listed files
   intact.
8. Run `rclipboard-uninstall` without purge; confirm the LaunchAgent and managed
   runtime are removed while configuration and keys remain.
