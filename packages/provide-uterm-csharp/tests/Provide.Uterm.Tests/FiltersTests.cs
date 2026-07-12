//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Filters;

namespace Provide.Uterm.Tests;

public class FiltersTests
{
    [Fact]
    public void ConsumeIac_WillOption()
    {
        // After IAC already consumed: WILL + option
        var data = new byte[] { InputFilters.Will, 31, (byte)'A' };
        using var ms = new MemoryStream(data);
        InputFilters.ConsumeIac(ms);
        Assert.Equal((byte)'A', ms.ReadByte());
    }

    [Fact]
    public void ConsumeIac_Subnegotiation()
    {
        var data = new byte[] { InputFilters.Sb, 24, 0, (byte)'x', InputFilters.Iac, InputFilters.Se, (byte)'Z' };
        using var ms = new MemoryStream(data);
        InputFilters.ConsumeIac(ms);
        Assert.Equal((byte)'Z', ms.ReadByte());
    }

    [Fact]
    public void ConsumeEscape_Csi()
    {
        // After ESC: [ A
        var data = new byte[] { (byte)'[', (byte)'A', (byte)'X' };
        using var ms = new MemoryStream(data);
        InputFilters.ConsumeEscape(ms);
        Assert.Equal((byte)'X', ms.ReadByte());
    }

    [Fact]
    public void ConsumeEscape_Ss3()
    {
        var data = new byte[] { (byte)'O', (byte)'P', (byte)'Y' };
        using var ms = new MemoryStream(data);
        InputFilters.ConsumeEscape(ms);
        Assert.Equal((byte)'Y', ms.ReadByte());
    }
}
