import hashlib
import hmac
import unittest
from unittest import mock

from slackpost import creds


class LinuxRawIdTests(unittest.TestCase):
    def test_prefers_dbus_path_order(self):
        # machineid tries /var/lib/dbus/machine-id FIRST, then /etc/machine-id.
        files = {
            "/var/lib/dbus/machine-id": "dbus-value\n",
            "/etc/machine-id": "etc-value\n",
        }

        def fake_read(path_self, *args, **kwargs):
            if str(path_self) in files:
                return files[str(path_self)]
            raise OSError("nope")

        with mock.patch.object(creds.Path, "read_text", fake_read):
            self.assertEqual(creds._raw_machine_id_linux(), "dbus-value")

    def test_falls_back_to_etc(self):
        def fake_read(path_self, *args, **kwargs):
            if str(path_self) == "/etc/machine-id":
                return "etc-value\n"
            raise OSError("nope")

        with mock.patch.object(creds.Path, "read_text", fake_read):
            self.assertEqual(creds._raw_machine_id_linux(), "etc-value")

    def test_missing_all_raises(self):
        with mock.patch.object(creds.Path, "read_text", side_effect=OSError("nope")):
            with self.assertRaises(creds.CredentialError):
                creds._raw_machine_id_linux()


class ContainerDetectionTests(unittest.TestCase):
    def _with_cgroup(self, content):
        return mock.patch(
            "builtins.open", mock.mock_open(read_data=content)
        )

    def test_docker_detected(self):
        with self._with_cgroup("12:pids:/docker/abc123\n"):
            self.assertTrue(creds._in_container_linux())

    def test_lxc_detected(self):
        with self._with_cgroup("1:name=systemd:/lxc/thing\n"):
            self.assertTrue(creds._in_container_linux())

    def test_plain_host_not_container(self):
        with self._with_cgroup("12:cpuset:/\n1:name=systemd:/init.scope\n"):
            self.assertFalse(creds._in_container_linux())

    def test_read_error_is_not_container(self):
        with mock.patch("builtins.open", side_effect=OSError("no /proc")):
            self.assertFalse(creds._in_container_linux())


class ProtectedIdTests(unittest.TestCase):
    def test_explicit_machine_uses_hmac(self):
        got = creds._protected_id(creds.APP_ID, "my-machine")
        want = hmac.new(
            b"my-machine", creds.APP_ID.encode(), hashlib.sha256
        ).hexdigest()
        self.assertEqual(got, want)

    def test_container_branch_uses_sha256_of_hostname_plus_appid(self):
        with mock.patch.object(creds.platform, "system", return_value="Linux"), \
             mock.patch.object(creds, "_in_container_linux", return_value=True), \
             mock.patch.object(creds, "_gen_id_linux", return_value="host1"):
            got = creds._protected_id(creds.APP_ID, None)
        want = hashlib.sha256(
            b"host1" + creds.APP_ID.encode()
        ).hexdigest()
        self.assertEqual(got, want)

    def test_non_container_linux_uses_hmac_of_machine_id(self):
        with mock.patch.object(creds.platform, "system", return_value="Linux"), \
             mock.patch.object(creds, "_in_container_linux", return_value=False), \
             mock.patch.object(creds, "machine_id", return_value="mid"), \
             mock.patch.object(creds, "_override", return_value=None):
            got = creds._protected_id(creds.APP_ID, None)
        want = hmac.new(
            b"mid", creds.APP_ID.encode(), hashlib.sha256
        ).hexdigest()
        self.assertEqual(got, want)

    def test_macos_regression_uses_hmac_of_ioplatformuuid(self):
        with mock.patch.object(creds.platform, "system", return_value="Darwin"), \
             mock.patch.object(creds, "_override", return_value=None), \
             mock.patch.object(
                 creds, "machine_id", return_value="AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
             ):
            got = creds._protected_id(creds.APP_ID, None)
        want = hmac.new(
            b"AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",
            creds.APP_ID.encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(got, want)


if __name__ == "__main__":
    unittest.main()
