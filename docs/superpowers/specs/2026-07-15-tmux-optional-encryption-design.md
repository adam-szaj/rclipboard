# Design: optional encryption in the tmux-rclipboard plugin

Date: 2026-07-15
Status: approved (pending spec review)

## Problem

The tmux plugin (`../tmux-rclipboard`, a separate git submodule) always
copies plaintext (`copy.sh` calls `rclipctl put` with no encryption) and
always auto-decrypts on paste (`paste.sh` calls `rclipctl get`, which
auto-decrypts when the value is encrypted). A user cannot copy an encrypted
value through the plugin, and cannot opt out of auto-decryption on paste.

We want the plugin to expose, without changing any default key bindings:
`copy-clear`, `copy-encrypted`, `copy-default`, `paste-clear`,
`paste-encrypted`, `paste-default`, `rclipboard-register`,
`rclipboard-unregister`.

## Decisions (from brainstorming)

- **Extend existing scripts** with a `--mode` argument rather than creating six
  separate files. `copy.sh` / `paste.sh` gain `--mode <clear|encrypted|default>`.
- **`default` follows the plugin option `@rclip_encrypt`** (`on`/`off`, default
  `off`) that the user sets in their `.tmux.conf`. `copy-default` /
  `paste-default` resolve the mode from this option at call time.
- **`rclipctl get` no longer auto-decrypts** — decryption becomes opt-in via a
  new `--decrypt` flag. This is the safer default (a plaintext dump never
  silently decrypts a secret) and is a **breaking change** for every `get`
  consumer, not just tmux. We update the user-facing docs and tests to match.
- **paste-clear** = plain `rclipctl get` (now returns the stored value as-is,
  no decryption). No new flag needed.
- **paste-encrypted** = `rclipctl get --decrypt` (explicitly decrypts).
- **No default binding changes** — the plugin only *provides* the commands
  (as `@rclip_*_cmd` tmux options); the user wires up keys themselves.
- **register via `display-popup`** — interactive prompt for the admin token
  (and optional label, default hostname), so the token never lands in shell
  history or config.
- **unregister is local-only + a rclipctl stub** — removes the local keypair;
  server-side key removal is a TODO (server registry is in-memory, keys vanish
  on restart anyway).
- **Server TODO (not this task):** registered keys that are not renewed within
  `key-ttl` are auto-evicted; renewal via `rclipctl register --renew` (refreshes
  `registered_at`); a `keys.delete` endpoint for full E2E unregister.

## Behaviour matrix

| Command | Action |
|---------|--------|
| copy-clear | `rclipctl put` (plaintext, current behaviour) |
| copy-encrypted | `rclipctl put -E --fetch-keys` |
| copy-default | resolve `@rclip_encrypt`: on→encrypted, off→clear |
| paste-clear | `rclipctl get` (no decryption — the new default), then paste |
| paste-encrypted | `rclipctl get --decrypt`, then paste |
| paste-default | resolve `@rclip_encrypt`: on→paste-encrypted, off→paste-clear |
| rclipboard-register | `display-popup` prompt → `rclipctl register --token ...` |
| rclipboard-unregister | `rclipctl unregister` (stub) + remove local keypair |

## Components

### Plugin repo (`../tmux-rclipboard`)

**`scripts/copy.sh --mode <clear|encrypted|default>`**
- No arg / `clear` → `exec rclipctl put -t $TOPIC --app $APP` (unchanged path,
  backwards compatible: the existing key binding keeps working untouched).
- `encrypted` → `exec rclipctl put -t $TOPIC --app $APP -E --fetch-keys`.
- `default` → read `@rclip_encrypt` via `tmux show-option -gqv`; dispatch to
  clear or encrypted.
- Depends on: `rclipctl`, tmux (for reading the option), stdin (the selection).

**`scripts/paste.sh --mode <clear|encrypted|default>`**
- `clear` → `rclipctl get -t $TOPIC` (no decryption, the new default),
  `tmux load-buffer` + `paste-buffer -d`.
- `encrypted` → `rclipctl get -t $TOPIC --decrypt`.
- `default` → resolve `@rclip_encrypt` → clear or encrypted.
- Depends on: `rclipctl`, tmux.

