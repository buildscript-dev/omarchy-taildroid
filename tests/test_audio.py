#!/usr/bin/env python3
"""Where the phone's audio should land, per phoned's routing rule."""
import importlib.util
import pathlib
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("phoned", ROOT / "phoned" / "phoned.py")
phoned = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(phoned)

CARDS = """Card #71
\tName: bluez_card.48_EF_1C_4D_E8_3F
\tDriver: module-bluez5-device.c
\tProfiles:
\t\toff: Off (sinks: 0, sources: 0, priority: 0, available: yes)
\t\taudio-gateway: Audio Gateway (sinks: 0, sources: 0, priority: 256, available: yes)
\tActive Profile: audio-gateway
Card #48
\tName: alsa_card.pci-0000_01_00.1
\tActive Profile: off
"""


class Route(unittest.TestCase):
    def setUp(self):
        phoned.calls.clear()
        phoned.audio.update(mode="follow", screenOn=False, route="pc", since=0.0, dialUntil=0.0)

    def test_dark_phone_gives_the_laptop_the_audio(self):
        self.assertEqual(phoned.wanted_route(), "pc")

    def test_phone_in_hand_keeps_its_own_audio(self):
        phoned.audio["screenOn"] = True
        self.assertEqual(phoned.wanted_route(), "phone")

    def test_a_glance_does_not_yank_the_audio_back(self):
        phoned.audio.update(screenOn=False, route="phone", since=time.time())
        self.assertEqual(phoned.wanted_route(), "phone")
        phoned.audio["since"] = time.time() - phoned.AUDIO_GRACE - 1
        self.assertEqual(phoned.wanted_route(), "pc")

    def test_ringing_keeps_the_link_so_the_island_can_answer(self):
        phoned.audio["screenOn"] = True  # the incoming call woke the screen
        phoned.calls["/c1"] = {"state": "incoming", "onPhone": False}
        self.assertEqual(phoned.wanted_route(), "pc")

    def test_answered_on_the_handset_moves_the_audio_there(self):
        phoned.calls["/c1"] = {"state": "active", "onPhone": True}
        self.assertEqual(phoned.wanted_route(), "phone")

    def test_answered_here_stays_here_even_if_the_screen_wakes(self):
        phoned.audio["screenOn"] = True
        phoned.calls["/c1"] = {"state": "active", "onPhone": False}
        self.assertEqual(phoned.wanted_route(), "pc")

    def test_manual_override_beats_everything(self):
        phoned.audio.update(mode="pc", screenOn=True)
        self.assertEqual(phoned.wanted_route(), "pc")
        phoned.audio["mode"] = "phone"
        self.assertEqual(phoned.wanted_route(), "phone")


class Profiles(unittest.TestCase):
    def test_reads_the_active_and_the_fallback_profile(self):
        phoned.run = lambda *a, **k: CARDS
        self.assertEqual(phoned.card_profiles("bluez_card.48_EF_1C_4D_E8_3F"),
                         ("audio-gateway", "audio-gateway"))

    def test_unknown_card_is_empty(self):
        phoned.run = lambda *a, **k: CARDS
        self.assertEqual(phoned.card_profiles("bluez_card.nope"), ("", ""))


if __name__ == "__main__":
    unittest.main()
