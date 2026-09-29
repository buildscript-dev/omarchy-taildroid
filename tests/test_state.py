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


if __name__ == "__main__":
    unittest.main()
