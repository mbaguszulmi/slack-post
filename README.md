# slack-post

Send Slack messages by **reusing the credentials `slackdump` already stores**
on your machine - no new Slack app or bot token required.

> This posts as the **logged-in user**, not as a bot.

## Why

`slackdump` is a read-only archiving tool: it has no command to send messages.
But it *does* keep a working Slack login on disk. `slack-post` decrypts that
stored login and calls Slack's `chat.postMessage` directly.

## Requirements

- Python 3.8+
- `cryptography` (`pip install cryptography`)
- A workspace already authenticated with `slackdump`
  (`slackdump workspace list` should show it)

## Install

```bash
pip install -e .          # installs the `slack-post` command
# or run without installing:
python3 -m slackpost ...
```

## Usage

```bash
# check which workspace/user the credentials belong to
slack-post auth

# send to a channel by name or id
slack-post send --channel '#general' --text 'Hello from slack-post'
slack-post send -c C0123456789 -t 'Hello'

# reply in a thread
slack-post send -c C0123456789 -t 'thread reply' --thread-ts 1792657894.285569

# DM a user by id (opens the IM first)
slack-post send -c U0351TPJH5Z -t 'hi'

# read message body from stdin / file
slack-post send -c C0123456789 --text-file -

# mentions: @name is auto-converted to a real Slack mention
slack-post send -c '#general' -t 'hey @Abhijeet please review'

# preview without sending
slack-post send -c '#general' -t 'test' --dry-run
```

### File attachments

Attach one or more files (images, video, text, PDFs - anything) with
`--file`/`-f` (repeatable). The `--text` becomes the message that introduces
the file:

```bash
# single file
slack-post send -c '#general' -t 'here is the report' -f report.pdf

# multiple files (text is attached to the first)
slack-post send -c '#general' -t 'two files' -f chart.png -f data.csv

# attach inside a thread
slack-post send -c '#general' -t 'in thread' -f log.txt --thread-ts 1792...

# custom title
slack-post send -c '#general' -f photo.jpg --file-title 'Team photo'
```

The text/comment is optional when attaching a file. Uses Slack's current
upload flow (`files.getUploadURLExternal` -> POST bytes ->
`files.completeUploadExternal`), since `files.upload` is deprecated.

Note: `--reply-broadcast` is ignored for file uploads - Slack has no
"also send file reply to channel" option.

### Mentions

Write `@Name` normally and `slack-post` resolves it to Slack's `<@USERID>`
syntax before sending:

```bash
slack-post send -c '#general' -t 'ping @Abhijeet and @bagus'
# sent as: ping <@U6R7MTYBG> and <@U0351TPJH5Z>
```

- Matching is **case-insensitive** and accepts username, display name, real
  name, or just the first name.
- A **literal** `@Abhijeet` is *not* a mention in Slack - it is plain text and
  notifies nobody. Only `<@USERID>` notifies, which is why the conversion
  matters.
- Unknown names are left as-is with a warning on stderr.
- Use `--no-mentions` to send text verbatim.
- You can always write raw `<@U123>` yourself; it is left untouched.

Useful flags: `--workspace NAME`, `--cache-dir DIR`, `--json`.

Exit codes: `0` ok, `1` Slack API error, `2` credential/decryption error.

## How the credentials are decrypted

`slackdump` stores each workspace as `~/Library/Caches/slackdump/<name>.bin`:

```
[ 16-byte IV ][ AES-256-CFB ciphertext ]  ->  {"Token":"xoxc-...","Cookie":[...]}
```

The key is derived from the machine id:

1. machine id (platform specific, see below)
2. `hex(HMAC-SHA256(key=machine_id, msg=APP_ID))`
3. `PBKDF2-HMAC-SHA512(protected, fixed_salt, 4096, 32)`
4. AES-256-CFB decrypt

See `slackpost/creds.py` for the implementation.

### Platform support

| OS | Machine id source | Status |
|----|-------------------|--------|
| macOS | `ioreg` -> `IOPlatformUUID` | verified |
| Linux (host) | `/var/lib/dbus/machine-id`, then `/etc/machine-id` | implemented, mirrors upstream |
| Linux (container) | `sha256(hostname + APP_ID)` | implemented, mirrors upstream quirk |
| Windows | not implemented | raises a clear error |

Two Linux behaviours are faithfully reproduced from upstream, because the
credential file was written *by* upstream:

- **Path order** is `/var/lib/dbus/machine-id` first, then `/etc/machine-id`
  (the `machineid` library checks dbus first - the opposite of what you might
  expect).
- **Inside a container** (detected via `/proc/1/cgroup`), `encio` does *not*
  use the machine id at all. It hashes the **hostname** instead:
  `sha256(hostname || APP_ID)`. If that cgroup heuristic misfires, the derived
  key will differ and decryption fails with a clear error.

> **Important:** credentials are machine-bound. A `*.bin` copied from macOS
> will **not** decrypt on Linux. You must run
> `slackdump workspace new <name>` on the Linux machine itself, then
> `slack-post` will read that machine's own credential file.

Set `MACHINE_ID_OVERRIDE=<value>` to force a specific machine id if your
environment's id cannot be read (or to reproduce another machine's key).

## Caveats

- **Machine-bound**: the encrypted file only decrypts on this machine.
- **Expires**: `xoxc-` client tokens and cookies eventually expire. On
  `invalid_auth`, re-run `slackdump workspace new <workspace>`.
- **Fragile**: a future `slackdump` release could change the file format; the
  tool fails loudly if so.
- **Enterprise Grid**: some endpoints (`conversations.list`) may be restricted
  (`enterprise_is_restricted`). Sending by channel name/id still works; the
  `channels` command may not.
- **Scope**: posts as the logged-in user. Use responsibly and only where you
  have permission.

## Layout

```
slackpost/creds.py    credential discovery + decryption
slackpost/client.py   minimal Slack Web API client
slackpost/__main__.py CLI
PLAN.md               design notes / reverse-engineering findings
```
