# PLAN: `slack-post` — sending Slack messages by reusing slackdump credentials

> **Status: IMPLEMENTED (Python).** The tool is built and verified end-to-end
> against the `nomni` workspace (see "Verification results" at the bottom).
> See `README.md` for usage.

## Goal

Build a small CLI in this workspace (`slack-post`) that can **send a Slack
message** without creating a new Slack app / bot token, by **reusing the
credentials slackdump already stores on this machine** for the `nomni`
workspace.

## Findings from investigation

### 1. `slackdump` itself cannot send messages

`slackdump` 4.4.2 is a read-only archiving tool. Its full command set
(`workspace`, `archive`, `export`, `dump`, `search`, `resume`, `convert`,
`list`, `emoji`, `tools`, `config`, `format`, `view`, `mcp`, `wiz`,
`version`) contains no write/post/send operation. The `chat.postMessage`
string found in the binary comes only from the bundled `slack-go` client
library, which slackdump never calls for posting.

### 2. slackdump already holds a working Slack login

A workspace named `nomni` is authenticated and stored on disk:

- Cache dir: `~/Library/Caches/slackdump/`
- Credential file: `nomni.bin`
- Selected workspace pointer: `workspace.txt` contains `nomni`

### 3. The `nomni.bin` format (reverse-engineered and verified)

The file is an **encrypted JSON blob** produced by two of the author's
libraries (`rusq/encio` + `rusq/secure`):

```
file layout:  [ 16-byte IV ][ AES-256-CFB ciphertext ]
plaintext:    JSON  { "Token": "xoxc-...", "Cookie": [ ...http.Cookie... ] }
```

Decryption chain:

1. **Machine ID** — on macOS, `machineid.ProtectedID(appID)` uses the
   hardware UUID from `ioreg -rd1 -c IOPlatformExpertDevice`
   (`IOPlatformUUID`).
2. **Protected ID** — `hex(HMAC-SHA256(key=UUID, msg=appID))` where
   `appID = "76d19bf515c59483e8923fcad9f1b65025d445e71801688b7edfb9cc2e64497f"`.
3. **Key** — `PBKDF2-HMAC-SHA512(password=protectedID, salt=<fixed 256-byte
   package salt>, iterations=4096, dkLen=32)`.
4. **Decrypt** — AES-256-CFB with the 16-byte IV read from the file header.

**This was tested successfully.** The decrypted credentials authenticate:

```json
{ "ok": true, "url": "https://nomni.enterprise.slack.com/",
  "team": "Nomni", "user": "bagus", "team_id": "E05341YKEMS",
  "user_id": "U0351TPJH5Z", "enterprise_id": "E05341YKEMS",
  "is_enterprise_install": true }
```

> Note: `nomni.bin` contains a **client token (`xoxc-`) plus 6 session
> cookies**. Posting requires sending both the token and the `Cookie`
> header, and a browser-like `User-Agent`.

### 4. Environment constraints

- No Go toolchain installed (`go: command not found`), so we cannot simply
  `import github.com/rusq/slackdump/v4/...` and compile.
- Python 3 with `cryptography` is available (used for the proof above).
- The credentials are encrypted with the **machine-specific** key, so they
  only work on this machine as-is.

## Proposed approach

Implement `slack-post` as a **Python 3 CLI** that:

1. Decrypts the slackdump credential file for a chosen workspace using the
   verified chain above.
2. Calls the Slack Web API `chat.postMessage` with the `xoxc-` token **and**
   the session cookies.
3. Exposes a friendly CLI: `slack-post send --channel <id|name> --text "..."`.

### Why Python (not Go)

Reusing slackdump's Go packages would be the "purest" approach, but it needs
a Go toolchain (not installed) and would pull in the whole module tree. The
Python implementation is ~150 lines, has one dependency
(`cryptography`), and the crypto chain is already proven.

### Alternative (documented, not default): Go wrapper

If a Go toolchain is later available, the cleaner long-term option is a
small Go program importing `github.com/rusq/encio` + `auth.Load`, which
avoids re-implementing the crypto. This is listed as **Phase 4 (optional)**.

## Implementation plan

### Phase 1 — Credential reader (`slackpost/creds.py`)

- `find_cache_dir()` — resolve `~/Library/Caches/slackdump` (allow override
  via `--cache-dir` / `SLACKDUMP_CACHE_DIR`).
- `current_workspace()` — read `workspace.txt`, fall back to `--workspace`.
- `machine_id()` — on macOS read `IOPlatformUUID` via `ioreg`; on
  Linux/macOS fall back to `hashlib`-based `machineid` equivalents
  (Linux: `/etc/machine-id`; document that other OSes are untested).
- `derive_key()` — HMAC-SHA256(appID, key=machineID) → hex → PBKDF2.
- `load_credentials()` — decrypt `AES-256-CFB`, parse JSON, return
  `(token, cookies)`.
- Support `--no-encryption`/override gracefully with a clear error message.

### Phase 2 — Slack client (`slackpost/client.py`)

- `SlackClient(token, cookies)` using `urllib` (no extra dependency) or
  `requests` if preferred.
- Common headers: `Cookie`, browser-like `User-Agent`, form-encoded body.
- Methods:
  - `auth_test()` — sanity check.
  - `post_message(channel, text, thread_ts=None, blocks=None)` — POST to
    `chat.postMessage`; **always check the JSON `ok` field** (Slack returns
    HTTP 200 on errors).
  - `resolve_channel(name)` — optional convenience: look up channel ID by
    name via `conversations.list`.
