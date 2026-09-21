#!/usr/bin/env python3
"""A notification app's stacked lines have to become a conversation."""
import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("phoned", ROOT / "phoned" / "phoned.py")
phoned = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(phoned)

KEY = "WhatsApp\x00Lucky"


def log(*lines):
    return phoned.log_lines(KEY, "WhatsApp", "Lucky", "", "rid", list(lines))


class ChatLog(unittest.TestCase):
    def setUp(self):
        phoned.chat_log.clear()

    def texts(self):
        return [m["text"] for m in phoned.chat_log[KEY]["messages"]]

    def test_first_stack_is_kept_in_order(self):
        self.assertTrue(log("Hi", "you there"))
        self.assertEqual(self.texts(), ["Hi", "you there"])

    def test_a_restacked_notification_only_adds_what_is_new(self):
        log("Hi", "you there")
        self.assertTrue(log("Hi", "you there", "third"))
        self.assertEqual(self.texts(), ["Hi", "you there", "third"])

    def test_the_same_stack_again_adds_nothing(self):
        log("Hi", "you there")
        self.assertFalse(log("Hi", "you there"))
        self.assertEqual(self.texts(), ["Hi", "you there"])

    def test_a_trimmed_stack_still_lines_up(self):
        # WhatsApp drops the oldest lines once a chat gets long.
        log("one", "two", "three")
        self.assertTrue(log("two", "three", "four"))
        self.assertEqual(self.texts(), ["one", "two", "three", "four"])

    def test_an_unrelated_message_is_appended_whole(self):
        log("one")
        self.assertTrue(log("totally different"))
        self.assertEqual(self.texts(), ["one", "totally different"])

    def test_blank_lines_are_dropped(self):
        self.assertTrue(log("", "real", ""))
        self.assertEqual(self.texts(), ["real"])

    def test_replies_land_on_the_right_side(self):
        log("Hi")
        phoned.log_sent(KEY, "hello back")
        self.assertEqual(self.texts(), ["Hi", "hello back"])
        self.assertTrue(phoned.chat_log[KEY]["messages"][-1]["out"])


class Split(unittest.TestCase):
    def test_br_separates_messages_and_tags_go(self):
        self.assertEqual(phoned.split_lines("<b>~ Manoj</b><br/>photo<br />next"),
                         ["~ Manoj", "photo", "next"])


if __name__ == "__main__":
    unittest.main()
