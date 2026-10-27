"""Command line interface for slack-post."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__, mentions
from .client import SlackClient, SlackError
from .creds import CredentialError, load_credentials

EXIT_OK = 0
EXIT_API_ERROR = 1
EXIT_CRED_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slack-post",
        description=(
            "Send Slack messages by reusing credentials that slackdump has "
            "already stored on this machine."
        ),
    )
    parser.add_argument("--version", action="version", version=f"slack-post {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--workspace",
        help="slackdump workspace name (default: the currently selected one)",
    )
    common.add_argument(
        "--cache-dir",
        type=Path,
        help="override slackdump's credential cache directory",
    )
    common.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="print machine-readable JSON output",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("auth", parents=[common], help="show the authenticated workspace/user")

    send = sub.add_parser("send", parents=[common], help="send a message")
    send.add_argument("--channel", "-c", required=True, help="channel id or #name")
    send.add_argument("--text", "-t", help="message text (or pass --text-file/-)")
    send.add_argument("--text-file", help="read message text from a file ('-' for stdin)")
    send.add_argument("--thread-ts", help="reply in a thread")
    send.add_argument(
        "--reply-broadcast",
        action="store_true",
        help="also send thread reply to the channel",
    )
    send.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be sent without calling the API",
    )
    send.add_argument(
        "--no-mentions",
        action="store_true",
        help="do not convert @name to <@USERID> mentions",
    )
    send.add_argument(
        "--file",
        "-f",
        action="append",
        default=[],
        metavar="PATH",
        help="attach a file (repeatable). Text in --text becomes the comment",
    )
    send.add_argument(
        "--file-title",
        help="title for the attached file (default: filename)",
    )

    sub.add_parser("channels", parents=[common], help="list channels visible to the user")

    return parser


def _read_text(args: argparse.Namespace, required: bool = True) -> str:
    if args.text_file:
        if args.text_file == "-":
            return sys.stdin.read()
        return Path(args.text_file).read_text(encoding="utf-8")
    if args.text is None:
        if required:
            raise SystemExit("error: one of --text or --text-file is required")
        return ""
    return args.text


def _client(args: argparse.Namespace) -> SlackClient:
    creds = load_credentials(workspace=args.workspace, cache_dir=args.cache_dir)
    return SlackClient(creds)


def cmd_auth(args: argparse.Namespace) -> int:
    info = _client(args).auth_test()
    if args.as_json:
        print(
            json.dumps(
                {
                    "url": info.url,
                    "team": info.team,
                    "user": info.user,
                    "team_id": info.team_id,
                    "user_id": info.user_id,
                    "enterprise_id": info.enterprise_id,
                },
                indent=2,
            )
        )
    else:
        print(f"Workspace : {info.team} ({info.team_id})")
        print(f"User      : {info.user} ({info.user_id})")
        print(f"URL       : {info.url}")
    return EXIT_OK


def cmd_channels(args: argparse.Namespace) -> int:
    client = _client(args)
    try:
        channels = client.list_channels()
    except SlackError as exc:
        if exc.error in {"enterprise_is_restricted", "missing_scope"}:
            print(
                f"channel listing is not available for this token ({exc.error}); "
                "you can still send by channel name or id",
                file=sys.stderr,
            )
            return EXIT_API_ERROR
        raise
    channels.sort(key=lambda c: c.get("name", ""))
    if args.as_json:
        print(json.dumps(channels, indent=2))
    else:
        for channel in channels:
            member = "member" if channel.get("is_member") else "      "
            print(f"{channel['id']}  {member}  #{channel.get('name', '?')}")
    return EXIT_OK


def _resolve_mentions(client: SlackClient, text: str) -> str:
    """Convert ``@name`` tokens into Slack ``<@USERID>`` mentions."""
    try:
        users = client.list_users()
    except SlackError as exc:
        print(
            f"warning: could not look up users ({exc.error}); "
            "mention conversion skipped",
            file=sys.stderr,
        )
        return text

    directory = mentions.build_directory(users)
    resolved = mentions.resolve_text(text, directory)
    leftover = mentions.unresolved(resolved)
    if leftover:
        print(
            "warning: could not resolve mention(s): " + ", ".join(leftover),
            file=sys.stderr,
        )
    return resolved


def cmd_send(args: argparse.Namespace) -> int:
    files = list(args.file or [])
    text = _read_text(args, required=not files)
    client = _client(args)

    if not args.no_mentions:
        text = _resolve_mentions(client, text)

    target = args.channel.lstrip("#")
    # A bare user id (U...) means "DM that user"; open the IM channel first.
    if target[:1] == "U" and target.isalnum() and target.isupper() and len(target) >= 9:
        channel = client.open_dm(target)
    else:
        channel = client.resolve_channel(args.channel)

    if files and args.reply_broadcast:
        print(
            "warning: --reply-broadcast is ignored for file uploads "
            "(Slack does not support broadcasting file replies)",
            file=sys.stderr,
        )

    if args.dry_run:
        preview = {
            "channel": channel,
            "channel_arg": args.channel,
            "text": text,
            "files": files,
            "thread_ts": args.thread_ts,
            "reply_broadcast": args.reply_broadcast,
        }
        print(json.dumps(preview, indent=2))
        print("dry run: nothing sent", file=sys.stderr)
        return EXIT_OK

    if not files:
        result = client.post_message(
            channel,
            text,
            thread_ts=args.thread_ts,
            reply_broadcast=args.reply_broadcast,
        )
        if args.as_json:
            print(json.dumps(result, indent=2))
        else:
            print(f"sent to {channel} at ts={result.get('ts')}")
        return EXIT_OK

    # File upload path.
    results = []
    for index, file_path in enumerate(files):
        # Attach the comment text to the first file only.
        comment = text if index == 0 else None
        results.append(
            client.upload_file(
                file_path,
                channel=channel,
                thread_ts=args.thread_ts,
                initial_comment=comment,
                title=args.file_title,
            )
        )

    if args.as_json:
        print(json.dumps(results, indent=2))
    else:
        for res in results:
            uploaded = (res.get("files") or [{}])[0]
            print(f"uploaded {uploaded.get('id', '?')} to {channel}")
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    handlers = {
        "auth": cmd_auth,
        "channels": cmd_channels,
        "send": cmd_send,
    }
    handler = handlers[args.command]

    try:
        return handler(args)
    except CredentialError as exc:
        print(f"credential error: {exc}", file=sys.stderr)
        return EXIT_CRED_ERROR
    except SlackError as exc:
        print(str(exc), file=sys.stderr)
        if exc.error in {"invalid_auth", "not_authed", "token_revoked", "account_inactive"}:
            print(
                "hint: the stored slackdump session has expired. "
                "Re-authenticate with: slackdump workspace new <workspace>",
                file=sys.stderr,
            )
        return EXIT_API_ERROR
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
