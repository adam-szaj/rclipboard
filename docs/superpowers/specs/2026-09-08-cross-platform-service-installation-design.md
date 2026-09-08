# Cross-Platform Service Installation Design

## Goal

Provide one coherent, user-level installation process for rclipboard on Linux
and macOS. The process must install and start the service, support safe updates
from a configurable Git remote, support uninstall, and allow the installation
runtime to be reset without deleting user configuration or encryption keys by
default.

The change evolves the existing shell-based installer. It does not replace the
project's Python packaging or introduce a system-wide/root installation.

## Current State and Problems

The repository currently has two installers with duplicated responsibilities:

- `scripts/install.sh` installs the application without a service;
- `scripts/install-systemd-user.sh` repeats most of that work and additionally
  installs `systemd --user` units.

The existing process has the following inconsistencies:

- it has no macOS `launchd` integration;
- it has no supported update, uninstall, or reset operation;
- the two installers choose different user binary locations;
- documentation does not consistently match the installed layout;
- the shared configuration example is stored below a systemd-specific path;
- `scripts/install.sh` copies every top-level script instead of an explicit
  public set;
- `rclipboard-launcher` depends on `realpath`, which is not supplied by a stock
  macOS installation;
- the setup wizard contains Bash syntax that is not compatible with the Bash
  version supplied by macOS;
- the example configuration enables proxy mode even though the documented
  minimal installation is a local, non-proxy service.

These issues will be addressed only where they affect the installation and
service lifecycle. The application protocol and remote deployment subsystem are
outside this design.

## Supported Systems and Preconditions

The installer supports:

- Linux with a working per-user systemd instance and `systemctl --user`;
- macOS with `launchd` and `launchctl`;
- Python 3.11 or newer;
- a source checkout inside a non-bare Git repository.

Installation is user-local and must not require `sudo`. The installer detects
the operating system with `uname -s`; an unsupported system fails before any
managed files are changed. Git is required because the installed update command
updates from the recorded source checkout.

The installer accepts `PYTHON_BIN` as an explicit Python override. Tool
overrides used by tests, such as `SYSTEMCTL_BIN` and `LAUNCHCTL_BIN`, remain
supported but are not part of the normal user interface.

## Public Command Interface

### Install

From the source checkout:

```bash
./scripts/install.sh [--remote REMOTE] [--branch BRANCH] [--transport uds|tcp] [--no-start]
```

With no operation flag, the script performs an idempotent installation. The
default remote is `origin`, the default branch is the checkout's current branch,
and the default transport is `uds`. Installation from a detached HEAD is
rejected because it cannot define a safe default update target.

The installer validates the requested remote and verifies that the requested
branch exists locally or as a remote-tracking branch. `--transport tcp`
generates a new initial configuration using `127.0.0.1:8989`; it never selects
`0.0.0.0`. The option does not modify an existing `config.toml`.

Unless `--no-start` is passed, a successful install registers, enables, and
starts the platform service. Re-running install refreshes managed scripts,
package installation, metadata, and service definitions but preserves all user
configuration and keys.

### Update

Installation creates an executable `rclipboard-update` command. Its interface
is:

```bash
rclipboard-update [--remote REMOTE] [--branch BRANCH]
```

The command reads the source checkout, remote, and branch from installation
metadata. Command-line values override the recorded remote and branch. The
update then:

1. verifies that the source checkout still exists and is the recorded Git
   repository;
2. rejects a detached HEAD or a checked-out branch different from the selected
   update branch;
3. rejects any tracked or untracked worktree changes reported by
   `git status --porcelain`;
4. runs `git fetch <remote> <branch>`;
5. verifies that local `HEAD` is an ancestor of `<remote>/<branch>`;
6. advances the checkout with a fast-forward-only merge;
7. invokes the newly fetched repository installer to refresh the installed
   application and service definition;
8. restarts the service only after the package refresh succeeds;
9. persists an explicitly selected remote or branch only after the whole update
   succeeds.

The command never performs an implicit merge, rebase, hard reset, branch switch,
or stash. If local history has diverged, it prints a diagnostic and leaves the
repository and running service unchanged.

