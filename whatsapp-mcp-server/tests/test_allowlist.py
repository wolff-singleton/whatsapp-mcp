"""Tests for the WHATSAPP_ALLOWED_NUMBERS read/send allowlist.

The allowlist restricts a given MCP server instance to a fixed set of DM
contacts. It is enforced in whatsapp.py (the data layer) so internal callers
are covered too. Empty/unset = full access (backwards compatible).

WHATSMEOW_DB_PATH is pointed at a nonexistent file so _sender_aliases uses its
deterministic fallback (bare / @s.whatsapp.net / @lid) instead of a real LID
map, keeping these tests hermetic.
"""

import sqlite3

import pytest

import main as mcp_main
import whatsapp

ALLOWED = "61412345678@s.whatsapp.net"
BLOCKED = "61400000000@s.whatsapp.net"
GROUP = "120363000000@g.us"


def _make_db(path):
    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    cursor.executescript(
        """
        CREATE TABLE chats (
            jid TEXT PRIMARY KEY,
            name TEXT,
            last_message_time TIMESTAMP
        );
        CREATE TABLE messages (
            id TEXT,
            chat_jid TEXT,
            sender TEXT,
            content TEXT,
            timestamp TIMESTAMP,
            is_from_me BOOLEAN,
            media_type TEXT,
            filename TEXT,
            quoted_message_id TEXT,
            PRIMARY KEY (id, chat_jid),
            FOREIGN KEY (chat_jid) REFERENCES chats(jid)
        );
        """
    )
    chats = [
        (ALLOWED, "Allowed", "2024-01-15 10:30:00+00:00"),
        (BLOCKED, "Blocked", "2024-01-15 11:30:00+00:00"),
        (GROUP, "Group", "2024-01-15 12:30:00+00:00"),
    ]
    cursor.executemany("INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)", chats)
    messages = [
        ("m_allowed", ALLOWED, "61412345678", "hi from allowed", "2024-01-15 10:30:00+00:00", 0),
        ("m_blocked", BLOCKED, "61400000000", "hi from blocked", "2024-01-15 11:30:00+00:00", 0),
        # The allowed contact also posts in a group — the group must still be
        # excluded, since the allowlist holds individual numbers only.
        ("m_group", GROUP, "61412345678", "hi in group", "2024-01-15 12:30:00+00:00", 0),
    ]
    cursor.executemany(
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?, ?, ?, ?, ?, ?)",
        messages,
    )
    conn.commit()
    conn.close()


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "messages.db"
    _make_db(str(db_path))
    monkeypatch.setattr(whatsapp, "MESSAGES_DB_PATH", str(db_path))
    monkeypatch.setattr(whatsapp, "WHATSMEOW_DB_PATH", str(tmp_path / "no-whatsmeow.db"))
    return db_path


@pytest.fixture
def allowlist(monkeypatch):
    """Restrict to the single 'allowed' contact."""
    monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "61412345678")


# --- helpers -------------------------------------------------------------


class TestHelpers:
    def test_normalise_strips_punctuation_and_suffix(self):
        assert whatsapp._normalise_msisdn("+61 412 345 678") == "61412345678"
        assert whatsapp._normalise_msisdn("61412345678@s.whatsapp.net") == "61412345678"
        assert whatsapp._normalise_msisdn("(61) 412-345-678") == "61412345678"

    def test_load_allowed_numbers_unset_is_none(self, monkeypatch):
        monkeypatch.delenv("WHATSAPP_ALLOWED_NUMBERS", raising=False)
        assert whatsapp._load_allowed_numbers() is None

    def test_load_allowed_numbers_blank_is_none(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "   ")
        assert whatsapp._load_allowed_numbers() is None

    def test_load_allowed_numbers_parses_and_normalises(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "+61 412 345 678, 61400000000")
        assert whatsapp._load_allowed_numbers() == {"61412345678", "61400000000"}

    def test_is_allowed_fail_open_when_unset(self, monkeypatch):
        monkeypatch.delenv("WHATSAPP_ALLOWED_NUMBERS", raising=False)
        assert whatsapp.is_allowed(BLOCKED) is True
        assert whatsapp.is_allowed(GROUP) is True
        assert whatsapp.is_allowed(None) is True

    def test_is_allowed_matches_full_and_bare_forms(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "+61 412 345 678")
        monkeypatch.setattr(whatsapp, "WHATSMEOW_DB_PATH", "/nonexistent.db")
        assert whatsapp.is_allowed(ALLOWED) is True
        assert whatsapp.is_allowed("61412345678") is True
        assert whatsapp.is_allowed("61412345678@lid") is True
        assert whatsapp.is_allowed(BLOCKED) is False
        assert whatsapp.is_allowed(GROUP) is False


# --- read filtering ------------------------------------------------------


