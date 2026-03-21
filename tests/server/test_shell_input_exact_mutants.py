# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Mutation-killing tests for shell connector — exact handle_input assertions."""

from __future__ import annotations

from typing import Any

import pytest

from provide.terminal.server.connectors.shell import ShellSessionConnector


def make(config: dict[str, Any] | None = None) -> ShellSessionConnector:
    return ShellSessionConnector("sess1", "Test Shell", config)


class TestHandleInputExact:
    @pytest.mark.asyncio
    async def test_turns_increments_by_one_exact(self):
        """Kills mutmut_8 (=1), mutmut_9 (-=1), mutmut_10 (+=2)."""
        c = make()
        await c.handle_input("first")
        assert c._turns == 1
        await c.handle_input("second")
        assert c._turns == 2
        await c.handle_input("third")
        assert c._turns == 3

    @pytest.mark.asyncio
    async def test_empty_input_banner_exact(self):
        """Kills mutmut_5: 'XXEmpty input ignored.XX'."""
        c = make()
        await c.handle_input("")
        assert c._banner == "Empty input ignored."
        assert c._banner != "XXEmpty input ignored.XX"

    @pytest.mark.asyncio
    async def test_last_command_set_not_none(self):
        """Kills mutmut_18: _last_command = None instead of command."""
        c = make()
        await c.handle_input("/help")
        assert c._last_command == "/help"
        assert c._last_command is not None

    @pytest.mark.asyncio
    async def test_help_command_case_sensitive(self):
        """Kills mutmut_20 (XX/helpXX) and mutmut_21 (/HELP)."""
        c = make()
        await c.handle_input("/help")
        assert c._banner == "Command help printed below."
        # /HELP should not trigger help
        c2 = make()
        await c2.handle_input("/HELP")
        # Should go to unknown command path
        assert "Unknown command" in c2._banner

    @pytest.mark.asyncio
    async def test_help_banner_exact(self):
        """Kills mutmut_22 (None), mutmut_23 (XX...XX), mutmut_24 (lowercase), mutmut_25 (UPPER)."""
        c = make()
        await c.handle_input("/help")
        assert c._banner == "Command help printed below."
        assert c._banner is not None
        assert c._banner != "XXCommand help printed below.XX"
        assert c._banner != "command help printed below."
        assert c._banner != "COMMAND HELP PRINTED BELOW."

    @pytest.mark.asyncio
    async def test_help_appends_system_speaker_exact(self):
        """Kills mutmut_30 (XXsystemXX) and mutmut_31 (SYSTEM)."""
        c = make()
        pre_len = len(c._transcript)
        await c.handle_input("/help")
        entries = list(c._transcript)
        new_entries = entries[pre_len:]
        speakers = [e.speaker for e in new_entries]
        assert "system" in speakers
        assert "XXsystemXX" not in speakers
        assert "SYSTEM" not in speakers

    @pytest.mark.asyncio
    async def test_help_appends_text_exact(self):
        """Kills mutmut_27 (None), mutmut_32 (XX...XX), mutmut_33 (lowercase), mutmut_34 (UPPER)."""
        c = make()
        pre_len = len(c._transcript)
        await c.handle_input("/help")
        entries = list(c._transcript)
        new_entries = entries[pre_len:]
        texts = [e.text for e in new_entries]
        expected = "Commands: /help /clear /mode open|hijack /status /nick <name> /say <text> /shell /reset"
        assert expected in texts
        assert None not in texts
        assert not any("XX" in (t or "") for t in texts)

    @pytest.mark.asyncio
    async def test_clear_command_case_sensitive(self):
        """Kills mutmut_36 (XX/clearXX) and mutmut_37 (/CLEAR)."""
        c = make()
        await c.handle_input("/clear")
        assert c._banner == "Transcript cleared."
        # /CLEAR should not work as clear command
        c2 = make()
        await c2.handle_input("/CLEAR")
        assert "Unknown command" in c2._banner

    @pytest.mark.asyncio
    async def test_clear_maxlen_is_10_not_none_not_11(self):
        """Kills mutmut_39 (maxlen=None) and mutmut_40 (maxlen=11)."""
        c = make()
        await c.handle_input("/clear")
        assert c._transcript.maxlen == 10
        assert c._transcript.maxlen is not None
        assert c._transcript.maxlen != 11

    @pytest.mark.asyncio
    async def test_clear_banner_exact(self):
        """Kills mutmut_41 (None), mutmut_42 (XX...XX), mutmut_43 (lowercase), mutmut_44 (UPPER)."""
        c = make()
        await c.handle_input("/clear")
        assert c._banner == "Transcript cleared."
        assert c._banner is not None
        assert c._banner != "XXTranscript cleared.XX"
        assert c._banner != "transcript cleared."
        assert c._banner != "TRANSCRIPT CLEARED."

    @pytest.mark.asyncio
    async def test_mode_invalid_banner_exact(self):
        """Kills mutmut_55 (None), mutmut_56 (XX...XX), mutmut_57 (lowercase), mutmut_58 (UPPER)."""
        c = make()
        await c.handle_input("/mode invalid")
        assert c._banner == "Usage: /mode open|hijack"
        assert c._banner is not None
        assert c._banner != "usage: /mode open|hijack"
        assert c._banner != "USAGE: /MODE OPEN|HIJACK"
        assert c._banner != "XXUsage: /mode open|hijackXX"

    @pytest.mark.asyncio
    async def test_mode_invalid_appends_system_text_exact(self):
        """Kills mutmut_60 (None), mutmut_63 (XXsystemXX), mutmut_64 (SYSTEM), mutmut_65/66 (text case)."""
        c = make()
        pre_len = len(c._transcript)
        await c.handle_input("/mode invalid")
        entries = list(c._transcript)
        new_entries = entries[pre_len:]
        assert len(new_entries) >= 1
        speakers = [e.speaker for e in new_entries]
        texts = [e.text for e in new_entries]
        assert "system" in speakers
        assert "XXsystemXX" not in speakers
        assert "SYSTEM" not in speakers
        assert "usage: /mode open|hijack" in texts
        assert None not in texts

    @pytest.mark.asyncio
    async def test_status_banner_exact(self):
        """Kills mutmut_71 (None), mutmut_72 (XX...XX), mutmut_73 (lowercase), mutmut_74 (UPPER)."""
        c = make()
        await c.handle_input("/status")
        assert c._banner == "Session status printed below."
        assert c._banner is not None
        assert c._banner != "XXSession status printed below.XX"
        assert c._banner != "session status printed below."
        assert c._banner != "SESSION STATUS PRINTED BELOW."

    @pytest.mark.asyncio
    async def test_status_appends_system_speaker_exact(self):
        """Kills mutmut_79 (XXsystemXX) and mutmut_80 (SYSTEM)."""
        c = make()
        pre_len = len(c._transcript)
        await c.handle_input("/status")
        entries = list(c._transcript)
        new_entries = entries[pre_len:]
        speakers = [e.speaker for e in new_entries]
        assert "system" in speakers
        assert "XXsystemXX" not in speakers
        assert "SYSTEM" not in speakers

    @pytest.mark.asyncio
    async def test_nick_no_arg_banner_exact(self):
        """Kills mutmut_85 (None), mutmut_86 (XX...XX), mutmut_87 (lowercase), mutmut_88 (UPPER)."""
        c = make()
        await c.handle_input("/nick")
        assert c._banner == "Usage: /nick <name>"
        assert c._banner is not None
        assert c._banner != "usage: /nick <name>"
        assert c._banner != "USAGE: /NICK <NAME>"

    @pytest.mark.asyncio
    async def test_nick_no_arg_appends_system_text_exact(self):
        """Kills mutmut_90 (None text), mutmut_93 (XX..XX speaker), mutmut_94/95/96 (case)."""
        c = make()
        pre_len = len(c._transcript)
        await c.handle_input("/nick")
        entries = list(c._transcript)
        new_entries = entries[pre_len:]
        speakers = [e.speaker for e in new_entries]
        texts = [e.text for e in new_entries]
        assert "system" in speakers
        assert "usage: /nick <name>" in texts
        assert None not in texts

    @pytest.mark.asyncio
    async def test_nick_truncates_at_24_not_25(self):
        """Kills mutmut_98: arg[:24] → no slice or different slice."""
        c = make()
        await c.handle_input("/nick " + "x" * 30)
        assert len(c._nickname) == 24
        assert c._nickname == "x" * 24

    @pytest.mark.asyncio
    async def test_nick_banner_uses_nickname(self):
        """Kills mutmut_99: _banner = None."""
        c = make()
        await c.handle_input("/nick alice")
        assert c._banner is not None
        assert "alice" in c._banner
        assert c._banner == "Nickname set to alice."

    @pytest.mark.asyncio
    async def test_nick_appends_system_with_nickname(self):
        """Kills mutmut_101 (None), mutmut_104 (XX..XX speaker), mutmut_105 (SYSTEM)."""
        c = make()
        pre_len = len(c._transcript)
        await c.handle_input("/nick bob")
        entries = list(c._transcript)
        new_entries = entries[pre_len:]
        speakers = [e.speaker for e in new_entries]
        texts = [e.text for e in new_entries]
        assert "system" in speakers
        assert "nickname: bob" in texts

    @pytest.mark.asyncio
    async def test_say_no_arg_banner_exact(self):
        """Kills mutmut_110 (None), mutmut_111/112/113 (case)."""
        c = make()
        await c.handle_input("/say")
        assert c._banner == "Usage: /say <text>"
        assert c._banner is not None
        assert c._banner != "usage: /say <text>"
        assert c._banner != "USAGE: /SAY <TEXT>"

    @pytest.mark.asyncio
    async def test_say_no_arg_appends_system_exact(self):
        """Kills mutmut_115 (None text), mutmut_118/119/120/121 (case/XX variants)."""
        c = make()
        pre_len = len(c._transcript)
        await c.handle_input("/say")
        entries = list(c._transcript)
        new_entries = entries[pre_len:]
        speakers = [e.speaker for e in new_entries]
        texts = [e.text for e in new_entries]
        assert "system" in speakers
        assert "usage: /say <text>" in texts
        assert None not in texts

    @pytest.mark.asyncio
    async def test_say_banner_exact(self):
        """Kills mutmut_122 (None), mutmut_123/124/125 (case/XX variants)."""
        c = make()
        await c.handle_input("/say hello world")
        assert c._banner == "Message appended."
        assert c._banner is not None
        assert c._banner != "XXMessage appended.XX"

    @pytest.mark.asyncio
    async def test_say_appends_user_with_nickname(self):
        """Kills mutmut_130 (None text), mutmut_131 (XX..XX speaker)."""
        c = make()
        c._nickname = "myuser"
        pre_len = len(c._transcript)
        await c.handle_input("/say greetings")
        entries = list(c._transcript)
        new_entries = entries[pre_len:]
        speakers = [e.speaker for e in new_entries]
        texts = [e.text for e in new_entries]
        assert "user" in speakers
        assert "myuser: greetings" in texts

    @pytest.mark.asyncio
    async def test_shell_command_case_sensitive(self):
        """Kills mutmut_133 (XX/shellXX) and mutmut_134 (/SHELL)."""
        c = make()
        await c.handle_input("/shell")
        assert c._banner == "Shell response appended."
        # /SHELL should not work
        c2 = make()
        await c2.handle_input("/SHELL")
        assert "Unknown command" in c2._banner

    @pytest.mark.asyncio
    async def test_shell_banner_exact(self):
        """Kills mutmut_135 (None), mutmut_136/137/138 (case/XX variants)."""
        c = make()
        await c.handle_input("/shell")
        assert c._banner == "Shell response appended."
        assert c._banner is not None
        assert c._banner != "shell response appended."
        assert c._banner != "SHELL RESPONSE APPENDED."

    @pytest.mark.asyncio
    async def test_shell_appends_session_exact(self):
        """Kills mutmut_140 (None text), mutmut_143/144/145/146/147 (case/speaker variants)."""
        c = make()
        pre_len = len(c._transcript)
        await c.handle_input("/shell")
        entries = list(c._transcript)
        new_entries = entries[pre_len:]
        speakers = [e.speaker for e in new_entries]
        texts = [e.text for e in new_entries]
        assert "session" in speakers
        assert "This hosted server is the reference implementation." in texts
        assert None not in texts

    @pytest.mark.asyncio
    async def test_reset_banner_exact(self):
        """Kills mutmut_151 (None), mutmut_152/153/154 (case/XX variants)."""
        c = make()
        await c.handle_input("/reset")
        assert c._banner == "Session reset."
        assert c._banner is not None
        assert c._banner != "session reset."
        assert c._banner != "SESSION RESET."

    @pytest.mark.asyncio
    async def test_unknown_command_banner_uses_command_name(self):
        """Kills mutmut_155: _banner = f'Unknown command: {command}'."""
        c = make()
        await c.handle_input("/foobar")
        assert c._banner == "Unknown command: /foobar"

    @pytest.mark.asyncio
    async def test_unknown_command_appends_system_exact(self):
        """Kills mutmut_157 (None), mutmut_160 (XX..XX speaker), mutmut_161 (SYSTEM)."""
        c = make()
        pre_len = len(c._transcript)
        await c.handle_input("/foobar")
        entries = list(c._transcript)
        new_entries = entries[pre_len:]
        speakers = [e.speaker for e in new_entries]
        texts = [e.text for e in new_entries]
        assert "system" in speakers
        assert "unknown command: /foobar" in texts

    @pytest.mark.asyncio
    async def test_plain_input_banner_exact(self):
        """Kills mutmut_162 (None), mutmut_163/164/165 (case/XX variants)."""
        c = make()
        await c.handle_input("hello world")
        assert c._banner == "Input accepted."
        assert c._banner is not None
        assert c._banner != "input accepted."
        assert c._banner != "INPUT ACCEPTED."

    @pytest.mark.asyncio
    async def test_plain_input_appends_user_with_nickname_exact(self):
        """Kills mutmut_167 (None text), mutmut_170 (XX..XX speaker), mutmut_171 (USER)."""
        c = make()
        c._nickname = "tester"
        pre_len = len(c._transcript)
        await c.handle_input("hello")
        entries = list(c._transcript)
        new_entries = entries[pre_len:]
        speakers = [e.speaker for e in new_entries]
        texts = [e.text for e in new_entries]
        assert "user" in speakers
        assert "tester: hello" in texts
        assert None not in texts

    @pytest.mark.asyncio
    async def test_plain_input_appends_session_exact(self):
        """Kills mutmut_173 (None text), mutmut_176 (XX..XX speaker), mutmut_177 (SESSION)."""
        c = make()
        pre_len = len(c._transcript)
        await c.handle_input("myinput")
        entries = list(c._transcript)
        new_entries = entries[pre_len:]
        speakers = [e.speaker for e in new_entries]
        texts = [e.text for e in new_entries]
        assert "session" in speakers
        assert 'session: received "myinput"' in texts
        assert None not in texts


# ---------------------------------------------------------------------------
# handle_control – exact banner, speaker, text checks
# ---------------------------------------------------------------------------
