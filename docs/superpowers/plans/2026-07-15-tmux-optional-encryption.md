# Optional Encryption in the tmux-rclipboard Plugin — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the tmux plugin optional per-operation encryption (copy/paste in clear / encrypted / default modes) plus register/unregister, without changing default key bindings.

**Architecture:** Two repos. In the **server worktree** (`rclipboard/.claude/worktrees/fix-proxy-double-b64`) we change `rclipctl get` to stop auto-decrypting (opt-in `--decrypt`) and add an `unregister` stub + `register --renew` alias, updating docs/tests. In the **tmux plugin repo** (`/home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard`, a separate git checkout) we extend `copy.sh`/`paste.sh` with a `--mode` argument, add `register.sh`/`unregister.sh`, wire commands as `@rclip_*` tmux options (no binding changes), and add a bash test harness.

**Tech Stack:** Bash (rclipctl, plugin scripts), tmux ≥ 3.2 (`display-popup`), age CLI, Python unittest (server suite).

## Global Constraints

- Server worktree path: `/home/yvdev/PrivProjects/rclipboard-project/rclipboard/.claude/worktrees/fix-proxy-double-b64` — run all server commands here; branch `refactor-module-structure`.
- Plugin repo path: `/home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard` — a SEPARATE git repo. Commit plugin changes there, server changes in the worktree. Never mix the two in one commit.
- Server test runner: `PYTHONPATH=src:. /home/yvdev/PrivProjects/rclipboard-project/rclipboard/.venv/bin/python -m unittest <module> -v`
- Do NOT change existing `bind-key` lines in `rclipboard.tmux`. New functionality is exposed only as `@rclip_*_cmd` tmux options.
- `rclipctl get` MUST default to NO decryption after this plan; `--decrypt` opts in. This is a deliberate breaking change.
- Bash scripts: `set -euo pipefail`, match existing style in `copy.sh`/`paste.sh`.
- `age` and `age-keygen` are available in this environment (verified) — e2e decrypt tests may rely on them.

---

## Task 1: `rclipctl get` — opt-in decryption (`--decrypt`), default clear

**Files:**
- Modify: `scripts/bin/rclipctl` (server worktree) — getopt long-opts line (~296-298), option case block (~303-325), `call_get` (~511-580), usage text (~201).
- Test: `tests/test_rclipctl_get_decrypt.py` (server worktree, new)

**Interfaces:**
- Produces: `rclipctl get -t <topic>` returns the stored value WITHOUT decrypting (ciphertext passed through the normal encoding path). `rclipctl get -t <topic> --decrypt` performs `age --decrypt` as the old default did. A new shell var `decrypt=0` (set to `1` by `--decrypt`).
- Consumes: existing `call_get` machinery (`is_encrypted`, `val`, `venc`, `decode`, `encode`, `require_age`, `AGE_KEY_FILE`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_rclipctl_get_decrypt.py`:

```python
"""rclipctl get: decryption is opt-in via --decrypt (default = no decrypt)."""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT_DIR, free_port, post_json, running_server

ADMIN_TOKEN = "get-decrypt-token"


@unittest.skipUnless(shutil.which("age") and shutil.which("age-keygen"),
                     "age CLI required")
class RclipctlGetDecryptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.port = free_port()
        self.home = tempfile.TemporaryDirectory()
        self.cfg = Path(self.home.name) / ".config" / "rclipboard"
        self.cfg.mkdir(parents=True)
        # generate a keypair in the fake HOME
        subprocess.run(
            [str(ROOT_DIR / "scripts" / "bin" / "rclipctl"), "keygen"],
            env=self._env(), check=True, capture_output=True, text=True,
        )
        self.pub = (self.cfg / "age_key.pub").read_text().strip()
        self.ctx = running_server(
            port=self.port,
            extra_env={"RCLIPBOARD_ADMIN_TOKEN": ADMIN_TOKEN},
        )
        self.ctx.__enter__()
        # register our key so clip.get is allowed to return the ciphertext
        post_json(
            f"http://127.0.0.1:{self.port}/v1/keys.publish",
            {"public_key": self.pub, "label": "t"},
            extra_headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )

    def tearDown(self) -> None:
        self.ctx.__exit__(None, None, None)
        self.home.cleanup()

    def _env(self) -> dict:
        env = os.environ.copy()
        env["HOME"] = self.home.name
        env["RCLIPBOARD_ENDPOINT"] = f"127.0.0.1:{self.port}"
        return env

    def _put_encrypted(self, plaintext: str) -> str:
        # encrypt to our own pubkey via `age`, base64 the armored-less binary
        enc = subprocess.run(
            ["age", "-r", self.pub],
            input=plaintext.encode(), capture_output=True, check=True,
        ).stdout
        cipher_b64 = base64.b64encode(enc).decode()
        post_json(
            f"http://127.0.0.1:{self.port}/v1/clip.put",
            {"items": [{"topic": "c", "mime": "application/octet-stream",
                        "encoding": "base64", "value": cipher_b64,
                        "encrypted": True}], "meta": {}},
        )
        return cipher_b64

    def _get(self, *args: str) -> str:
        return subprocess.run(
            [str(ROOT_DIR / "scripts" / "bin" / "rclipctl"), "get", "-t", "c",
             *args],
            env=self._env(), capture_output=True, text=True, check=True,
        ).stdout

    def test_get_without_decrypt_returns_ciphertext(self):
        cipher_b64 = self._put_encrypted("s3cret")
        out = self._get()  # no --decrypt
        self.assertNotIn("s3cret", out)
        # the base64 ciphertext is returned as stored
        self.assertIn(cipher_b64.rstrip("="), out)

    def test_get_with_decrypt_returns_plaintext(self):
        self._put_encrypted("s3cret")
        out = self._get("--decrypt")
        self.assertEqual(out, "s3cret")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:. /home/yvdev/PrivProjects/rclipboard-project/rclipboard/.venv/bin/python -m unittest tests.test_rclipctl_get_decrypt -v`
Expected: `test_get_without_decrypt_returns_ciphertext` FAILS (current `get` auto-decrypts, so `s3cret` appears in output).

- [ ] **Step 3: Add the `--decrypt` flag to option parsing**

In `scripts/bin/rclipctl`, add `decrypt=0` next to the other option defaults (near line 178, with `json=0`):

```bash
json=0
decrypt=0
```

Add `decrypt` to the getopt long-opts list (the `-l "..."` string, ~line 296). Insert `decrypt,` after `json,`:

```bash
    -l "app:,clipboard,primary,secondary,encoded,encrypt,fetch-keys,json,decrypt,help,encoding:,\
endpoint:,host:,port:,transport:,uds:,topic:,config:,key:,key-file:,token:,label:" \
```

Add a case arm after the `--json)` arm (~line 314):

```bash
        --json)         json=1;                 shift ;;
        --decrypt)      decrypt=1;              shift ;;
