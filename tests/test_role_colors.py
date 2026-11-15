# SPDX-License-Identifier: Apache-2.0
"""Each role (builder, reviewer, CTO) has one default colour in Mission Control, and the owner can change it in Settings (issue #92).

The colour is saved with the rest of the Settings form, an older Settings file loads with the defaults, and every colour on offer keeps
the role's name readable (4.5:1 on white). Machine inputs removed: Settings live in a temporary home.

Run:  PYTHONPATH=. python3 -m unittest tests.test_role_colors -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness import board_viewer, control, global_settings
from tests.pinned_settings import old_settings


def contrast_on_white(hex_color: str) -> float:
    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    red, green, blue = (channel(int(hex_color[index:index + 2], 16) / 255) for index in (1, 3, 5))
    return 1.05 / (0.2126 * red + 0.7152 * green + 0.0722 * blue + 0.05)


class RoleColors(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "home"
        self.home.mkdir()

    def test_every_role_starts_on_its_own_default_color(self):
        self.assertEqual(control.DEFAULT_ROLE_COLORS, {"delivery": "teal", "reviewer": "coral", "cto": "purple"})
        self.assertEqual(set(control.DEFAULT_ROLE_COLORS), set(control.ROLE_SETTINGS), "one colour for each role Settings lists")
        self.assertEqual(global_settings.initialize(self.home)["role_colors"], control.DEFAULT_ROLE_COLORS)
        self.assertEqual(board_viewer.role_color_view(None), {
            "builder": {"hex": "#14b8a6", "ink": "#0f766e"},
            "reviewer": {"hex": "#f97361", "ink": "#b8392a"},
            "cto": {"hex": "#8b7cf6", "ink": "#5b4bc4"},
        }, "Mission Control's colours before Settings had a choice")

    def test_every_color_on_offer_keeps_the_name_readable(self):
        for color_id, color in control.ROLE_COLOR_CHOICES.items():
            self.assertGreaterEqual(contrast_on_white(color["ink"]), 4.5, color_id)

    def test_the_choice_is_saved_with_the_form_and_reaches_mission_control(self):
        saved = global_settings.update_agent_settings(self.home, old_settings(), None, None, {"cto": "blue", "delivery": "green"})
        self.assertEqual(global_settings.load(self.home)["role_colors"], {"delivery": "green", "reviewer": "coral", "cto": "blue"})
        self.assertEqual(saved["agent_settings"], global_settings.load(self.home)["agent_settings"], "the AI choices are saved as before")
        view = board_viewer.role_color_view(global_settings.load(self.home))
        self.assertEqual(view["builder"], {key: control.ROLE_COLOR_CHOICES["green"][key] for key in ("hex", "ink")})
        self.assertEqual(view["cto"]["hex"], control.ROLE_COLOR_CHOICES["blue"]["hex"])
        self.assertEqual(view["reviewer"]["hex"], control.ROLE_COLOR_CHOICES["coral"]["hex"])
        global_settings.update_agent_settings(self.home, old_settings())
        self.assertEqual(global_settings.load(self.home)["role_colors"]["cto"], "blue", "a save without colours leaves them as they were")

    def test_an_unknown_color_or_role_is_refused_and_nothing_is_saved(self):
        global_settings.update_agent_settings(self.home, old_settings(), None, None, {"cto": "blue"})
        for bad in ({"cto": "#ff0000"}, {"owner": "teal"}, ["teal"]):
            with self.assertRaises(ValueError, msg=bad):
                global_settings.update_agent_settings(self.home, old_settings(), None, None, bad)
        self.assertEqual(global_settings.load(self.home)["role_colors"]["cto"], "blue")

    def test_an_older_or_damaged_settings_file_loads_with_the_defaults(self):
        saved = global_settings.update_agent_settings(self.home, old_settings(), None, None, {"reviewer": "pink"})
        path = self.home / global_settings.SETTINGS_FILENAME
        document = json.loads(path.read_text(encoding="utf-8"))
        document.pop("role_colors")
        path.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(global_settings.load(self.home)["role_colors"], control.DEFAULT_ROLE_COLORS)
        self.assertEqual(global_settings.load(self.home)["agent_settings"], saved["agent_settings"])
        document["role_colors"] = {"reviewer": "not-a-color", "cto": "amber"}
        path.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(global_settings.load(self.home)["role_colors"], {"delivery": "teal", "reviewer": "coral", "cto": "amber"},
                         "only the unusable value falls back to its default")


if __name__ == "__main__":
    unittest.main()
