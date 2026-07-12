//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Vt;

/// <summary>Terminal mode switches (pyte.modes). DEC private modes are shifted left by 5.</summary>
public static class ModeCodes
{
    public const int Lnm = 20;
    public const int Irm = 4;
    public const int Dectcem = 25 << 5;
    public const int Decscnm = 5 << 5;
    public const int Decom = 6 << 5;
    public const int Decawm = 7 << 5;
    public const int Deccolm = 3 << 5;
}