```

- [ ] **Step 4: Make decryption conditional in `call_get`**

In `call_get`, change the decryption gate so it requires BOTH `is_encrypted=true` AND `decrypt=1`. Replace the line:

```bash
    if [ "$is_encrypted" = "true" ]; then
```

with:

```bash
    if [ "$is_encrypted" = "true" ] && [ "$decrypt" = "1" ]; then
```

(Everything inside that block is unchanged. When encrypted but `--decrypt` was
not passed, execution falls through to the normal encoding path below, which
returns the stored ciphertext value.)

- [ ] **Step 5: Update the usage text**

In `usage()`, change the `get` line (~line 201) from:

```bash
  get [options]            call clip.get
```

to:

```bash
  get [options]            call clip.get (ciphertext as-is; add --decrypt to decrypt)
```

And add a flag description near the other get-related flags (after the `--json` description line, ~229):

```bash
  --decrypt                                       decrypt an encrypted value (age)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `PYTHONPATH=src:. /home/yvdev/PrivProjects/rclipboard-project/rclipboard/.venv/bin/python -m unittest tests.test_rclipctl_get_decrypt -v`
Expected: both tests PASS.

- [ ] **Step 7: Commit (server worktree)**

```bash
cd /home/yvdev/PrivProjects/rclipboard-project/rclipboard/.claude/worktrees/fix-proxy-double-b64
git add scripts/bin/rclipctl tests/test_rclipctl_get_decrypt.py
git commit -m "feat(rclipctl): get no longer auto-decrypts; opt-in via --decrypt

Safer default: a plaintext dump never silently decrypts a secret. Encrypted
values are returned as stored unless --decrypt is passed. Breaking change for
get consumers; docs updated in a follow-up task."
```

---

## Task 2: Update user-facing docs for the `get --decrypt` change

**Files:**
- Modify: `docs/USAGE.md:101,152` (server worktree)
- Modify: `docs/CONFIGURE.md:231-234`
- Modify: `README.md:197-200,234-235`

**Interfaces:**
- Consumes: the `--decrypt` flag from Task 1. No code; documentation only.

- [ ] **Step 1: USAGE.md — fix the auto-decrypt claim**

Replace `docs/USAGE.md:101`:

```
When the item has `encrypted: true`, `rclipctl get` automatically decrypts it using `~/.config/rclipboard/age_key.txt`. No extra flag needed.
```

with:

```
When the item has `encrypted: true`, `rclipctl get` returns the ciphertext as stored. Pass `--decrypt` to decrypt it with `~/.config/rclipboard/age_key.txt` (requires the `age` CLI). Decryption is opt-in so a plain `get` never silently reveals a secret.
```

- [ ] **Step 2: CONFIGURE.md — fix the receive example**

Replace the `**Receive (auto-decrypt)**` heading and its command block (`docs/CONFIGURE.md:231-234`):

```
**Receive (auto-decrypt)**

```bash
rclipctl get
```
```

with:

```
**Receive (decrypt explicitly)**

```bash
rclipctl get --decrypt
```
```

