"""Read pushes use synthetic IDs and mocked UI calls; never open Messages."""
import contextlib
import importlib.machinery
import importlib.util
import io
import plistlib
from pathlib import Path
import unittest
import os
import sqlite3
import tempfile
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from subprocess import CompletedProcess

loader = importlib.machinery.SourceFileLoader("imsg_read", str(Path(__file__).with_name("imsg-read")))
spec = importlib.util.spec_from_loader(loader.name, loader)
read = importlib.util.module_from_spec(spec)
loader.exec_module(read)


class ReadTests(unittest.TestCase):
    def test_group_and_direct_urls(self):
        for identifier, key in [("chat12345", "groupid"), ("a" * 32, "groupid"),
                                ("+15551234567", "address"), ("12345", "address"),
                                ("person+tag@example.com", "address")]:
            self.assertEqual(parse_qs(urlparse(read.chat_url(identifier)).query), {key: [identifier]})
            self.assertTrue(read.chat_url(identifier).startswith("imessage:open?"))

    def test_private_lock_refuses_symlinks(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(read.os.path, "expanduser", return_value=folder):
            with read.messages_ui_lock():
                self.assertEqual(os.stat(Path(folder) / "messages-ui.lock").st_mode & 0o777, 0o600)
            (Path(folder) / "messages-ui.lock").unlink()
            (Path(folder) / "messages-ui.lock").symlink_to(Path(folder) / "other")
            with self.assertRaises(OSError):
                with read.messages_ui_lock():
                    self.fail("symlink lock accepted")

    def test_no_url_parameter_injection(self):
        handle = "person&body=wrong@example.com"
        self.assertEqual(parse_qs(urlparse(read.chat_url(handle)).query), {"address": [handle]})
        for value in ["", "group name", "chat123?body=bad", "person\x00@example.com", "a" * 255]:
            with self.assertRaises(ValueError):
                read.chat_url(value)

    def run_chat(self, before=1, select_error="", click_error=""):
        with patch.object(read.sys, "argv", ["imsg-read", "--chat", "chat12345"]), \
             patch.object(read, "ensure_messages", return_value=""), \
             patch.object(read, "accessibility", return_value=""), \
             patch.object(read, "unread_on_mac", return_value=before), \
             patch.object(read, "frontmost", return_value="Previous app"), \
             patch.object(read, "select_chat", return_value=select_error) as select, \
             patch.object(read, "click", return_value=(not click_error, click_error)) as click, \
             patch.object(read, "settle", return_value=0) as settle, \
             patch.object(read, "restore_front") as restore:
            try:
                read.main()
            except SystemExit:
                pass
            return select, click, settle, restore

    def test_group_read_and_focus_restored(self):
        select, click, settle, restore = self.run_chat()
        select.assert_called_once_with("chat12345")
        click.assert_called_once_with("Mark as Read")
        settle.assert_called_once_with(1, "chat12345")
        restore.assert_called_once_with("Previous app")

    def test_open_failure_never_clicks_another_conversation(self):
        _, click, _, restore = self.run_chat(select_error="could not open")
        click.assert_not_called()
        restore.assert_called_once()

    def test_menu_failure_restores_focus(self):
        _, _, _, restore = self.run_chat(click_error="not available")
        restore.assert_called_once()

    def test_url_dispatch_without_a_window_is_not_selection_success(self):
        with patch.object(read.subprocess, "run", return_value=CompletedProcess([], 0)), \
             patch.object(read, "osa", return_value=(0, "0")):
            self.assertIn("no accessible window", read.select_chat("chat12345"))

    def test_failed_url_dispatch_does_not_query_or_click_a_window(self):
        with patch.object(read.subprocess, "run", return_value=CompletedProcess([], 1)), \
             patch.object(read, "osa") as osa:
            self.assertIn("could not open", read.select_chat("chat12345"))
            osa.assert_not_called()

    def test_already_read_never_opens_messages(self):
        select, click, settle, restore = self.run_chat(before=0)
        for call in (select, click, settle, restore):
            call.assert_not_called()



class ClusterScopedUnread(unittest.TestCase):
    """The push's referee must count the whole conversation, not one chat row.

    Messages splits one conversation across several chat rows: a re-keyed
    group keeps its retired row, and a merged 1:1 keeps a phone row beside an
    email row. Blip pushes ONE identifier. When the unread sits on an alias,
    a per-row count reports "nothing unread" and --chat exits 0 having done
    nothing at all.
    """

    SCHEMA = """
        CREATE TABLE chat (
          ROWID INTEGER PRIMARY KEY,
          chat_identifier TEXT,
          guid TEXT,
          display_name TEXT,
          style INTEGER,
          group_id TEXT,
          original_group_id TEXT,
          last_read_message_timestamp INTEGER DEFAULT 0
        );
        CREATE TABLE message (
          ROWID INTEGER PRIMARY KEY,
          date INTEGER,
          item_type INTEGER DEFAULT 0,
          is_from_me INTEGER DEFAULT 0,
          is_read INTEGER DEFAULT 0,
          associated_message_type INTEGER DEFAULT 0,
          group_title TEXT
        );
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE chat_recoverable_message_join (message_id INTEGER);
    """

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = str(Path(self.folder.name) / "chat.db")
        con = sqlite3.connect(self.db)
        con.executescript(self.SCHEMA)
        con.commit()
        con.close()
        # imsg caches the message columns from whichever database it saw first.
        read.imsg_module()._COLS = None
        self.patch = patch.object(read, "DB_PATH", self.db)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def write(self, statements):
        con = sqlite3.connect(self.db)
        for sql, args in statements:
            con.execute(sql, args)
        con.commit()
        con.close()

    def unread_row(self, chat_row, msg_row, date):
        return [
            ("INSERT INTO message (ROWID, date, is_from_me, is_read) VALUES (?, ?, 0, 0)", (msg_row, date)),
            ("INSERT INTO chat_message_join VALUES (?, ?)", (chat_row, msg_row)),
        ]

    def test_merged_dm_counts_the_unread_on_its_other_handle(self):
        """Phone row + email row, one group_id. The unread is on the email row;
        Blip pushes the phone identifier."""
        rows = [
            ("INSERT INTO chat (ROWID, chat_identifier, guid, display_name, style, group_id) "
             "VALUES (1, '+15550100001', 'SMS;-;+15550100001', '', 45, 'MERGED-DM-1')", ()),
            ("INSERT INTO chat (ROWID, chat_identifier, guid, display_name, style, group_id) "
             "VALUES (2, 'pat@example.com', 'iMessage;-;pat@example.com', '', 45, 'MERGED-DM-1')", ()),
            ("INSERT INTO handle VALUES (1, '+15550100001')", ()),
            ("INSERT INTO handle VALUES (2, 'pat@example.com')", ()),
            ("INSERT INTO chat_handle_join VALUES (1, 1)", ()),
            ("INSERT INTO chat_handle_join VALUES (2, 2)", ()),
        ]
        self.write(rows + self.unread_row(2, 10, 700000000000000000))
        self.assertEqual(read.unread_on_mac("+15550100001"), 1)
        self.assertEqual(read.unread_on_mac("pat@example.com"), 1)

    def test_rekeyed_group_counts_the_unread_on_its_retired_row(self):
        """Same name, same members, new chat_identifier and new group_id. The
        unread is on the retired row; Blip pushes the live one."""
        live, retired = "4b3d072e07b14bf88b4b8fde00deebcf", "chat909594254947022019"
        rows = [
            ("INSERT INTO chat (ROWID, chat_identifier, guid, display_name, style, group_id) "
             "VALUES (1, ?, 'any;+;' || ?, 'Sportsball!', 43, 'LIVE-GROUP')", (live, live)),
            ("INSERT INTO chat (ROWID, chat_identifier, guid, display_name, style, group_id) "
             "VALUES (2, ?, 'any;+;' || ?, 'Sportsball!', 43, 'RETIRED-GROUP')", (retired, retired)),
            ("INSERT INTO handle VALUES (1, '+15550100001')", ()),
            ("INSERT INTO handle VALUES (2, '+15550100002')", ()),
            ("INSERT INTO chat_handle_join VALUES (1, 1)", ()),
            ("INSERT INTO chat_handle_join VALUES (1, 2)", ()),
            ("INSERT INTO chat_handle_join VALUES (2, 1)", ()),
            ("INSERT INTO chat_handle_join VALUES (2, 2)", ()),
        ]
        # The live row is the one with the newest message, as Messages has it.
        self.write(rows + [
            ("INSERT INTO message (ROWID, date, is_from_me, is_read) VALUES (5, 800000000000000000, 1, 1)", ()),
            ("INSERT INTO chat_message_join VALUES (1, 5)", ()),
        ] + self.unread_row(2, 11, 700000000000000000))
        self.assertEqual(read.unread_on_mac(live), 1)

    def test_an_unrelated_conversation_is_never_folded_in(self):
        """Two chats that share nothing stay two: the widened scope must not
        turn every push into a mark-all."""
        rows = [
            ("INSERT INTO chat (ROWID, chat_identifier, guid, display_name, style, group_id) "
             "VALUES (1, '+15550100001', 'iMessage;-;+15550100001', '', 45, 'DM-ONE')", ()),
            ("INSERT INTO chat (ROWID, chat_identifier, guid, display_name, style, group_id) "
             "VALUES (2, '+15550100002', 'iMessage;-;+15550100002', '', 45, 'DM-TWO')", ()),
            ("INSERT INTO handle VALUES (1, '+15550100001')", ()),
            ("INSERT INTO handle VALUES (2, '+15550100002')", ()),
            ("INSERT INTO chat_handle_join VALUES (1, 1)", ()),
            ("INSERT INTO chat_handle_join VALUES (2, 2)", ()),
        ]
        self.write(rows + self.unread_row(2, 12, 700000000000000000))
        self.assertEqual(read.unread_on_mac("+15550100001"), 0)
        self.assertEqual(read.unread_on_mac("+15550100002"), 1)

    def test_an_unreadable_cluster_still_counts_the_pushed_row(self):
        """imsg gone or a schema it cannot read: fall back to the identifier
        alone rather than losing the count entirely."""
        self.write(self.unread_row(1, 13, 700000000000000000) + [
            ("INSERT INTO chat (ROWID, chat_identifier, guid, display_name, style, group_id) "
             "VALUES (1, '+15550100001', 'iMessage;-;+15550100001', '', 45, NULL)", ()),
        ])
        with patch.object(read, "imsg_module", side_effect=OSError("no imsg here")):
            self.assertEqual(read.unread_on_mac("+15550100001"), 1)
            self.assertEqual(read.unread_on_mac("+15550100009"), 0)


class ConversationActions(unittest.TestCase):
    """Pin, Unpin, Hide Alerts, Show Alerts: four Mac writes through Messages'
    Conversation menu. Every UI call is mocked; nothing opens Messages."""

    DM = "+15550100001"
    FLAGS = {"--pin": ("Pin", "chat_pinned", True, "pinned"),
             "--unpin": ("Unpin", "chat_pinned", False, "unpinned"),
             "--mute": ("Hide Alerts", "chat_muted", True, "muted"),
             "--unmute": ("Show Alerts", "chat_muted", False, "unmuted")}

    WANT = object()   # the probe reports exactly what the action asked for

    def run_action(self, flag, probe=WANT, unread=(0,), select_error="", clicks=((True, ""),),
                   front=("Previous app", "Messages"), handle=DM):
        """Run main() for one action. Returns (exit code, stdout, stderr, calls)."""
        item, probe_name, want, _ = self.FLAGS[flag]
        calls = []
        fronts = list(front)
        clicks = list(clicks)
        unread = list(unread)

        def frontmost():
            calls.append("frontmost")
            return fronts.pop(0) if fronts else "Messages"

        def select(handle):
            calls.append(f"select:{handle}")
            return select_error

        def click(name):
            calls.append(f"click:{name}")
            return clicks.pop(0) if clicks else (True, "")

        def count(handle=None):
            calls.append("unread")
            return unread.pop(0) if len(unread) > 1 else unread[0]

        def restore(prev):
            calls.append(f"restore:{prev}")

        def check(handle):
            calls.append("verify")
            return want if probe is self.WANT else probe

        out, err = io.StringIO(), io.StringIO()
        code = 0
        with patch.object(read.sys, "argv", ["imsg-read", flag, handle]), \
             patch.object(read, "ensure_messages", return_value=""), \
             patch.object(read, "accessibility", return_value=""), \
             patch.object(read, "frontmost", side_effect=frontmost), \
             patch.object(read, "osa", return_value=(0, "")), \
             patch.object(read, "select_chat", side_effect=select), \
             patch.object(read, "click", side_effect=click), \
             patch.object(read, "unread_on_mac", side_effect=count), \
             patch.object(read, "restore_front", side_effect=restore), \
             patch.object(read, probe_name, side_effect=check), \
             patch.object(read.time, "sleep"), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                read.main()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue(), calls

    def test_front_app_is_captured_before_the_conversation_opens(self):
        """`open imessage:` can foreground Messages by itself; capturing after
        it restored Messages to Messages and left the user's app behind."""
        for flag, (item, _, _, word) in self.FLAGS.items():
            with self.subTest(flag=flag):
                code, out, _, calls = self.run_action(flag)
                self.assertEqual(code, 0)
                self.assertEqual(out.strip(), word)
                self.assertLess(calls.index("frontmost"), calls.index(f"select:{self.DM}"))
                self.assertLess(calls.index(f"select:{self.DM}"), calls.index(f"click:{item}"))
                self.assertIn(f"click:{item}", calls)
                self.assertEqual(calls[-1], "restore:Previous app")

    def test_dormant_menu_wakes_messages_and_still_restores_the_original_app(self):
        code, _, _, calls = self.run_action("--pin", clicks=((False, ""), (True, "")))
        self.assertEqual(code, 0)
        self.assertEqual(calls.count("click:Pin"), 2)
        self.assertEqual(calls[-1], "restore:Previous app")

    def test_selection_failure_never_clicks_and_restores_focus(self):
        code, _, err, calls = self.run_action("--mute", select_error="could not open the conversation")
        self.assertEqual(code, 64)
        self.assertIn("could not open", err)
        self.assertFalse([c for c in calls if c.startswith("click:")])
        self.assertEqual(calls[-1], "restore:Previous app")

    def test_menu_error_restores_focus(self):
        code, _, _, calls = self.run_action("--unmute", clicks=((False, "boom"),))
        self.assertEqual(code, 1)
        self.assertNotIn("verify", calls)
        self.assertEqual(calls[-1], "restore:Previous app")

    def test_verified_failed_and_unknown(self):
        for flag, (_, _, want, word) in self.FLAGS.items():
            with self.subTest(flag=flag, outcome="failed"):
                code, out, err, _ = self.run_action(flag, probe=not want)
                self.assertEqual(code, 75)
                self.assertEqual(out, "")
                self.assertIn(f"is not {word}", err)
            with self.subTest(flag=flag, outcome="unknown"):
                # Unknown is never success - not for the "off" actions either.
                code, out, err, _ = self.run_action(flag, probe=None)
                self.assertEqual(code, 75)
                self.assertEqual(out, "")
                self.assertIn("cannot confirm", err)

    def test_settle_flag_waits_through_a_transient_unknown(self):
        with patch.object(read.time, "sleep"):
            self.assertIs(read.settle_flag(iter([None, None, False]).__next__, False), False)
            self.assertIsNone(read.settle_flag(lambda: None, False, tries=3))
            self.assertIs(read.settle_flag(lambda: True, False, tries=3), True)

    def test_groups_are_refused_before_anything_opens(self):
        for group in ("chat12345", "a" * 32):
            with self.subTest(group=group):
                code, _, err, calls = self.run_action("--pin", handle=group)
                self.assertEqual(code, 64)
                self.assertIn("group", err)
                self.assertFalse([c for c in calls if c.startswith(("select:", "click:", "frontmost"))])

    def test_pin_or_mute_puts_back_an_unread_that_opening_it_read(self):
        """Selecting shows the DM and Messages reads what it shows. A pin is
        not a read: the unread goes back on while the chat is still selected,
        before focus returns."""
        for flag in self.FLAGS:
            with self.subTest(flag=flag):
                # before=1, after the action 0 (opening read it), then 1 again.
                code, out, err, calls = self.run_action(flag, unread=(1, 0, 1))
                self.assertEqual(code, 0)
                self.assertEqual(err, "")
                self.assertIn("click:Mark as Unread", calls)
                self.assertLess(calls.index("click:Mark as Unread"), calls.index("restore:Previous app"))

    def test_a_read_conversation_is_left_read(self):
        code, _, _, calls = self.run_action("--mute", unread=(0,))
        self.assertEqual(code, 0)
        self.assertNotIn("click:Mark as Unread", calls)

    def test_an_unread_that_survived_opening_is_not_touched(self):
        code, _, _, calls = self.run_action("--pin", unread=(2, 2))
        self.assertEqual(code, 0)
        self.assertNotIn("click:Mark as Unread", calls)

    def test_a_lost_unread_is_reported_not_hidden(self):
        code, out, err, calls = self.run_action("--pin", unread=(1, 0, 0),
                                                clicks=((True, ""), (True, "")))
        self.assertEqual(code, 0)          # the pin itself landed and was verified
        self.assertEqual(out.strip(), "pinned")
        self.assertIn("unread state", err)

    def test_unread_is_restored_even_when_the_action_fails(self):
        code, _, _, calls = self.run_action("--unpin", probe=True, unread=(1, 0, 1))
        self.assertEqual(code, 75)
        self.assertIn("click:Mark as Unread", calls)


class ActionProbes(unittest.TestCase):
    """The verifiers read Messages' own records with synthetic fixtures."""

    SCHEMA = ClusterScopedUnread.SCHEMA.replace(
        "last_read_message_timestamp INTEGER DEFAULT 0",
        "last_read_message_timestamp INTEGER DEFAULT 0,\n          properties BLOB")

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.db = str(root / "chat.db")
        self.pins = root / "com.apple.messages.pinning.plist"
        con = sqlite3.connect(self.db)
        con.executescript(self.SCHEMA)
        # A merged DM: phone row and email row, one group_id.
        con.executescript("""
            INSERT INTO chat (ROWID, chat_identifier, guid, display_name, style, group_id)
              VALUES (1, '+15550100001', 'SMS;-;+15550100001', '', 45, 'MERGED-DM-1');
            INSERT INTO chat (ROWID, chat_identifier, guid, display_name, style, group_id)
              VALUES (2, 'pat@example.com', 'iMessage;-;pat@example.com', '', 45, 'MERGED-DM-1');
            INSERT INTO chat (ROWID, chat_identifier, guid, display_name, style, group_id)
              VALUES (3, '+15550100002', 'iMessage;-;+15550100002', '', 45, 'DM-TWO');
            INSERT INTO handle VALUES (1, '+15550100001');
            INSERT INTO handle VALUES (2, 'pat@example.com');
            INSERT INTO handle VALUES (3, '+15550100002');
            INSERT INTO chat_handle_join VALUES (1, 1);
            INSERT INTO chat_handle_join VALUES (2, 2);
            INSERT INTO chat_handle_join VALUES (3, 3);
        """)
        con.commit()
        con.close()
        mod = read.imsg_module()
        mod._COLS = None
        for target, name, value in ((read, "DB_PATH", self.db),
                                    (mod, "PINNING_PLIST_PATHS", (str(self.pins),))):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def write_pins(self, data):
        with open(self.pins, "wb") as fh:
            plistlib.dump(data, fh)

    def set_props(self, rowid, blob):
        con = sqlite3.connect(self.db)
        con.execute("UPDATE chat SET properties = ? WHERE ROWID = ?", (blob, rowid))
        con.commit()
        con.close()

    def test_unreadable_pin_list_is_unknown_not_unpinned(self):
        self.assertIsNone(read.chat_pinned("+15550100001"))      # no file at all
        self.pins.write_bytes(b"not a plist")
        self.assertIsNone(read.chat_pinned("+15550100001"))
        self.write_pins({"something": ["+15550100001"]})          # unknown layout
        self.assertIsNone(read.chat_pinned("+15550100001"))

    def test_pin_stored_under_the_other_handle_counts(self):
        self.write_pins({"pD": {"pP": ["pat@example.com"]}})
        self.assertIs(read.chat_pinned("+15550100001"), True)
        self.assertIs(read.chat_pinned("+15550100002"), False)

    def test_an_empty_pin_list_is_a_verified_unpin(self):
        self.write_pins({"pD": {"pP": [], "pZ": {"x": {"o": "+15550100001"}}}})
        self.assertIs(read.chat_pinned("+15550100001"), False)

    def test_unreadable_database_is_unknown_for_pins(self):
        self.write_pins({"pD": {"pP": ["+15550100002"]}})
        with patch.object(read, "DB_PATH", str(Path(self.folder.name) / "missing" / "chat.db")):
            self.assertIsNone(read.chat_pinned("+15550100001"))

    def test_mute_flag_on_either_row_mutes_the_conversation(self):
        self.assertIs(read.chat_muted("+15550100001"), False)    # never configured
        self.set_props(2, plistlib.dumps({"ignoreAlertsFlag": True}, fmt=plistlib.FMT_BINARY))
        self.assertIs(read.chat_muted("+15550100001"), True)
        self.assertIs(read.chat_muted("pat@example.com"), True)
        self.assertIs(read.chat_muted("+15550100002"), False)
        self.set_props(2, plistlib.dumps({"ignoreAlertsFlag": False}, fmt=plistlib.FMT_BINARY))
        self.assertIs(read.chat_muted("+15550100001"), False)

    def test_malformed_or_missing_mute_state_is_unknown(self):
        self.set_props(1, b"garbage")
        self.assertIsNone(read.chat_muted("+15550100001"))
        self.set_props(1, plistlib.dumps(["not", "a", "dict"]))
        self.assertIsNone(read.chat_muted("+15550100001"))
        self.assertIsNone(read.chat_muted("+15550199999"))      # not on this Mac
        with patch.object(read, "DB_PATH", str(Path(self.folder.name) / "missing" / "chat.db")):
            self.assertIsNone(read.chat_muted("+15550100002"))

    def test_chat_list_and_write_agree_on_a_muted_alias(self):
        """`imsg chats` reports muted over the same cluster the verifier checks."""
        self.set_props(2, plistlib.dumps({"ignoreAlertsFlag": True}))
        mod = read.imsg_module()
        con = read.open_db()
        try:
            clusters = mod._group_cluster_map(con)
            muted = mod.muted_conversations(con, clusters)
        finally:
            con.close()
        canon = clusters.get("+15550100001", "+15550100001")
        self.assertIn(canon, muted)
        self.assertNotIn("+15550100002", muted)


if __name__ == "__main__":
    unittest.main()
