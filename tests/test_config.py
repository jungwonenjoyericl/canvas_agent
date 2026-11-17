"""Validate configuration without consulting real credentials or the network."""

import tempfile
import unittest
from pathlib import Path

from canvas_agent.config import ConfigError, StyleSettings, load_settings


class ConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "config.toml"

    def write_config(self, content):
        self.path.write_text(content, encoding="utf-8")

    def test_demo_defaults_require_no_file(self):
        settings = load_settings(self.path, demo=True, environ={})
        self.assertEqual(settings.days_ahead, 14)
        self.assertEqual(settings.zone.key, "Europe/Stockholm")
        self.assertEqual(settings.course_ids, ())
        self.assertFalse(settings.include_completed)

    def test_live_run_requires_a_config_file(self):
        with self.assertRaisesRegex(ConfigError, "Copy config.example.toml"):
            load_settings(self.path, environ={})

    def test_environment_overrides_credentials_but_preserves_preferences(self):
        self.write_config(
            '[canvas]\nbase_url="https://file.example"\ntoken="file-token"\n'
            "course_ids=[10, 20]\n[study]\ndays_ahead=7\n"
        )
        settings = load_settings(
            self.path,
            environ={
                "CANVAS_BASE_URL": "https://env.example",
                "CANVAS_API_TOKEN": "env-token",
            },
        )
        self.assertEqual(settings.base_url, "https://env.example")
        self.assertEqual(settings.token, "env-token")
        self.assertEqual(settings.course_ids, (10, 20))
        self.assertEqual(settings.days_ahead, 7)
        self.assertNotIn("env-token", repr(settings))

    def test_invalid_value_types_are_rejected(self):
        cases = [
            ("canvas=5", "canvas must be a TOML table"),
            ("[canvas]\nbase_url=42", "must be strings"),
            ("[canvas]\ncourse_ids=[true]", "positive integers"),
            ("[canvas]\ncourse_ids=[0]", "positive integers"),
            ("[canvas]\ncourse_ids=42", "positive integers"),
            ("[study]\ndays_ahead=true", "nonnegative integer"),
            ("[study]\ndays_ahead=-1", "nonnegative integer"),
            ('[study]\ninclude_completed="yes"', "must be true or false"),
            ("[study]\ntimezone=42", "IANA timezone string"),
            ("[style]\ntitle=42", "must be strings"),
        ]
        for content, message in cases:
            with self.subTest(content=content):
                self.write_config(content)
                with self.assertRaisesRegex(ConfigError, message):
                    load_settings(self.path, environ={})

    def test_unknown_timezone_has_an_actionable_error(self):
        self.write_config('[study]\ntimezone="Unknown/Zone"')
        with self.assertRaisesRegex(ConfigError, "Unknown timezone"):
            load_settings(self.path, environ={})

    def test_invalid_toml_does_not_echo_secret_values(self):
        self.write_config('[canvas]\ntoken="secret-token')
        with self.assertRaisesRegex(ConfigError, "Invalid TOML") as error:
            load_settings(self.path, environ={})
        self.assertNotIn("secret-token", str(error.exception))

    def test_invalid_templates_are_rejected_without_any_assignment_data(self):
        for template in ("$unknown", "${name", "$", "${__class__}"):
            with self.subTest(template=template):
                with self.assertRaisesRegex(ConfigError, "style.item_template"):
                    StyleSettings(item_template=template)

    def test_templates_allow_literal_dollar_signs(self):
        style = StyleSettings(item_template="${name} — $$5")
        self.assertEqual(style.item_template, "${name} — $$5")


if __name__ == "__main__":
    unittest.main()
