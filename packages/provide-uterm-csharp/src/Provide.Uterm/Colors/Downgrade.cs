//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Colors;

public static class Downgrade
{
    public static string DowngradeTo256(string text) =>
        Sgr.SgrRegexp.Replace(text, m => Sgr.RewriteParams(m.Groups[1].Value, ColorMode.Mode256));

    public static string DowngradeTo16(string text) =>
        Sgr.SgrRegexp.Replace(text, m => Sgr.RewriteParams(m.Groups[1].Value, ColorMode.Mode16));
}