- Typed errors for `not_authed`, `channel_not_found`, `invalid_auth`, etc.

### Phase 3 — CLI (`slackpost/__main__.py`)

- `slack-post send --channel C123 --text "hi" [--thread-ts ...]`
- `slack-post send --channel "#general" --text ...` (resolve name)
- `slack-post auth` — print workspace/user from `auth.test`.
- `--workspace`, `--cache-dir`, `--json`, `--dry-run` flags.
- Exit codes: `0` success, `1` API error, `2` credential/decryption error.

### Phase 4 (optional) — Go wrapper

- If Go is installed later: a `cmd/slack-post` Go program using
  `encio.Open()` + `auth.Load()` + `slack-go` `chat.postMessage`.

### Phase 5 — Docs & safety

- `README.md` describing usage and the credential-reuse model.
- Never log or print the token/cookies. Redact in `--dry-run`.
- Add `.gitignore` for any local `.env`/credential artifacts.
- Clearly document that this relies on **client tokens + cookies**, which
  Slack may rotate/expire; on `invalid_auth` the fix is to re-run
  `slackdump workspace new nomni`.

## Risks & caveats

1. **Fragility** — reimplemented crypto depends on slackdump's internal file
   format and the fixed `appID`/salt; a slackdump upgrade could break it.
   Mitigation: fail loudly with a clear message; keep the Go path documented.
2. **Token lifetime** — `xoxc-` client tokens and `d=`/`d-s=` cookies expire.
   Re-auth via slackdump when needed.
3. **Machine binding** — encrypted creds only work on this machine.
4. **Terms/scope** — this posts as the logged-in *user* (`bagus`), not a bot.
   Ensure that is acceptable for the intended use.
5. **No secrets in repo** — PLAN.md intentionally contains no token/cookie
   values; only the public `appID` constant and salt (both are public
   source-code constants).

## Verification steps

1. `slack-post auth` → prints `Nomni / bagus`.
2. `slack-post send --dry-run --channel C... --text hi` → shows request,
   redacts secrets.
3. `slack-post send --channel <test channel> --text "hello"` → message
   appears in Slack; API returns `ok:true`.

## Verification results (actual)

- `slack-post auth` → `Nomni (E05341YKEMS)` / `bagus (U0351TPJH5Z)` ✅
- `slack-post send -c U0351TPJH5Z -t ...` → `ok:true`, message delivered to
  self-DM `D034V8E3DPG` ✅
- threaded reply via `--thread-ts` → `ok:true` ✅
- `--dry-run` → prints request, sends nothing, redacts secrets ✅
- 6 unit tests pass (`python3 -m unittest discover -s tests`)
- `conversations.list` is blocked here (`enterprise_is_restricted`), so
  channel-name resolution degrades gracefully and lets `chat.postMessage`
  resolve the name server-side.

## Implemented files

```
slackpost/creds.py      credential discovery + decryption
slackpost/client.py     minimal Slack Web API client
slackpost/mentions.py   @name -> <@USERID> resolution
slackpost/__main__.py   CLI (auth | send | channels)
tests/                  unittest suites for creds + mentions
pyproject.toml          packaging + `slack-post` entry point
README.md               usage + how decryption works
.gitignore              ignores creds / venv / build
PLAN.md                 this document
```

## Mentions (added)

Slack does **not** treat a literal `@Abhijeet` as a mention - it renders as
plain text and notifies nobody. Only `<@USERID>` is a real mention.

Verified by inspecting the returned message blocks:

- `"@Abhijeet"`  -> `element type: text` (no notification)
- `"<@U6R7MTYBG>"` -> `element type: user, user_id: U6R7MTYBG` (notifies)

So `slack-post` resolves `@name` -> `<@USERID>` automatically via `users.list`
(which *is* available on this grid, unlike `conversations.list`). Matching is
case-insensitive across username / display name / real name / first name.
Disable with `--no-mentions`.

## Cross-platform notes (Linux)

`creds.py` was audited against the upstream sources (`encio`, `machineid`,
`secure`). Linux is **not** a one-liner, and the first implementation had two
bugs, now fixed:

1. **Path order.** `machineid.machineID()` on Linux reads
   `/var/lib/dbus/machine-id` **first**, then `/etc/machine-id`. The original
   code had these reversed.
2. **Container branch.** `encio_id_linux.go` overrides the machine-id function
   entirely when it detects a container (via `/proc/1/cgroup` containing
   `lxc`, `docker`, or `0::/`). In that case the protected id is
   `sha256(hostname || APP_ID)` - **not** an HMAC of the machine id.

Both behaviours are now reproduced faithfully, with unit tests
(`tests/test_linux.py`). The `0::/` marker also matches many non-container
cgroup-v2 hosts; this quirk is kept deliberately because slackdump used the
same check when writing the file, so matching it (bug included) is what makes
decryption succeed.

Windows is not implemented and raises a clear `CredentialError`.

> Credentials are machine-bound: a `*.bin` from macOS will not decrypt on
> Linux. Run `slackdump workspace new <name>` on the target machine.

## Decision taken

User chose **Python** implementation.

## Open questions for the user

- Preferred language: **Python** (fast, matches current toolchain) vs **Go**
  (cleaner reuse, needs install)?
- Target channel for a first test message?
- Should channel **name** resolution (`#general` → ID) be included in v1?