**`scripts/register.sh`**
- `tmux display-popup -E` running an inner prompt: read admin token (silent),
  optional label (default `hostname`), then `rclipctl register --token <tok>
  [--label <lbl>]`. Report success/failure inside the popup.
- Depends on: `rclipctl`, tmux ≥ 3.2 (`display-popup`).

**`scripts/unregister.sh`**
- Call `rclipctl unregister` (stub, exits 0). Then remove the local keypair
  (`age_key.txt` / `age_key.pub`) after confirmation in a `display-popup`.
  Print that server-side removal is a TODO.
- Depends on: `rclipctl`, tmux.

**`rclipboard.tmux`**
- Expose every command as a tmux option so users can bind it:
  `@rclip_copy_clear_cmd`, `@rclip_copy_encrypted_cmd`, `@rclip_copy_default_cmd`,
  `@rclip_paste_clear_cmd`, `@rclip_paste_encrypted_cmd`, `@rclip_paste_default_cmd`,
  `@rclip_register_cmd`, `@rclip_unregister_cmd`.
- Introduce `@rclip_encrypt` (default `off`).
- **Do NOT change existing `bind-key` lines.** The current `y`/`Enter`/`]`
  bindings stay exactly as they are (they map to the clear path).
- README: document the new options and show example bindings the user can copy.

### Server repo (rclipctl, this worktree)

**`scripts/bin/rclipctl` — new `unregister` subcommand (stub)**
- Removes the local `age_key.txt` / `age_key.pub` if present; prints that
  server-side key removal is not yet implemented (registry is in-memory).
  Exits 0 so the tmux script can always call it.

**`scripts/bin/rclipctl` — `register --renew` flag**
- For now an alias of plain `register` (re-publishes the key). Full
  renew/TTL semantics are a server TODO. Documented as such.

**`scripts/bin/rclipctl` — `get` default changes to NO decryption; add `--decrypt`**
- BREAKING: `call_get` no longer auto-decrypts when `is_encrypted=true`. By
  default it returns the stored (possibly ciphertext) value through the normal
  output-encoding path. The `--decrypt` flag restores the old `age --decrypt`
  behaviour (requires the local key; errors as today if missing/mismatched).
- All user-facing docs describing "get auto-decrypts" are updated (README.md,
  docs/USAGE.md, docs/CONFIGURE.md, rclipctl usage text): the encryption
  workflow's final step becomes `rclipctl get --decrypt`.
- Existing tests that assert auto-decrypt on plain `get` are updated to pass
  `--decrypt`; a new test asserts plain `get` returns ciphertext (no decrypt).

## Error handling

- `copy-encrypted` with no registered keys: `rclipctl put -E --fetch-keys`
  already errors ("requires recipients …"); the script surfaces that error to
  the user (tmux `display-message` on non-zero exit).
- `paste-*` when the topic is empty / missing: current `paste.sh` already
  no-ops on empty content; keep that.
- `register` popup: on `rclipctl register` failure, show the error in the popup
  and keep it open until the user dismisses it.
- Invalid `--mode`: scripts print usage to stderr and exit 2.

## Testing

- **Plugin repo:** add a minimal bash test harness (none exists today) with a
  fake `rclipctl` on PATH asserting:
  - `copy.sh --mode encrypted` invokes `put … -E --fetch-keys`;
  - `copy.sh --mode clear` (and no arg) invokes `put` without `-E`;
  - `copy.sh --mode default` honours `@rclip_encrypt` on/off;
  - `paste.sh --mode clear` calls `get` WITHOUT `--decrypt`;
  - `paste.sh --mode encrypted` calls `get --decrypt`.
- **Server repo:**
  - unittest for `rclipctl unregister` — creates a temp keypair, runs
    `unregister`, asserts the files are gone and exit 0; `unregister` with no
    keypair still exits 0.
  - `get --decrypt` decrypts encrypted content (moved from the old implicit
    behaviour); plain `get` on encrypted content returns the ciphertext.
- Full server suite stays green; get-related tests updated for the new default.

## Out of scope (server TODO, separate task)

- `key-ttl` auto-eviction of un-renewed keys.
- `register --renew` real semantics (refresh `registered_at`).
- `keys.delete` endpoint + real E2E `rclipctl unregister`.
- nvim plugin fixes (`publish`/`fetch` → `put`/`get`) — tracked separately.