class TestReadFiltering:
    def test_list_chats_unrestricted_returns_all(self, db, monkeypatch):
        monkeypatch.delenv("WHATSAPP_ALLOWED_NUMBERS", raising=False)
        jids = {c["jid"] for c in whatsapp.list_chats(limit=10)}
        assert jids == {ALLOWED, BLOCKED, GROUP}

    def test_list_chats_restricted_to_allowed_dm(self, db, allowlist):
        chats = whatsapp.list_chats(limit=10)
        assert [c["jid"] for c in chats] == [ALLOWED]

    def test_list_messages_unrestricted_returns_all(self, db, monkeypatch):
        monkeypatch.delenv("WHATSAPP_ALLOWED_NUMBERS", raising=False)
        jids = {m["chat_jid"] for m in whatsapp.list_messages(limit=50, include_context=False)}
        assert jids == {ALLOWED, BLOCKED, GROUP}

    def test_list_messages_restricted_to_allowed_dm(self, db, allowlist):
        msgs = whatsapp.list_messages(limit=50, include_context=False)
        assert {m["chat_jid"] for m in msgs} == {ALLOWED}

    def test_get_chat_allowed_and_blocked(self, db, allowlist):
        assert whatsapp.get_chat(ALLOWED) is not None
        assert whatsapp.get_chat(BLOCKED) is None
        assert whatsapp.get_chat(GROUP) is None

    def test_get_direct_chat_by_contact(self, db, allowlist):
        assert whatsapp.get_direct_chat_by_contact("61412345678") is not None
        assert whatsapp.get_direct_chat_by_contact("61400000000") is None

    def test_get_last_interaction(self, db, allowlist):
        assert whatsapp.get_last_interaction("61412345678") is not None
        assert whatsapp.get_last_interaction("61400000000") is None

    def test_get_message_context_denies_blocked(self, db, allowlist):
        ctx = whatsapp.get_message_context("m_allowed")
        assert ctx.message.id == "m_allowed"
        with pytest.raises(ValueError, match="not found"):
            whatsapp.get_message_context("m_blocked")

    def test_search_contacts_filtered(self, db, allowlist):
        # "614" matches both DM JIDs in SQL; the allowlist drops the blocked one.
        jids = {c["jid"] for c in whatsapp.search_contacts("614")}
        assert jids == {ALLOWED}

    def test_get_contact_chats_excludes_group_and_blocked(self, db, allowlist):
        chats = whatsapp.get_contact_chats("61412345678")
        assert [c["jid"] for c in chats] == [ALLOWED]
        assert whatsapp.get_contact_chats("61400000000") == []


# --- send / download -----------------------------------------------------


class _OkResponse:
    status_code = 200

    def json(self):
        return {"success": True, "message": "sent", "path": "/tmp/x.jpg"}

    text = "OK"


class TestSendGuard:
    def test_send_message_blocked_recipient_short_circuits(self, db, allowlist, monkeypatch):
        calls = []
        monkeypatch.setattr(whatsapp.requests, "post", lambda *a, **k: calls.append(1) or _OkResponse())

        ok, msg = whatsapp.send_message("61400000000", "hi")
        assert ok is False
        assert "allowed contacts" in msg
        assert calls == []  # never reached the bridge

    def test_send_message_allowed_recipient_passes(self, db, allowlist, monkeypatch):
        monkeypatch.setattr(whatsapp.requests, "post", lambda *a, **k: _OkResponse())
        ok, _ = whatsapp.send_message("61412345678", "hi")
        assert ok is True

    def test_send_file_blocked_recipient(self, db, allowlist, tmp_path, monkeypatch):
        f = tmp_path / "x.jpg"
        f.write_bytes(b"x")
        monkeypatch.setattr(whatsapp.requests, "post", lambda *a, **k: _OkResponse())
        ok, msg = whatsapp.send_file("61400000000", str(f))
        assert ok is False
        assert "allowed contacts" in msg

    def test_send_audio_blocked_recipient(self, db, allowlist, tmp_path, monkeypatch):
        f = tmp_path / "x.ogg"
        f.write_bytes(b"x")
        monkeypatch.setattr(whatsapp.requests, "post", lambda *a, **k: _OkResponse())
        ok, msg = whatsapp.send_audio_message("61400000000", str(f))
        assert ok is False
        assert "allowed contacts" in msg

    def test_download_media_blocked_chat(self, db, allowlist, monkeypatch):
        calls = []
        monkeypatch.setattr(whatsapp.requests, "post", lambda *a, **k: calls.append(1) or _OkResponse())
        assert whatsapp.download_media("m_blocked", BLOCKED) is None
        assert calls == []

    def test_download_media_allowed_chat(self, db, allowlist, monkeypatch):
        monkeypatch.setattr(whatsapp.requests, "post", lambda *a, **k: _OkResponse())
        assert whatsapp.download_media("m_allowed", ALLOWED) == "/tmp/x.jpg"


class TestGetContactGuard:
    def test_blocked_identifier_returns_error_without_lookup(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "61412345678")
        monkeypatch.setattr(whatsapp, "WHATSMEOW_DB_PATH", "/nonexistent.db")

        def boom(*a, **k):
            raise AssertionError("get_chat must not be called for a blocked contact")

        monkeypatch.setattr(mcp_main, "whatsapp_get_chat", boom)

        result = mcp_main.get_contact(identifier="61400000000")
        assert result["resolved"] is False
        assert result["jid"] is None
        assert "allowed contacts" in result["error"]

    def test_allowed_identifier_resolves_normally(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "61412345678")
        monkeypatch.setattr(whatsapp, "WHATSMEOW_DB_PATH", "/nonexistent.db")
        monkeypatch.setattr(
            mcp_main, "whatsapp_get_chat", lambda jid, include_last_message=True: {"jid": jid, "name": "Allowed"}
        )
        monkeypatch.setattr(mcp_main, "whatsapp_get_sender_name", lambda jid: jid)

        result = mcp_main.get_contact(identifier="61412345678")
        assert "error" not in result
        assert result["jid"] == ALLOWED
        assert result["name"] == "Allowed"
