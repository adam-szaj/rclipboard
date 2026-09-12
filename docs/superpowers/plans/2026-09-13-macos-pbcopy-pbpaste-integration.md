# macOS pbcopy/pbpaste Integration — Implementation Plan

**Goal:** Synchronize the macOS text clipboard bidirectionally with rclipboard
topic `c` from the existing per-user LaunchAgent.

**Base:** `main` at `537700a`; implementation branch
`feat/macos-pasteboard-integration` in the dedicated
`.worktrees/macos-pasteboard-integration` worktree.

## Scope and decisions

- Use stock `/usr/bin/pbpaste` and `/usr/bin/pbcopy`; add no dependency.
- Synchronize only topic `c`. macOS has no X11 primary/secondary selections.
- Support text-compatible pasteboard data. Images, file lists, rich UTI data,
  and native `NSPasteboard.changeCount` notifications are out of scope.
- Poll locally, default every 250 ms. Keep `seen` and `applied` bytes so a
  remote write is not immediately published back as a new local change.
- Implement a separate `PasteboardInterface(BidirectionalInterface)`; do not
  add platform branches to `XselInterface` or duplicate service management.
- A new macOS installation enables the adapter. Linux leaves it disabled.
  Existing `config.toml` files remain untouched and require an explicit
  `[pasteboard] enabled = true` opt-in.
- The existing LaunchAgent hosts both server and adapter. No second service or
  privileged component is introduced.
- Additive health/status fields preserve the existing API contract.

## Stage 1 — Configuration and installer contract

**Files:**

- `src/rclipboard/config.py`
- `scripts/config/rclipboard.conf.example`
- `scripts/install.sh`
- `tests/test_installer.py`
- new `tests/test_pasteboard.py`

1. Add failing tests for `[pasteboard]` → environment mapping:
   `enabled`, `pbcopy_path`, `pbpaste_path`, and `interval_ms`.
2. Add the disabled cross-platform template section and exact environment
   mappings.
3. Add failing installer tests proving that a new Darwin config enables the
   adapter, a new Linux config does not, and reinstall preserves an existing
   choice.
4. Implement initialization-only Darwin enablement without changing existing
   configurations.
5. Verify config tests and the full installer/update suite, then commit.

**Success:** generated macOS configuration contains
`[pasteboard] enabled = true`; Linux and all existing installations retain
their prior behavior.

## Stage 2 — Bidirectional pasteboard adapter

**Files:**

- new `src/rclipboard/transports/pasteboard.py`
- `tests/test_pasteboard.py`

1. Add failing asynchronous tests for availability gating, `/usr/bin/pbpaste`
   reads, `/usr/bin/pbcopy` writes, binary/base64 conversion, topic filtering,
   subprocess failure/timeout handling, and shutdown.
2. Add tests proving local changes enqueue topic `c`, remote changes write the
   pasteboard, and the resulting echo is suppressed.
3. Implement the smallest adapter using the existing
   `BidirectionalInterface`, `enqueue_topic_data`, `register_client`, and
   `subscribe_client` contracts.
4. Verify only the adapter tests first, then adjacent xsel tests, then commit.

**Success:** mocked local and remote clipboard changes synchronize once in each
direction without a feedback loop or blocking the event loop indefinitely.

## Stage 3 — Runtime lifecycle, health, and status

**Files:**

- `src/rclipboard/main.py`
- `src/rclipboard/core/state.py`
- `src/rclipboard/models/wire.py`
- `src/rclipboard/transports/http.py` only if detailed monitor status requires it
- `tests/test_pasteboard.py`
- `tests/test_functional_http.py`

1. Add failing tests for startup registration, shutdown cancellation, health
   booleans, detailed status, and Linux-disabled behavior.
2. Install/shutdown the module alongside the other optional interfaces.
3. Add `pasteboard_enabled`, `pasteboard_good`, and a detailed `pasteboard`
   status object without changing existing response fields.
4. Ensure `/v1/health.get` and `/v1/status.get` expose the new state.
5. Verify focused lifecycle/API tests and commit.

**Success:** the adapter participates in the normal dispatcher lifecycle and
its state is observable without breaking current clients.

## Stage 4 — Setup flow and documentation

**Files:**

- `scripts/bin/rclipboard-setup`
- `README.md`
- `docs/CONFIGURE.md`
- `docs/INSTALL.md`
- `tests/test_installer.py`

1. Add failing contract tests for the Darwin setup default and documentation.
2. Make the Bash 3.2-compatible wizard offer/enable native text clipboard sync
   on Darwin while keeping xsel Linux-only.
3. Replace the old “server only/no native synchronization” statement with the
   exact text-only behavior, configuration, commands, status, limitations, and
   existing-config opt-in instructions.
4. Keep explicit absolute pbcopy/pbpaste paths in examples because launchd does
   not inherit an interactive shell PATH.
5. Verify wizard, documentation, and shell syntax tests, then commit.

**Success:** a macOS user can install or configure the feature without knowing
internal environment variables, and limitations are explicit.

## Stage 5 — Full verification and platform limit

1. Run `bash -n` for every touched shell script.
2. Run `tests.test_pasteboard`, `tests.test_xsel_display_env`,
   `tests.test_installer`, and `tests.test_installer_update`.
3. Run affected functional HTTP/WS tests and the repository-wide `make test`.
4. Run `make test-install-docker` and
   `make test-install-docker-systemd` to prove Linux remains unaffected.
5. Run `git diff --check`, inspect the entire diff from the merge base, and
   obtain an independent final code review.
6. If no real macOS host is available, report exactly that pbcopy/pbpaste
   subprocess behavior and launchd integration were verified with portable
   mocks, but an end-to-end real macOS pasteboard session was not run.

**Success:** all portable, Linux, and regression checks pass; real-macOS
coverage is claimed only when actually executed.