The update operation installs from the updated checkout, not from a Python
package index. The initial source path is retained even if the remote URL later
changes; remote selection uses the Git remote name, with `origin` as the initial
default.

### Reset

From the repository installer:

```bash
./scripts/install.sh --reset [--purge-user-data]
```

A normal reset stops and unregisters the service, removes managed runtime
artifacts, and performs a clean installation from the current checkout. It
preserves `config.toml`, encryption keys, and the known-key list.

`--purge-user-data` additionally selects these user-owned files for deletion:

- `config.toml`;
- `age_key.txt`;
- `age_key.pub`;
- `known_keys`.

Before deleting any selected user data, the installer prints the exact existing
paths and requires the user to type the literal lowercase text `yes` on an
interactive terminal. Any other input, end-of-file, or absence of an interactive
terminal cancels the reset before user data is removed. There is no flag or
environment variable that bypasses this confirmation. After a purging reset,
the installer creates a fresh default `config.toml`; encryption keys and the
known-key list remain absent.

### Uninstall

Installation creates an executable `rclipboard-uninstall` command:

```bash
rclipboard-uninstall [--purge-user-data]
```

It stops and unregisters the service and removes only managed installation
artifacts. By default it retains `config.toml`, encryption keys, and the known-key
list so a later install can reuse them. `--purge-user-data` applies the same
exact-path listing, interactive-terminal requirement, and literal `yes`
confirmation as reset.

The running uninstall script may unlink its own installed file after all earlier
steps have succeeded. Neither reset nor uninstall recursively removes the whole
application directory. Directories are removed only when empty.

## Installed Layout

The canonical application directory is:

```text
${XDG_CONFIG_HOME:-$HOME/.config}/rclipboard
```

The service receives its absolute `config.toml` path through
`RCLIPBOARD_CONFIG`, so a non-default `XDG_CONFIG_HOME` is respected even though
the Python application's historical fallback path is `~/.config/rclipboard`.

The managed layout is:

```text
<app-dir>/
├── bin/
│   ├── rclipctl
│   ├── rcliptunel
│   ├── rclipboard-launcher
│   ├── rclipboard-service-run
│   ├── rclipboard-setup
│   ├── rclipboard-update
│   └── rclipboard-uninstall
├── installer/
│   ├── install.sh
│   ├── common.sh
│   ├── systemd.sh
│   ├── launchd.sh
│   └── service templates required by those adapters
├── install.conf
├── config.toml
└── venv/
```

Only the explicitly listed public scripts are installed. Development, deploy,
smoke-test, and source installer scripts are not copied into `bin/`.

`install.conf` is mode `0600` and stores three plain `key=value` records:

```text
repo_dir=/absolute/path/to/checkout
remote=origin
branch=main
```

The parser recognizes only those exact keys and does not evaluate the file as
shell code. Values must be single-line strings. Remote and branch names are
validated with Git before use.

All installed executable files are mode `0755`; metadata and generated
environment files are mode `0600`; private runtime directories are mode `0700`.
The configuration file retains its existing mode when preserved and is created
as mode `0600` for a new installation.

No symlinks are placed in a second binary directory. Documentation tells users
to add `<app-dir>/bin` to `PATH`. This avoids the current disagreement between
`~/bin`, `~/.local/bin`, and the actual installed files.

## Shared Installer and Platform Adapters

`scripts/install.sh` owns argument parsing and the high-level transaction. A
small sourced common module owns path calculation, validation, copying the
explicit manifest, Python environment installation, metadata, confirmation, and
managed-file removal.

Two thin adapters expose the same operations:

```text
service_install
service_start
service_stop
service_restart
service_uninstall
```

The Linux adapter implements these operations using `systemctl --user`. The
macOS adapter implements them using `launchctl`. Platform-specific command
syntax and paths do not leak into the common installer.

`scripts/install-systemd-user.sh` remains as a compatibility wrapper. It emits a
deprecation notice and delegates to `scripts/install.sh` without duplicating
installation logic. Existing Makefile targets are retained as aliases and new
platform-neutral targets are added for install, update, reset, and uninstall.

## Shared Service Runner and Runtime Directory

Both service definitions execute the installed `rclipboard-service-run` script.
It:

