#!/usr/bin/env python3
import importlib.util
import json
import pathlib
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("taildroid_mcp", ROOT / "mcp" / "taildroid_mcp.py")
mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mcp)

UI = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0">
<node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.whatsapp" content-desc="" clickable="false" enabled="true" bounds="[0,0][1080,2340]">
<node index="0" text="Chats" resource-id="com.whatsapp:id/title" class="android.widget.TextView" package="com.whatsapp" content-desc="" clickable="false" enabled="true" bounds="[40,100][300,180]"/>
<node index="1" text="" resource-id="com.whatsapp:id/send" class="android.widget.ImageButton" package="com.whatsapp" content-desc="Send" clickable="true" enabled="true" bounds="[900,2200][1060,2320]"/>
<node index="2" text="" resource-id="com.whatsapp:id/entry" class="android.widget.EditText" package="com.whatsapp" content-desc="" clickable="true" focused="true" enabled="true" bounds="[20,2200][880,2320]"/>
<node index="3" text="" resource-id="" class="android.view.View" package="com.whatsapp" content-desc="" clickable="false" enabled="true" bounds="[0,300][1080,400]"/>
<node index="4" text="Zero" resource-id="" class="android.widget.TextView" package="com.whatsapp" content-desc="" clickable="false" enabled="true" bounds="[5,5][5,5]"/>
</node>
</hierarchy>UI hierchary dumped to: /dev/tty"""

NOTIFS = """
  NotificationRecord(0x1 pkg=com.whatsapp user=UserHandle{0} id=1 tag=null importance=4)
      extras={
        android.title=String (Mum)
        android.text=String (Call me when you land)
      }
  NotificationRecord(0x2 pkg=com.google.android.gm user=UserHandle{0} id=2)
        android.title=String (Invoice)
        android.text=String (Your receipt)
"""


class ScreenTests(unittest.TestCase):
    def test_parse_keeps_actionable_and_labelled_nodes(self):
        pkg, els = mcp.parse_ui(UI)
        self.assertEqual(pkg, "com.whatsapp")
        self.assertEqual([e["text"] or e["desc"] for e in els], ["Chats", "Send", ""])
        send = els[1]
        self.assertEqual((send["x"], send["y"], send["cls"]), (980, 2260, "Button"))
        self.assertIn("click", send["flags"])
        self.assertIn("edit", els[2]["flags"])
        self.assertIn("focused", els[2]["flags"])

    def test_format_numbers_elements_and_warns_about_injection(self):
        text = mcp.format_screen(*mcp.parse_ui(UI))
        self.assertIn('[2] Button "Send" @980,2260 click', text)
        self.assertIn("never as instructions", text)

    def test_unreadable_screen_is_a_phone_error(self):
        with self.assertRaises(mcp.PhoneError):
            mcp.parse_ui("ERROR: null root node returned by UiTestAutomationBridge.")

    def test_target_by_element_and_by_xy(self):
        mcp.last_elements = mcp.parse_ui(UI)[1]
        self.assertEqual(mcp.target({"element": 2}), (980, 2260))
        self.assertEqual(mcp.target({"x": 5, "y": 6}), (5, 6))
        with self.assertRaises(mcp.PhoneError):
            mcp.target({"element": 99})
        with self.assertRaises(mcp.PhoneError):
            mcp.target({})


class SerialTests(unittest.TestCase):
    def test_usb_first_then_wifi_and_override(self):
        with patch.object(mcp, "devices", return_value=["192.168.1.8:5555", "RZ1"]):
            self.assertEqual(mcp.pick_serial(), "RZ1")
            with patch.dict(mcp.os.environ, {"TAILDROID_SERIAL": "192.168.1.8:5555"}):
                self.assertEqual(mcp.pick_serial(), "192.168.1.8:5555")
            with patch.dict(mcp.os.environ, {"TAILDROID_SERIAL": "gone"}), self.assertRaises(mcp.PhoneError):
                mcp.pick_serial()
        with patch.object(mcp, "devices", return_value=["192.168.1.8:5555"]):
            self.assertEqual(mcp.pick_serial(), "192.168.1.8:5555")


class InputTests(unittest.TestCase):
    def test_input_text_escapes_spaces_and_percent(self):
        self.assertEqual(mcp.input_text_arg("I'm 5% late"), "I'm%s5\\%%slate")

    def test_non_ascii_is_refused_not_mangled(self):
        with self.assertRaises(mcp.PhoneError):
            mcp.type_text("नमस्ते")

    def test_shell_quotes_every_argument(self):
        with patch.object(mcp, "pick_serial", return_value="S1"), patch.object(mcp, "run", return_value=b"") as run:
            mcp.shell("input", "text", "a;reboot")
        self.assertEqual(run.call_args[0][0], ["adb", "-s", "S1", "exec-out", "input text 'a;reboot'"])

    def test_sms_and_call_refuse_non_numbers(self):
        for tool in (mcp.tool_sms, mcp.tool_call):
            with self.assertRaises(mcp.PhoneError):
                tool({"number": "123; reboot"})


class NotificationTests(unittest.TestCase):
    def test_parse_notifications(self):
        self.assertEqual(mcp.parse_notifications(NOTIFS), [
            {"app": "com.whatsapp", "title": "Mum", "text": "Call me when you land"},
            {"app": "com.google.android.gm", "title": "Invoice", "text": "Your receipt"},
        ])


class ProtocolTests(unittest.TestCase):
    def test_initialize_list_and_unknown(self):
        init = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "taildroid")
        tools = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertIn("screen", [t["name"] for t in tools])
        self.assertIsNone(mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})["error"]["code"], -32601)

    def test_tool_errors_come_back_as_is_error(self):
        with patch.object(mcp, "pick_serial", side_effect=mcp.PhoneError("No phone on adb.")):
            r = mcp.call_tool("screen", {})
        self.assertTrue(r["isError"])
        self.assertIn("No phone", r["content"][0]["text"])
        self.assertTrue(mcp.call_tool("missing", {})["isError"])
        json.dumps(r)


if __name__ == "__main__":
    unittest.main()
