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


class OpenChat(unittest.TestCase):
    """An open conversation has to update itself, not wait to be reopened."""

    def setUp(self):
        phoned.chat_log.clear()
        phoned.open_chat["key"] = KEY
        self.frames = []
        self.real_emit, phoned.emit = phoned.emit, self.frames.append
        self.real_save, phoned.save_chats = phoned.save_chats, lambda: None
        self.real_pub, phoned.publish_chats = phoned.publish_chats, lambda: None

    def tearDown(self):
        phoned.emit = self.real_emit
        phoned.save_chats = self.real_save
        phoned.publish_chats = self.real_pub
        phoned.open_chat["key"] = ""

    def chats(self):
        return [f for f in self.frames if f.get("type") == "chat"]

    def test_a_sent_reply_reaches_the_open_chat(self):
        log("Hi")
        phoned.log_sent(KEY, "hello back")
        sent = self.chats()[-1]["messages"][-1]
        self.assertEqual(sent["text"], "hello back")
        self.assertTrue(sent["out"])

    def test_another_chat_being_open_is_left_alone(self):
        phoned.open_chat["key"] = "WhatsApp\x00Someone else"
        log("Hi")
        phoned.log_sent(KEY, "hello back")
        self.assertEqual(self.chats()[-1]["messages"], [])


class Split(unittest.TestCase):
    def test_br_separates_messages_and_tags_go(self):
        self.assertEqual(phoned.split_lines("<b>~ Manoj</b><br/>photo<br />next"),
                         ["~ Manoj", "photo", "next"])

    def test_entities_are_read_back_as_characters(self):
        self.assertEqual(phoned.split_lines("Demo &amp; Mentor &lt;tag&gt; &quot;q&quot;"),
                         ['Demo & Mentor <tag> "q"'])

    def test_an_escaped_entity_is_not_decoded_twice(self):
        self.assertEqual(phoned.split_lines("&amp;lt;"), ["&lt;"])


if __name__ == "__main__":
    unittest.main()