- [ ] **Step 3: README.md — fix both receive examples**

Replace `README.md:197`+`:200` (the "Receive and auto-decrypt:" block):

```
Receive and auto-decrypt:

```bash
./scripts/bin/rclipctl get -c --endpoint 127.0.0.1:8989
```
```

with:

```
Receive and decrypt (opt-in):

```bash
./scripts/bin/rclipctl get -c --decrypt --endpoint 127.0.0.1:8989
```
```

Replace `README.md:234-235`:

```
# 5. Receive and auto-decrypt on any registered machine
rclipctl get
```

with:

```
# 5. Receive and decrypt on any registered machine
rclipctl get --decrypt
```

- [ ] **Step 4: Verify no stale "auto-decrypt" claims remain in user docs**

Run:
```bash
cd /home/yvdev/PrivProjects/rclipboard-project/rclipboard/.claude/worktrees/fix-proxy-double-b64
grep -nE "auto.?decrypt|automatically decrypts" README.md docs/USAGE.md docs/CONFIGURE.md
```
Expected: no matches (api-contract.md may still describe server behaviour — that is out of scope for this doc pass and untouched).

- [ ] **Step 5: Commit (server worktree)**

```bash
git add README.md docs/USAGE.md docs/CONFIGURE.md
git commit -m "docs: rclipctl get is opt-in decrypt (--decrypt), not auto"
```

---

## Task 3: `rclipctl unregister` stub + `register --renew` alias

**Files:**
- Modify: `scripts/bin/rclipctl` (server worktree) — dispatch case (~685-694), usage (~199-205), add `call_unregister`, extend `call_register`.
- Test: `tests/test_rclipctl_unregister.py` (server worktree, new)

**Interfaces:**
- Produces:
  - `rclipctl unregister` — removes local `age_key.txt`/`age_key.pub` under `$RCLIP_CONFIG_DIR` if present; prints that server-side removal is a TODO; ALWAYS exits 0.
  - `rclipctl register --renew` — accepted flag; behaves as a normal `register` (re-publish). Sets shell var `renew=1` (currently only affects a log line).