1. resolves the application directory consistently with the installer;
2. exports `RCLIPBOARD_CONFIG=<app-dir>/config.toml`;
3. retains an existing `XDG_RUNTIME_DIR`, otherwise uses
   `$HOME/.local/run`;
4. creates `$XDG_RUNTIME_DIR/rclipboard` with mode `0700`;
5. sets `umask 077` so newly created runtime files and Unix sockets are private;
6. renders the resolved configuration environment to
   `$XDG_RUNTIME_DIR/rclipboard/env` with mode `0600` for shell clients;
7. replaces itself with `<app-dir>/venv/bin/rclipboard` using `exec`.

The client scripts use the same `XDG_RUNTIME_DIR` fallback, so they find the
same environment file on systems such as macOS that do not define the XDG
runtime variable. Existing Linux graphical sessions keep using their supplied
per-user runtime directory.

The application already loads `config.toml` itself. The generated runtime env
file exists to keep shell clients consistent and to expand values such as
`${XDG_RUNTIME_DIR}` in one place; it is not a second user-editable
configuration source.

## Linux Service Behavior

Linux installation generates or installs:

```text
~/.config/systemd/user/rclipboard.service
~/.config/systemd/user/rclipboard-display.service
```

The main unit runs `rclipboard-service-run`, uses the application directory as
its working directory, restarts on failure, and retains the current bounded stop
timeout. It sets a restrictive umask. Installation calls `systemctl --user
daemon-reload`, followed by `enable --now` unless `--no-start` was selected.

The display-environment publisher remains Linux-only and is not enabled unless
X11 integration is enabled or the user explicitly enables it. Reset and
uninstall disable and remove both managed units, reload the user manager, and do
not touch unrelated units.

Generated units contain a marker comment. An existing file at either target
path without that marker is treated as user-managed: installation and reset fail
without overwriting it, and uninstall leaves it unchanged.

## macOS Service Behavior

macOS installation generates:

```text
~/Library/LaunchAgents/com.rclipboard.service.plist
~/Library/Logs/rclipboard/stdout.log
~/Library/Logs/rclipboard/stderr.log
```

The plist identifies a per-user LaunchAgent and supplies absolute paths. Its
program is `rclipboard-service-run`; it has `RunAtLoad` and `KeepAlive` enabled
and records standard output and error in the user log directory. The service
does not require root privileges and is active only in the user's login session.

The adapter addresses the graphical launchd domain as `gui/<uid>`. It uses
`bootout` when replacing or removing a loaded job, `bootstrap` to load it, and
`kickstart` when an explicit restart is required. A missing loaded job during
bootout is not an error. Other launchctl failures are reported.

When installation is performed over SSH and the user's GUI domain is not
available, the plist and all installation files are still installed. The
installer prints a warning that immediate activation was unavailable and exits
successfully; launchd will discover the LaunchAgent at the user's next graphical
login. Failures in an existing GUI domain are errors rather than warnings.

The plist includes a generated marker. The same unmanaged-file collision rules
as systemd apply. macOS does not install the X11 display publisher and this
design does not add native macOS clipboard synchronization.

## Transport Policy

The initial configuration defaults to a Unix Domain Socket:

```toml
[server]
endpoint = "uds://${XDG_RUNTIME_DIR}/rclipboard/uds.sock"

[client]
transport = "uds"
```

There is no automatic runtime fallback from UDS to TCP. Silent fallback could
change the service's network exposure and could make clients communicate with a
different listener than intended.

When UDS forwarding is unavailable, the user may select TCP during initial
installation or edit `config.toml` explicitly. The safe fallback is:

```toml
[server]
endpoint = "127.0.0.1:8989"

[client]
transport = "tcp"
endpoint = "127.0.0.1:8989"
```

This listener is reachable through an SSH local-forward such as
`ssh -L 8989:127.0.0.1:8989 host` but is not exposed on non-loopback network
interfaces. An existing configuration is never rewritten merely because
`--transport` is supplied on a later idempotent install.

The shared configuration example moves to a platform-neutral scripts directory.
The old systemd-specific example path remains temporarily available as a copy or
compatibility link for repository consumers, but all maintained code refers to
the neutral path.

The new example disables proxy and X11 by default. The setup wizard is made
compatible with the stock macOS shell and offers UDS first and loopback TCP as
the explicit fallback.

