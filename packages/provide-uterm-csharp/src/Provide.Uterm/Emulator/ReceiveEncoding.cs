//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Emulator;

/// <summary>
/// The codec an emulator decodes the bytes it is fed with: the
/// <c>receive_encoding</c> of the Python TerminalEmulator. The default is CP437,
/// which is the reference's.
/// </summary>
public enum ReceiveEncoding
{
    /// <summary>
    /// Maps every byte to a character: what a BBS draws its interface in, and
    /// what the telnet, SSH and websocket connectors decode, as in the reference.
    /// </summary>
    Cp437 = 0,

    /// <summary>
    /// Decoded across reads, so a character split between two arrives whole, and
    /// invalid bytes become one U+FFFD per maximal subpart. A UTF-8 session also
    /// honours DEC special graphics, as the reference's does. It is what a local
    /// program writes: the shell connector.
    /// </summary>
    Utf8 = 1,
}