- Consumes: `RCLIP_CONFIG_DIR`, existing `call_register`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_rclipctl_unregister.py`:

```python
"""rclipctl unregister: local keypair removal stub (server-side is a TODO)."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT_DIR


class RclipctlUnregisterTests(unittest.TestCase):
    def _run(self, home: Path) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["HOME"] = str(home)
        return subprocess.run(
            [str(ROOT_DIR / "scripts" / "bin" / "rclipctl"), "unregister"],
            env=env, capture_output=True, text=True,
        )

    def test_unregister_removes_local_keypair_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg = home / ".config" / "rclipboard"
            cfg.mkdir(parents=True)
            (cfg / "age_key.txt").write_text("AGE-SECRET-KEY-1TEST\n")
            (cfg / "age_key.pub").write_text("age1testpub\n")

            proc = self._run(home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse((cfg / "age_key.txt").exists())
            self.assertFalse((cfg / "age_key.pub").exists())

    def test_unregister_without_keypair_still_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".config" / "rclipboard").mkdir(parents=True)
            proc = self._run(home)
            self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:. /home/yvdev/PrivProjects/rclipboard-project/rclipboard/.venv/bin/python -m unittest tests.test_rclipctl_unregister -v`
Expected: FAIL — `unregister` is an unknown subcommand (exit 2).

- [ ] **Step 3: Add `call_unregister` and `--renew` handling**

In `scripts/bin/rclipctl`, add a `renew=0` default near the other register vars
(search for `REGISTER_TOKEN=`; add below it):

```bash
renew=0
```

Add `renew` to the getopt long-opts list (the `-l "..."` string, ~296) after `label:`:

```bash
...token:,label:,renew" \
```

Add a case arm after the `--label)` arm (~313):

```bash
        --renew)        renew=1;                shift ;;
```

Add a `call_unregister` function next to `call_register` (after `call_keys_list`, ~594):

```bash
call_unregister() {
    # Stub: server-side key removal is not implemented (registry is in-memory;
    # keys also vanish on server restart). This removes the LOCAL keypair so a
    # machine can drop its identity. Always exits 0 so callers (e.g. the tmux
    # plugin) can invoke it unconditionally.
    local removed=0
    for f in "$RCLIP_CONFIG_DIR/age_key.txt" "$RCLIP_CONFIG_DIR/age_key.pub"; do
        if [ -e "$f" ]; then rm -f "$f"; removed=1; fi
    done
    if [ "$removed" = 1 ]; then
        echo "removed local keypair from $RCLIP_CONFIG_DIR" >&2
    else
        echo "no local keypair to remove in $RCLIP_CONFIG_DIR" >&2
    fi
    echo "note: server-side key removal is not yet implemented (TODO)" >&2
    exit 0
}
```

In `call_register`, add a renew note at the top of the function body (after the
`local pub_key` line is fine) — keep behaviour identical otherwise:

```bash
    [ "$renew" = 1 ] && echo "renewing registration (re-publishing key)" >&2
```

- [ ] **Step 4: Wire `unregister` into dispatch + usage**

In the dispatch `case "$mode"` (~686), add after the `register)` line:

```bash
    register)   call_register ;;
    unregister) call_unregister ;;
```

Update the top usage line (~6) and the subcommand list (~199) to include
`unregister`. Change line 6:

```bash
    echo "usage: $0 <put|get|exec|keygen|register|unregister|keys-list|status|topics|health> [options]" >&2
```

Add to the subcommands help block (after the `register` description, ~near the
`keys-list` line):

```bash
  unregister               remove local keypair (server-side removal is a TODO)
```

And a `--renew` flag description near the register flags:

```bash
  --renew                                         re-publish (renew) the registration
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=src:. /home/yvdev/PrivProjects/rclipboard-project/rclipboard/.venv/bin/python -m unittest tests.test_rclipctl_unregister -v`
Expected: both tests PASS.

- [ ] **Step 6: Sanity — `register --renew` parses**

Run:
```bash
cd /home/yvdev/PrivProjects/rclipboard-project/rclipboard/.claude/worktrees/fix-proxy-double-b64
HOME=$(mktemp -d) scripts/bin/rclipctl register --renew --token x --endpoint 127.0.0.1:1 2>&1 | head -3
```
Expected: it reaches the "no public key found" or connection error path (i.e. `--renew` is accepted, not an "unknown option"), printing the renew note.

- [ ] **Step 7: Commit (server worktree)**

```bash
git add scripts/bin/rclipctl tests/test_rclipctl_unregister.py
git commit -m "feat(rclipctl): add unregister stub (local keypair) and register --renew alias

Server-side key removal and real renew/TTL are server TODOs; these give the
tmux plugin a stable CLI surface to call today."
```

---

## Task 4: Server TODO note for key TTL / renew / keys.delete

**Files:**
- Modify: `CLAUDE.md` (server worktree) — encryption section (after the "Encrypted retention (TTL)" paragraph, ~line 330).

**Interfaces:**
- Consumes: nothing. Documentation of deferred server work.

- [ ] **Step 1: Add the TODO paragraph**

In `CLAUDE.md`, immediately after the "**Encrypted retention (TTL)**" paragraph, add:

```markdown
**Key registry TODO (not yet implemented)** — planned symmetry with message
retention: registered public keys that are not renewed within a `key-ttl` are
auto-evicted from the in-memory registry; renewal is `rclipctl register --renew`
(refreshes the key's `registered_at`); a `keys.delete` endpoint (admin-token
gated, HTTP + RPC) enables real end-to-end `rclipctl unregister`. Today
`rclipctl unregister` only drops the LOCAL keypair; server-side removal relies
on the registry being in-memory (cleared on restart).
```

- [ ] **Step 2: Commit (server worktree)**

```bash
git add CLAUDE.md
git commit -m "docs(CLAUDE): note key-ttl/renew/keys.delete as server TODOs"
```

---

## Task 5: tmux plugin — `copy.sh --mode`

**Files:**
- Modify: `/home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/scripts/copy.sh`
- Test: `/home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/tests/test_copy.sh` (new)
- Create: `/home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/tests/fake-rclipctl` (new; shared fake)

**Interfaces:**
- Produces: `copy.sh [--mode clear|encrypted|default]`. `clear`/absent → `rclipctl put -t $TOPIC --app $APP`; `encrypted` → adds `-E --fetch-keys`; `default` → reads tmux option `@rclip_encrypt` (`on`→encrypted, else clear).
- Consumes: `rclipctl` on PATH, `RCLIP_BIN`/`RCLIP_TOPIC`/`RCLIP_APP` env (existing), `tmux show-option -gqv @rclip_encrypt`.

- [ ] **Step 1: Write the shared fake rclipctl + failing test**

Create `tests/fake-rclipctl` (executable — records its argv to `$FAKE_LOG`):

```bash
#!/usr/bin/env bash
# Test double: append the full argv (one per line, NUL-safe enough for tests)
# to $FAKE_LOG and consume stdin so pipes don't break.
printf '%s\n' "$*" >> "$FAKE_LOG"
cat >/dev/null 2>&1 || true
# For `get`, emit canned content so paste tests can inspect it.
if [ "${1:-}" = "get" ]; then printf 'FAKE_CONTENT'; fi
exit 0
```

Create `tests/test_copy.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "$DIR/.." && pwd)"

fail=0
run_copy() {
    # $1 = mode arg string (may be empty); remaining env vars set by caller
    local tmp; tmp="$(mktemp)"
    FAKE_LOG="$tmp" PATH="$DIR:$PATH" RCLIP_BIN=fake-rclipctl \
        bash "$PLUGIN_DIR/scripts/copy.sh" $1 </dev/null || true
    cat "$tmp"; rm -f "$tmp"
}

assert_contains() { # haystack needle label
    if [[ "$1" != *"$2"* ]]; then echo "FAIL: $3 (got: $1)"; fail=1;
    else echo "ok: $3"; fi
}
assert_not_contains() {
    if [[ "$1" == *"$2"* ]]; then echo "FAIL: $3 (got: $1)"; fail=1;
    else echo "ok: $3"; fi
}

# The fake needs a topic; copy.sh sets defaults.
out="$(run_copy '--mode clear')"
assert_contains "$out" "put" "clear invokes put"
assert_not_contains "$out" "-E" "clear has no -E"

out="$(run_copy '--mode encrypted')"
assert_contains "$out" "-E" "encrypted adds -E"
assert_contains "$out" "--fetch-keys" "encrypted adds --fetch-keys"

out="$(run_copy '')"
assert_not_contains "$out" "-E" "no-arg defaults to clear"

exit $fail
```

Make both executable:
```bash
chmod +x /home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/tests/fake-rclipctl \
         /home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/tests/test_copy.sh
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/tests/test_copy.sh`
Expected: FAIL on "encrypted adds -E" (current `copy.sh` ignores `--mode`).

- [ ] **Step 3: Rewrite `copy.sh` with `--mode`**

Replace the whole file `tmux-rclipboard/scripts/copy.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

RCLIP_BIN=${RCLIP_BIN:-rclipctl}
RCLIP_TOPIC=${RCLIP_TOPIC:-c}
RCLIP_APP=${RCLIP_APP:-tmux}

mode=clear
while [ $# -gt 0 ]; do
    case "$1" in
        --mode) mode="${2:-clear}"; shift 2 ;;
        *) echo "usage: copy.sh [--mode clear|encrypted|default]" >&2; exit 2 ;;
    esac
done

if [ "$mode" = "default" ]; then
    enc=$(tmux show-option -gqv '@rclip_encrypt' 2>/dev/null || true)
    if [ "$enc" = "on" ]; then mode=encrypted; else mode=clear; fi
fi

case "$mode" in
    clear)     exec "$RCLIP_BIN" put -t "$RCLIP_TOPIC" --app "$RCLIP_APP" ;;
    encrypted) exec "$RCLIP_BIN" put -t "$RCLIP_TOPIC" --app "$RCLIP_APP" -E --fetch-keys ;;
    *) echo "copy.sh: invalid mode '$mode'" >&2; exit 2 ;;
esac
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/tests/test_copy.sh`
Expected: all `ok:` lines, exit 0.

- [ ] **Step 5: Commit (plugin repo)**

```bash
cd /home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard
git add scripts/copy.sh tests/fake-rclipctl tests/test_copy.sh
git commit -m "feat(copy): --mode clear|encrypted|default (default reads @rclip_encrypt)"
```

---

## Task 6: tmux plugin — `paste.sh --mode`

**Files:**
- Modify: `/home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/scripts/paste.sh`
- Test: `/home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/tests/test_paste.sh` (new)

**Interfaces:**
- Produces: `paste.sh [--mode clear|encrypted|default]`. `clear`/absent → `rclipctl get -t $TOPIC` (no `--decrypt`); `encrypted` → `rclipctl get -t $TOPIC --decrypt`; `default` → reads `@rclip_encrypt` (`on`→encrypted, else clear). Pastes non-empty content via `tmux load-buffer` + `paste-buffer -d` (existing behaviour preserved).
- Consumes: `rclipctl get` / `get --decrypt` from Task 1; the fake rclipctl from Task 5.

- [ ] **Step 1: Write the failing test**

Create `tests/test_paste.sh` (uses the same fake; stubs `tmux` so `load-buffer`/`paste-buffer` are no-ops but option reads work):

```bash
#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "$DIR/.." && pwd)"

# Stub tmux: record calls, return @rclip_encrypt from $RCLIP_ENCRYPT_OPT.
STUB_BIN="$(mktemp -d)"
cat > "$STUB_BIN/tmux" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "show-option" ]; then printf '%s' "${RCLIP_ENCRYPT_OPT:-}"; exit 0; fi
exit 0
EOF
chmod +x "$STUB_BIN/tmux"

fail=0
run_paste() {
    local tmp; tmp="$(mktemp)"
    FAKE_LOG="$tmp" PATH="$DIR:$STUB_BIN:$PATH" RCLIP_BIN=fake-rclipctl \
        RCLIP_ENCRYPT_OPT="${2:-}" \
        bash "$PLUGIN_DIR/scripts/paste.sh" $1 </dev/null || true
    cat "$tmp"; rm -f "$tmp"
}
assert_contains(){ [[ "$1" == *"$2"* ]] && echo "ok: $3" || { echo "FAIL: $3 (got: $1)"; fail=1; }; }
assert_not_contains(){ [[ "$1" != *"$2"* ]] && echo "ok: $3" || { echo "FAIL: $3 (got: $1)"; fail=1; }; }

out="$(run_paste '--mode clear')"
assert_contains "$out" "get" "clear invokes get"
assert_not_contains "$out" "--decrypt" "clear has no --decrypt"

out="$(run_paste '--mode encrypted')"
assert_contains "$out" "--decrypt" "encrypted adds --decrypt"

out="$(run_paste '--mode default' 'on')"
assert_contains "$out" "--decrypt" "default+on → decrypt"

out="$(run_paste '--mode default' 'off')"
assert_not_contains "$out" "--decrypt" "default+off → no decrypt"

rm -rf "$STUB_BIN"
exit $fail
```

Make executable:
```bash
chmod +x /home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/tests/test_paste.sh
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/tests/test_paste.sh`
Expected: FAIL on "encrypted adds --decrypt" (current `paste.sh` ignores `--mode`).

- [ ] **Step 3: Rewrite `paste.sh` with `--mode`**

Replace the whole file `tmux-rclipboard/scripts/paste.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

RCLIP_BIN=${RCLIP_BIN:-rclipctl}
RCLIP_TOPIC=${RCLIP_TOPIC:-c}

mode=clear
while [ $# -gt 0 ]; do
    case "$1" in
        --mode) mode="${2:-clear}"; shift 2 ;;
        *) echo "usage: paste.sh [--mode clear|encrypted|default]" >&2; exit 2 ;;
    esac
done

if [ "$mode" = "default" ]; then
    enc=$(tmux show-option -gqv '@rclip_encrypt' 2>/dev/null || true)
    if [ "$enc" = "on" ]; then mode=encrypted; else mode=clear; fi
fi

case "$mode" in
    clear)     content=$("$RCLIP_BIN" get -t "$RCLIP_TOPIC" 2>/dev/null || true) ;;
    encrypted) content=$("$RCLIP_BIN" get -t "$RCLIP_TOPIC" --decrypt 2>/dev/null || true) ;;
    *) echo "paste.sh: invalid mode '$mode'" >&2; exit 2 ;;
esac

if [ -n "$content" ]; then
    printf '%s' "$content" | tmux load-buffer -
    tmux paste-buffer -d
fi
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/tests/test_paste.sh`
Expected: all `ok:` lines, exit 0.

- [ ] **Step 5: Commit (plugin repo)**

```bash
cd /home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard
git add scripts/paste.sh tests/test_paste.sh
git commit -m "feat(paste): --mode clear|encrypted|default (encrypted → get --decrypt)"
```

---

## Task 7: tmux plugin — `register.sh` and `unregister.sh`

**Files:**
- Create: `/home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/scripts/register.sh`
- Create: `/home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/scripts/unregister.sh`
- Test: `/home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/tests/test_register.sh` (new)

**Interfaces:**
- Produces:
  - `register.sh` — opens `tmux display-popup -E` running an inner prompt that reads the admin token (silent) and optional label (default hostname), then runs `rclipctl register --token <tok> [--label <lbl>]`.
  - `unregister.sh` — opens a confirm `display-popup` then runs `rclipctl unregister`.
  - Both honour `RCLIP_BIN`. Both accept `RCLIP_POPUP=0` to run inline (no popup) — used by tests and headless callers.
- Consumes: `rclipctl register`/`unregister` (Task 3); `tmux display-popup`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_register.sh` (drives the inline `RCLIP_POPUP=0` path so no tmux popup is needed; feeds token/label on stdin):

```bash
#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "$DIR/.." && pwd)"

fail=0
# register.sh inline: token from stdin line 1, label from line 2 (blank=default)
tmp="$(mktemp)"
printf 'my-token\nlaptop\n' | \
    FAKE_LOG="$tmp" PATH="$DIR:$PATH" RCLIP_BIN=fake-rclipctl RCLIP_POPUP=0 \
    bash "$PLUGIN_DIR/scripts/register.sh" || true
out="$(cat "$tmp")"; rm -f "$tmp"
[[ "$out" == *"register"* ]] && echo "ok: calls register" || { echo "FAIL: calls register (got: $out)"; fail=1; }
[[ "$out" == *"--token my-token"* ]] && echo "ok: passes token" || { echo "FAIL: passes token (got: $out)"; fail=1; }
[[ "$out" == *"--label laptop"* ]] && echo "ok: passes label" || { echo "FAIL: passes label (got: $out)"; fail=1; }

# unregister.sh inline: confirm 'y' on stdin
tmp="$(mktemp)"
printf 'y\n' | \
    FAKE_LOG="$tmp" PATH="$DIR:$PATH" RCLIP_BIN=fake-rclipctl RCLIP_POPUP=0 \
    bash "$PLUGIN_DIR/scripts/unregister.sh" || true
out="$(cat "$tmp")"; rm -f "$tmp"
[[ "$out" == *"unregister"* ]] && echo "ok: calls unregister" || { echo "FAIL: calls unregister (got: $out)"; fail=1; }

exit $fail
```

Make executable:
```bash
chmod +x /home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/tests/test_register.sh
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/tests/test_register.sh`
Expected: FAIL — `register.sh`/`unregister.sh` do not exist.

- [ ] **Step 3: Write `register.sh`**

Create `tmux-rclipboard/scripts/register.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

RCLIP_BIN=${RCLIP_BIN:-rclipctl}
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/register.sh"

# When invoked as a key binding, re-launch inside a tmux popup so we can prompt
# interactively without the token touching shell history. RCLIP_POPUP=0 runs
# the prompt inline (used by tests / headless callers).
if [ "${RCLIP_POPUP:-1}" = "1" ] && [ -n "${TMUX:-}" ]; then
    exec tmux display-popup -E "RCLIP_POPUP=0 RCLIP_BIN='$RCLIP_BIN' bash '$SELF'"
fi

read -r -s -p "rclipboard admin token: " token; echo
read -r -p "label [$(hostname)]: " label
label="${label:-$(hostname)}"

if [ -z "$token" ]; then
    echo "aborted: no token given" >&2
    exit 1
fi

"$RCLIP_BIN" register --token "$token" --label "$label"
echo "press enter to close"; read -r _ || true
```

- [ ] **Step 4: Write `unregister.sh`**

Create `tmux-rclipboard/scripts/unregister.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

RCLIP_BIN=${RCLIP_BIN:-rclipctl}
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/unregister.sh"

if [ "${RCLIP_POPUP:-1}" = "1" ] && [ -n "${TMUX:-}" ]; then
    exec tmux display-popup -E "RCLIP_POPUP=0 RCLIP_BIN='$RCLIP_BIN' bash '$SELF'"
fi

read -r -p "remove local rclipboard identity? [y/N]: " ans
case "$ans" in
    y|Y) "$RCLIP_BIN" unregister ;;
    *)   echo "cancelled" >&2; exit 0 ;;
esac
echo "press enter to close"; read -r _ || true
```

Make both executable:
```bash
chmod +x /home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/scripts/register.sh \
         /home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/scripts/unregister.sh
```

- [ ] **Step 5: Run test to verify it passes**

Run: `/home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/tests/test_register.sh`
Expected: all `ok:` lines, exit 0.

- [ ] **Step 6: Commit (plugin repo)**

```bash
cd /home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard
git add scripts/register.sh scripts/unregister.sh tests/test_register.sh
git commit -m "feat: register.sh/unregister.sh via display-popup (RCLIP_POPUP=0 inline)"
```

---

## Task 8: tmux plugin — expose commands as `@rclip_*` options + README

**Files:**
- Modify: `/home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/rclipboard.tmux`
- Modify: `/home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/README.md`

**Interfaces:**
- Consumes: `copy.sh`/`paste.sh` `--mode` (Tasks 5-6), `register.sh`/`unregister.sh` (Task 7).
- Produces: tmux options `@rclip_copy_clear_cmd`, `@rclip_copy_encrypted_cmd`, `@rclip_copy_default_cmd`, `@rclip_paste_clear_cmd`, `@rclip_paste_encrypted_cmd`, `@rclip_paste_default_cmd`, `@rclip_register_cmd`, `@rclip_unregister_cmd`, and `@rclip_encrypt` (default `off`). No `bind-key` line is added or changed.

- [ ] **Step 1: Add option definitions to `rclipboard.tmux`**

In `rclipboard.tmux`, inside `main()`, after the existing `tmux set -gq
@rclip_health_cmd ...` line, add:

```bash
    # Encryption mode for *-default commands (user sets in .tmux.conf).
    tmux set -gq @rclip_encrypt off

    # Per-mode copy/paste commands (bind these yourself; defaults unchanged).
    local envp="RCLIP_BIN=${RCLIP_BIN} RCLIP_TOPIC=${RCLIP_TOPIC} RCLIP_APP=${RCLIP_APP}"
    tmux set -gq @rclip_copy_clear_cmd      "${envp} ${SCRIPT_DIR}/scripts/copy.sh --mode clear"
    tmux set -gq @rclip_copy_encrypted_cmd  "${envp} ${SCRIPT_DIR}/scripts/copy.sh --mode encrypted"
    tmux set -gq @rclip_copy_default_cmd    "${envp} ${SCRIPT_DIR}/scripts/copy.sh --mode default"
    tmux set -gq @rclip_paste_clear_cmd     "${envp} ${SCRIPT_DIR}/scripts/paste.sh --mode clear"
    tmux set -gq @rclip_paste_encrypted_cmd "${envp} ${SCRIPT_DIR}/scripts/paste.sh --mode encrypted"
    tmux set -gq @rclip_paste_default_cmd   "${envp} ${SCRIPT_DIR}/scripts/paste.sh --mode default"
    tmux set -gq @rclip_register_cmd        "RCLIP_BIN=${RCLIP_BIN} ${SCRIPT_DIR}/scripts/register.sh"
    tmux set -gq @rclip_unregister_cmd      "RCLIP_BIN=${RCLIP_BIN} ${SCRIPT_DIR}/scripts/unregister.sh"
```

Leave the existing `bind-key` block exactly as-is.

- [ ] **Step 2: Verify the plugin loads without error and sets options**

Run (requires a tmux server; starts a throwaway one):
```bash
cd /home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard
tmux -L rclip_test new-session -d "sleep 5" 2>/dev/null || tmux -L rclip_test new-session -d bash
tmux -L rclip_test run-shell "$(pwd)/rclipboard.tmux" 2>/dev/null || true
tmux -L rclip_test show-option -gqv @rclip_copy_encrypted_cmd
tmux -L rclip_test show-option -gqv @rclip_encrypt
tmux -L rclip_test kill-server 2>/dev/null || true
```
Expected: first prints a path ending in `copy.sh --mode encrypted`; second prints `off`.

- [ ] **Step 3: Document the new commands in README**

In `tmux-rclipboard/README.md`, add a section (place it after the existing
options/bindings documentation):

```markdown
## Encryption modes

The plugin can copy/paste in three modes. Default key bindings are unchanged
(they use the *clear* path); wire the encrypted variants yourself.

Set the mode used by the `*-default` commands in `~/.tmux.conf`:

```tmux
set -g @rclip_encrypt off   # or "on" — controls copy-default / paste-default
```

Commands are exposed as options you can bind:

| Option | Action |
|--------|--------|
| `@rclip_copy_clear_cmd` | copy selection as plaintext |
| `@rclip_copy_encrypted_cmd` | copy selection encrypted (`put -E --fetch-keys`) |
| `@rclip_copy_default_cmd` | copy per `@rclip_encrypt` |
| `@rclip_paste_clear_cmd` | paste stored value as-is (no decryption) |
| `@rclip_paste_encrypted_cmd` | paste and decrypt (`get --decrypt`) |
| `@rclip_paste_default_cmd` | paste per `@rclip_encrypt` |
| `@rclip_register_cmd` | register this host's key (popup prompt for admin token) |
| `@rclip_unregister_cmd` | remove this host's local key |

Example bindings (add to `~/.tmux.conf`):

```tmux
bind-key C-e run-shell "#{@rclip_copy_encrypted_cmd}"
bind-key C-d run-shell "#{@rclip_paste_encrypted_cmd}"
bind-key R   run-shell "#{@rclip_register_cmd}"
```

Encryption requires a keypair and a registered public key — see the main
rclipboard docs (`rclipctl keygen`, then `@rclip_register_cmd`).
```

- [ ] **Step 4: Commit (plugin repo)**

```bash
cd /home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard
git add rclipboard.tmux README.md
git commit -m "feat: expose per-mode copy/paste + register commands as @rclip_* options"
```

---

## Task 9: Full regression + plugin test runner

**Files:**
- Create: `/home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/tests/run.sh` (new — runs all plugin tests)

**Interfaces:**
- Consumes: all plugin tests (Tasks 5-7).

- [ ] **Step 1: Write the plugin test runner**

Create `tmux-rclipboard/tests/run.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
rc=0
for t in "$DIR"/test_*.sh; do
    echo "== $(basename "$t") =="
    bash "$t" || rc=1
done
exit $rc
```
```bash
chmod +x /home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/tests/run.sh
```

- [ ] **Step 2: Run all plugin tests**

Run: `/home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard/tests/run.sh`
Expected: every test file prints `ok:` lines and the runner exits 0.

- [ ] **Step 3: Run the full server suite**

Run:
```bash
cd /home/yvdev/PrivProjects/rclipboard-project/rclipboard/.claude/worktrees/fix-proxy-double-b64
PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests 2>&1 | tail -3
```
Expected: `OK`, test count ≥ previous (186+ with the two new rclipctl test modules).

- [ ] **Step 4: Commit (plugin repo)**

```bash
cd /home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard
git add tests/run.sh
git commit -m "test: add plugin test runner (run.sh)"
```

- [ ] **Step 5: Push both repos**

```bash
cd /home/yvdev/PrivProjects/rclipboard-project/rclipboard/.claude/worktrees/fix-proxy-double-b64
git push origin refactor-module-structure
# Plugin repo: push only if it has an 'origin' remote; otherwise report the local commits.
cd /home/yvdev/PrivProjects/rclipboard-project/tmux-rclipboard
if git remote get-url origin >/dev/null 2>&1; then
    git push origin "$(git branch --show-current)"
else
    echo "tmux-rclipboard has no 'origin' remote — commits are local only"
    git log --oneline -6
fi
```
Expected: server branch pushed; plugin either pushed or reported local.

---

## Notes for the implementer

- Two separate git repos. Server commits go in the worktree; plugin commits in `tmux-rclipboard`. The plan's commit steps `cd` to the right repo each time — follow them exactly.
- `age`/`age-keygen` are present; Task 1's e2e test relies on them (skips if absent).
- The plugin has no pre-existing test harness — Tasks 5-9 build a tiny bash one with a fake `rclipctl`. Do not pull in bats or other deps.
- Do NOT touch the nvim plugin (`publish`/`fetch` bug) — that is a separate task.