## Failure Handling and Data Ownership

All preflight validation occurs before service or installation files are
modified. Package installation completes before an existing service is
restarted. A failure during package installation leaves the currently running
process untouched and returns a nonzero status with the failed command
identified.

The installer owns only files listed in its explicit manifest and generated
service files containing its marker. It may replace those managed files during
install, update, or reset. It never claims or deletes an unmarked service file.

User data is defined narrowly as `config.toml`, `age_key.txt`, `age_key.pub`,
and `known_keys`. These files are preserved across normal lifecycle operations.
Other unknown files below the application directory are never removed. After
managed files have been deleted, empty directories may be removed with `rmdir`;
recursive deletion of the application directory is prohibited.

The update command cannot make the Git checkout fully transactional: after a
successful fast-forward, a later package installation may fail. In that case the
checkout remains at the fetched commit, the service is not deliberately
restarted, and the command explains that rerunning update or reset will retry
installation. The installer does not rewrite Git history to roll back the
checkout.

## Testing Strategy

Automated tests extend the current installer suite and cover:

- Linux and macOS platform detection;
- the exact managed directory and executable manifest;
- valid generated systemd units and LaunchAgent plist data;
- parsing the plist with Python `plistlib`;
- `systemctl --user` and `launchctl` calls through test doubles;
- successful installation without starting a service;
- idempotent reinstall preserving configuration and keys;
- ordinary reset preserving configuration and keys;
- purging reset deleting the four defined user-data files only after exact
  interactive `yes` input;
- rejection of other confirmation text and non-interactive purge attempts;
- uninstall preserving data by default and purging it with the same safeguards;
- update against temporary local Git repositories and remotes;
- remote and branch override persistence after success;
- dirty-worktree, detached-HEAD, missing-remote, and non-fast-forward rejection;
- compatibility delegation from `install-systemd-user.sh`;
- shell syntax checks for every changed shell script;
- the existing real-systemd container installation and health check.

Tests mock launchctl on Linux and validate the generated plist structurally.
A manual macOS smoke procedure documents installation, `launchctl print`, UDS
health, restart, update from a temporary remote, reset, and uninstall. Running
that procedure on a real macOS host is required before claiming real-launchd
integration is proven; Linux-only automated results will be described as such.

## Documentation Changes

README installation sections are consolidated into a single cross-platform
flow. They document prerequisites, default paths, service status commands,
explicit TCP fallback, update, reset, uninstall, and the destructive-data
confirmation rule.

`docs/CONFIGURE.md` stops presenting systemd as the only installation path and
shows equivalent service operations for Linux and macOS. Stale references to
nonexistent socket/proxy units and inconsistent binary directories are removed
or corrected. The Makefile help describes the platform-neutral entry points and
keeps the legacy systemd target labeled as a compatibility alias.

## Explicit Non-Goals

This change does not:

- add native macOS clipboard integration;
- add a system-wide service or require root access;
- change HTTP, WebSocket, UDS, proxy, or encryption protocols;
- redesign `rclip-systemctl` or make it manage launchd;
- change the existing remote host deployment workflow;
- resolve Git divergence automatically;
- choose TCP automatically after a UDS failure;
- delete unrecognized user files.

## Acceptance Criteria

The design is complete when all of the following are true:

1. The same install command produces a working user service definition on Linux
   and macOS.
2. A new installation defaults to a private UDS endpoint; loopback TCP is an
   explicit supported fallback.
3. Install and update preserve existing configuration and keys.
4. The installed update command advances only through a clean fast-forward from
   its recorded, configurable Git remote and branch.
5. The installed uninstall command removes managed artifacts and preserves user
   data by default.
6. Reset recreates the runtime and service while preserving user data by
   default.
7. Reset and uninstall delete configuration or keys only after an interactive,
   exact `yes` confirmation.
8. No lifecycle operation overwrites or removes an unmarked service definition
   or an unknown user file.
9. Linux automated and real-systemd tests pass; macOS plist and launchctl-command
   tests pass in the portable suite, with real-macOS verification reported
   separately.
10. README, configuration documentation, installed paths, and Makefile help
    describe the same commands and behavior.
