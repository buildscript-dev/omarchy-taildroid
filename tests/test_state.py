#!/usr/bin/env python3
"""phoned's own bookkeeping: the state file, notification order, Bluetooth retries."""
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("phoned", ROOT / "phoned" / "phoned.py")
phoned = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(phoned)


class Remember(unittest.TestCase):
    def test_a_failed_write_keeps_the_old_file(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(phoned, "MEMORY", os.path.join(d, "phone.json")), \
                mock.patch.dict(phoned.memory, clear=True):
            phoned.remember(wifiAddress="10.0.0.2")
            with mock.patch.object(phoned.json, "dump", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    phoned.remember(model="SM-S921E")
            with open(phoned.MEMORY) as f:
                self.assertEqual(json.load(f), {"wifiAddress": "10.0.0.2"})


class NotifOrder(unittest.TestCase):
    def test_ten_follows_nine(self):
        with mock.patch.dict(phoned.phone_notifs, clear=True), mock.patch.object(phoned, "changed"):
            for nid in ("10", "9", "1"):
                phoned.phone_notifs[nid] = {"id": nid}
            phoned.publish_notifs()
            self.assertEqual([n["id"] for n in phoned.state["phoneNotifs"]], ["1", "9", "10"])


class BluetoothRetry(unittest.TestCase):
    def scan(self, connected):
        dev = {"Address": "48:EF:1C:4D:E8:3F", "Alias": "S24", "Paired": True, "Connected": connected, "Icon": "phone"}
        om = mock.Mock()
        om.GetManagedObjects.return_value = {"/org/bluez/hci0/dev_48": {"org.bluez.Device1": dev}}
        device = mock.Mock()
        with mock.patch.object(phoned.dbus, "Interface", side_effect=lambda obj, iface: om if "ObjectManager" in iface else device), \
                mock.patch.object(phoned.system, "get_object"), mock.patch.object(phoned, "changed"), \
                mock.patch.object(phoned, "event"), mock.patch.object(phoned, "remember"), \
                mock.patch.object(phoned, "wifi_reconnect"), mock.patch.object(phoned.GLib, "timeout_add"), \
                mock.patch.object(phoned.GLib, "timeout_add_seconds"):
            phoned.bt_scan()
        return device.Connect.call_count

    def test_retries_back_off_until_the_phone_connects(self):
        phoned.state["phone"]["serial"] = "RZCY90VSWBA"
        phoned.state["bluetooth"] = {}
        phoned.bt_retry.update(at=0, wait=phoned.BT_RETRY_MIN)
        self.assertEqual(self.scan(False), 1)
        self.assertEqual(self.scan(False), 0)  # inside the back-off window
        self.assertEqual(phoned.bt_retry["wait"], 2 * phoned.BT_RETRY_MIN)
        phoned.bt_retry["at"] = 0
        self.assertEqual(self.scan(False), 1)
        self.scan(True)
        self.assertEqual(phoned.bt_retry["wait"], phoned.BT_RETRY_MIN)


class Pruning(unittest.TestCase):
    def test_cache_keeps_newest_threads_messages_and_chats(self):
        m = {1: {"a": {"date": 1, "attachments": [{"uid": "p1"}]}},
             2: {"b": {"date": 5, "attachments": []}, "c": {"date": 6, "attachments": []}, "d": {"date": 7, "attachments": []}}}
        t = {1: {"date": 1}, 2: {"date": 7}}
        with mock.patch.dict(phoned.messages, m, clear=True), mock.patch.dict(phoned.threads, t, clear=True), \
                mock.patch.dict(phoned.attachment_files, {"p1": "/x"}, clear=True), \
                mock.patch.dict(phoned.open_thread, {"id": 0}):
            phoned.prune_messages(2, per_thread=2, keep_threads=1)
            self.assertEqual(list(phoned.threads), [2])
            self.assertEqual(sorted(phoned.messages[2]), ["c", "d"])
            self.assertEqual(phoned.attachment_files, {})
        log = {str(i): {"date": i} for i in range(5)}
        with mock.patch.dict(phoned.chat_log, log, clear=True), mock.patch.object(phoned, "write_json"):
            phoned.save_chats(keep=2)
            self.assertEqual(sorted(phoned.chat_log), ["3", "4"])


class NeighborIp(unittest.TestCase):
    def test_finds_the_phone_by_mac_after_an_address_change(self):
        table = "192.168.1.1 dev wlo1 lladdr 90:f0 REACHABLE\n192.168.1.23 dev wlo1 lladdr 36:fb STALE\n"
        self.assertEqual(phoned.neighbor_ip("36:fb", table), "192.168.1.23")
        self.assertEqual(phoned.neighbor_ip("", table), "")
        self.assertEqual(phoned.neighbor_ip("aa:bb", table), "")


class RadioName(unittest.TestCase):
    def test_skips_wifi_calling_and_names_5g(self):
        self.assertEqual(phoned.radio_name("IWLAN,LTE"), "LTE")
        self.assertEqual(phoned.radio_name("NR_SA,IWLAN"), "5G")
        self.assertEqual(phoned.radio_name("IWLAN,Unknown"), "")


class SocketAnswers(unittest.TestCase):
    def test_thread_is_oldest_first_and_limited(self):
        msgs = {i: {"uid": i, "date": 1000 - i, "body": str(i)} for i in range(5)}
        with mock.patch.dict(phoned.messages, {7: msgs}, clear=True), \
                mock.patch.dict(phoned.state["kdeconnect"], deviceId=""):
            r = phoned.answer({"q": "thread", "threadId": 7, "limit": 3})
        self.assertEqual([m["uid"] for m in r["messages"]], [2, 1, 0])

    def test_a_thread_with_one_message_asks_the_phone_for_history(self):
        conv = mock.Mock()
        with mock.patch.dict(phoned.messages, {7: {1: {"uid": 1, "date": 1}}}, clear=True), \
                mock.patch.dict(phoned.state["kdeconnect"], deviceId="dev"), \
                mock.patch.object(phoned, "kdec", return_value=conv):
            phoned.answer({"q": "thread", "threadId": 7})
        conv.requestConversation.assert_called_once()

    def test_unknown_query_is_an_error_not_a_crash(self):
        self.assertIn("error", phoned.answer({"q": "send"}))

    def test_socket_is_private_and_answers(self):
        import socket
        with tempfile.TemporaryDirectory() as d, mock.patch.object(phoned, "SOCK", os.path.join(d, "s")):
            srv = phoned.serve_socket()
            self.assertEqual(os.stat(phoned.SOCK).st_mode & 0o777, 0o600)
            c = socket.socket(socket.AF_UNIX)
            c.connect(phoned.SOCK)
            c.sendall(b'{"q": "chat", "key": "nope"}\n')
            ctx = phoned.GLib.MainContext.default()
            for _ in range(20):
                ctx.iteration(False)
            self.assertEqual(json.loads(c.recv(4096)), {"messages": []})
            c.close()
            srv.close()


if __name__ == "__main__":
    unittest.main()
