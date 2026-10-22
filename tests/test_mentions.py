import unittest

from slackpost import mentions


USERS = [
    {"id": "U1", "name": "abhijeet", "real_name": "Abhijeet Bendkhale",
     "profile": {"display_name": "Abhijeet", "real_name": "Abhijeet Bendkhale"}},
    {"id": "U2", "name": "bagus", "real_name": "Bagus Zulmi",
     "profile": {"display_name": "bagus", "real_name": "Bagus Zulmi"}},
    {"id": "U3", "name": "gone", "deleted": True, "profile": {}},
]


class BuildDirectoryTests(unittest.TestCase):
    def test_maps_names(self):
        d = mentions.build_directory(USERS)
        self.assertEqual(d["abhijeet"], "U1")
        self.assertEqual(d["abhijeet bendkhale"], "U1")
        self.assertEqual(d["bagus"], "U2")

    def test_skips_deleted(self):
        d = mentions.build_directory(USERS)
        self.assertNotIn("gone", d)


class ResolveTextTests(unittest.TestCase):
    def setUp(self):
        self.dir = mentions.build_directory(USERS)

    def test_simple(self):
        self.assertEqual(mentions.resolve_text("hi @bagus", self.dir), "hi <@U2>")

    def test_case_insensitive(self):
        self.assertEqual(mentions.resolve_text("hi @BAGUS", self.dir), "hi <@U2>")

    def test_first_name(self):
        self.assertEqual(
            mentions.resolve_text("hi @Abhijeet", self.dir), "hi <@U1>"
        )

    def test_quoted_full_name(self):
        self.assertEqual(
            mentions.resolve_text('hi @"Abhijeet Bendkhale"', self.dir), "hi <@U1>"
        )

    def test_unknown_left_alone(self):
        self.assertEqual(
            mentions.resolve_text("hi @Nobody", self.dir), "hi @Nobody"
        )

    def test_already_resolved_untouched(self):
        text = "hi <@U2>"
        self.assertEqual(mentions.resolve_text(text, self.dir), text)


class UnresolvedTests(unittest.TestCase):
    def test_ignores_resolved(self):
        self.assertEqual(mentions.unresolved("ok <@U2>"), [])

    def test_reports_unknown(self):
        self.assertEqual(mentions.unresolved("hi @Nobody"), ["@Nobody"])

    def test_mixed(self):
        self.assertEqual(mentions.unresolved("<@U2> and @Nobody"), ["@Nobody"])


if __name__ == "__main__":
    unittest.main()
