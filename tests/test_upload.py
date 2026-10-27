import json
import unittest
from unittest import mock

from slackpost.client import SlackClient, SlackError
from slackpost.creds import Cookie, Credentials


def make_client():
    return SlackClient(Credentials(token="xoxc-1-2-3-abc", cookies=[Cookie("d", "x")]))


class UploadFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = None

    def _write_tmp(self, data=b"hello"):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.NamedTemporaryFile(suffix=".txt", delete=False)
        self.tmp.write(data)
        self.tmp.close()
        return Path(self.tmp.name)

    def test_full_flow(self):
        path = self._write_tmp(b"file-body")
        client = make_client()
        calls = []

        def fake_call(method, params=None):
            calls.append(method)
            if method == "files.getUploadURLExternal":
                self.assertEqual(params["filename"], path.name)
                self.assertEqual(params["length"], len(b"file-body"))
                return {"ok": True, "upload_url": "https://files.example/u", "file_id": "F1"}
            if method == "files.completeUploadExternal":
                files = json.loads(params["files"])
                self.assertEqual(files[0]["id"], "F1")
                self.assertEqual(params["channel_id"], "C1")
                self.assertEqual(params["initial_comment"], "hi")
                return {"ok": True, "files": [{"id": "F1"}]}
            raise AssertionError(f"unexpected method {method}")

        fake_resp = mock.MagicMock()
        fake_resp.status = 200
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = lambda *a: None

        with mock.patch.object(client, "call", side_effect=fake_call), \
             mock.patch("urllib.request.urlopen", return_value=fake_resp):
            result = client.upload_file(path, channel="C1", initial_comment="hi")

        self.assertEqual(calls, ["files.getUploadURLExternal", "files.completeUploadExternal"])
        self.assertEqual(result["files"][0]["id"], "F1")

    def test_missing_file_raises(self):
        client = make_client()
        with self.assertRaises(SlackError) as ctx:
            client.upload_file("/nonexistent/nope.txt", channel="C1")
        self.assertEqual(ctx.exception.error, "file_not_found")

    def test_upload_http_error(self):
        import urllib.error
        path = self._write_tmp()
        client = make_client()

        with mock.patch.object(
            client,
            "call",
            return_value={"ok": True, "upload_url": "https://x/u", "file_id": "F1"},
        ), mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("https://x/u", 500, "boom", {}, None),
        ):
            with self.assertRaises(SlackError) as ctx:
                client.upload_file(path, channel="C1")
            self.assertIn("upload_http_500", ctx.exception.error)

    def test_thread_ts_forwarded(self):
        path = self._write_tmp()
        client = make_client()
        seen = {}

        def fake_call(method, params=None):
            if method == "files.getUploadURLExternal":
                return {"ok": True, "upload_url": "https://x/u", "file_id": "F9"}
            seen.update(params)
            return {"ok": True, "files": [{"id": "F9"}]}

        fake_resp = mock.MagicMock()
        fake_resp.status = 200
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = lambda *a: None

        with mock.patch.object(client, "call", side_effect=fake_call), \
             mock.patch("urllib.request.urlopen", return_value=fake_resp):
            client.upload_file(path, channel="C1", thread_ts="123.456")

        self.assertEqual(seen["thread_ts"], "123.456")


if __name__ == "__main__":
    unittest.main()
