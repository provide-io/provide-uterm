//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Hub;
using Provide.Uterm.ServerConfig;
using Xunit;

namespace Provide.Uterm.Tests;

/// <summary>
/// The browser ceiling an operator may write in server.toml,
/// <c>browser_rate_limit_per_sec</c> (default 300) — the per-connection budget
/// for inbound browser WebSocket messages.
///
/// It shares its validator with the two REST keys because it shares a
/// <see cref="TokenBucket"/>: same burst-equals-rate rule, so the same floor
/// follows. The reference joined them for that reason
/// (<c>config_schema._validate_rate_limit</c>), and it named the browser key the
/// most dangerous of the three: <c>RateLimiter.__init__</c> clamps the REST
/// rates, but the browser rate reaches <c>TokenBucket</c> unclamped, so a
/// configured <c>0</c> denied every browser message for the life of the process.
/// An operator could brick their own deployment with a value that reads like "no
/// limit", and nothing would say so — not at startup, not in a log, only in a
/// terminal nobody can type into.
///
/// Two defects are pinned here, and they compound:
///
/// <em>No validation at all.</em> The key had no floor, so every value the REST
/// siblings refuse — <c>0</c>, negatives, the whole <c>(0, 1)</c> band, and the
/// non-finite three — was accepted verbatim.
///
/// <em>Silently dropped.</em> The loader read the key with an <c>is double</c>
/// type test, so TOML's <c>browser_rate_limit_per_sec = 300</c> parses as a
/// <c>long</c>, fails the test, and is discarded without a word: the operator's
/// configured value vanishes and the default applies. A rate that failed to
/// reach the config is indistinguishable from one never written, which is the
/// exact silent-loosening the validation exists to prevent — so validating
/// without also fixing the read would have left the guard unreachable from a
/// TOML file for every integer an operator is most likely to write.
/// </summary>
public sealed class ServerConfigBrowserRateLimitTests
{
    private const string Key = "browser_rate_limit_per_sec";

    private static string WriteToml(string body)
    {
        var path = Path.Combine(Path.GetTempPath(), "uterm-browser-rl-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(path, body);
        return path;
    }

    private static UtermServerConfig LoadToml(string body)
    {
        var path = WriteToml(body);
        try
        {
            return ConfigLoader.Load(path);
        }
        finally
        {
            File.Delete(path);
        }
    }

    // -- The default -------------------------------------------------------

    /// <summary>
    /// A deployment that never wrote the key behaves exactly as it did before
    /// the key was validated: the reference's 300.
    /// </summary>
    [Fact]
    public void An_Absent_Key_Keeps_The_Reference_Default()
    {
        Assert.Equal(300, UtermServerConfig.Default().BrowserRateLimitPerSec);
        Assert.Equal(300, ConfigLoader.Load(null).BrowserRateLimitPerSec);
        Assert.Equal(300, LoadToml("""
            [server]
            host = "127.0.0.1"
            """).BrowserRateLimitPerSec);
    }

    // -- Reading the key ---------------------------------------------------

    /// <summary>
    /// A bare TOML integer is a rate. This is the whole of the second defect:
    /// <c>42</c> decodes to a <c>long</c>, and the loader's <c>is double</c>
    /// test dropped it on the floor. The value is deliberately not 300, because
    /// a test written against the default cannot tell "read correctly" from
    /// "discarded and defaulted".
    /// </summary>
    [Fact]
    public void A_Toml_Integer_Is_A_Rate()
    {
        Assert.Equal(42, LoadToml($"{Key} = 42\n").BrowserRateLimitPerSec);
    }

    /// <summary>A float is a rate, and was the only accepted spelling before.</summary>
    [Fact]
    public void A_Toml_Float_Is_A_Rate()
    {
        Assert.Equal(12.5, LoadToml($"{Key} = 12.5\n").BrowserRateLimitPerSec);
    }

    /// <summary>
    /// A numeric string is still a number, as it is for the reference's
    /// (lax-mode) float field and for the REST keys here.
    /// </summary>
    [Fact]
    public void A_Numeric_String_Is_Still_A_Rate()
    {
        Assert.Equal(2.5, LoadToml($"{Key} = \"2.5\"\n").BrowserRateLimitPerSec);
    }

    /// <summary>
    /// A value present but unparseable is refused by name, not folded to the
    /// default — the operator wrote something, and being told it was wrong is
    /// the only way they learn it never took effect.
    /// </summary>
    [Theory]
    [InlineData("\"fast\"")]
    [InlineData("true")]
    public void A_Present_But_Unparseable_Rate_Is_Refused_By_Name(string literal)
    {
        var ex = Assert.Throws<ArgumentException>(() => LoadToml($"{Key} = {literal}\n"));
        Assert.Contains(Key, ex.Message, StringComparison.Ordinal);
    }

    /// <summary>The floor itself and everything above it are real policies.</summary>
    [Theory]
    [InlineData(1.0)]
    [InlineData(1.5)]
    [InlineData(300.0)]
    public void Rates_At_Or_Above_The_Floor_Are_Allowed(double rate)
    {
        var literal = rate.ToString(System.Globalization.CultureInfo.InvariantCulture);
        Assert.Equal(rate, LoadToml($"{Key} = {literal}\n").BrowserRateLimitPerSec);
    }

    // -- The refusals ------------------------------------------------------

    /// <summary>
    /// Zero, negatives, and the whole sub-floor band — refused at load, by a
    /// message that names the key so the operator can find the line they wrote,
    /// and states the floor so they know what to write instead.
    /// </summary>
    [Theory]
    [InlineData("0")]
    [InlineData("0.0")]
    [InlineData("-1")]
    [InlineData("-0.5")]
    [InlineData("0.1")]
    [InlineData("0.5")]
    [InlineData("0.99")]
    public void A_Rate_Under_The_Floor_Is_Refused_At_Load(string literal)
    {
        var ex = Assert.Throws<ArgumentException>(() => LoadToml($"{Key} = {literal}\n"));
        Assert.Contains(Key, ex.Message, StringComparison.Ordinal);
        Assert.Contains("must be >= 1.0", ex.Message, StringComparison.Ordinal);
    }

    /// <summary>
    /// The non-finite three. <c>inf</c> is the dangerous one: it satisfies every
    /// <c>&gt;=</c> bound, so a range check alone would accept it and leave a
    /// "limit" that never refuses anything — silent disabling, exactly what
    /// refusing <c>0</c> exists to prevent.
    /// </summary>
    [Theory]
    [InlineData("nan")]
    [InlineData("inf")]
    [InlineData("+inf")]
    [InlineData("-inf")]
    public void A_Non_Finite_Rate_Is_Refused_At_Load(string literal)
    {
        var ex = Assert.Throws<ArgumentException>(() => LoadToml($"{Key} = {literal}\n"));
        Assert.Contains(Key, ex.Message, StringComparison.Ordinal);
        Assert.Contains("must be a finite number >= 1.0", ex.Message, StringComparison.Ordinal);
    }

    /// <summary>
    /// TOML is not the only way a config is built — the CLI, the conformance
    /// serve driver, and embedding callers all construct one in memory. The
    /// guard lives on the property, so those paths are refused at the moment of
    /// assignment rather than at the first throttled message.
    /// </summary>
    [Fact]
    public void Assigning_A_Bad_Rate_Programmatically_Is_Refused_Too()
    {
        var cfg = UtermServerConfig.Default();
        Assert.Throws<ArgumentException>(() => cfg.BrowserRateLimitPerSec = 0);
        Assert.Throws<ArgumentException>(() => cfg.BrowserRateLimitPerSec = 0.5);
        Assert.Throws<ArgumentException>(() => cfg.BrowserRateLimitPerSec = -3);
        Assert.Throws<ArgumentException>(() => cfg.BrowserRateLimitPerSec = double.NaN);
        Assert.Throws<ArgumentException>(() => cfg.BrowserRateLimitPerSec = double.PositiveInfinity);
        Assert.Throws<ArgumentException>(() => cfg.BrowserRateLimitPerSec = double.NegativeInfinity);

        // Refused means unchanged, not half-applied.
        Assert.Equal(300, cfg.BrowserRateLimitPerSec);
    }

    // -- Why the floor is 1.0 ----------------------------------------------

    /// <summary>
    /// The floor is measured, not copied. A <see cref="TokenBucket"/> defaults
    /// its burst to one second of its rate, so a bucket configured below 1/s can
    /// never accumulate the single whole token one browser message costs: it
    /// denies every message forever, however long the caller waits. A configured
    /// 0.5 is not "one message every two seconds", it is "never".
    ///
    /// Driven by <see cref="ManualClock"/>, so "however long you wait" is a day
    /// of simulated time and a millisecond of real time.
    /// </summary>
    [Theory]
    [InlineData(0.0)]
    [InlineData(0.5)]
    [InlineData(0.99)]
    public void A_Browser_Bucket_Below_The_Floor_Admits_Nothing_However_Long_You_Wait(double rate)
    {
        var clock = new ManualClock();
        var bucket = new TokenBucket(rate, clock: clock);

        Assert.False(bucket.Allow());
        clock.SetMonotonic(60);
        Assert.False(bucket.Allow());
        clock.SetMonotonic(3600);
        Assert.False(bucket.Allow());
        clock.SetMonotonic(86_400);
        Assert.False(bucket.Allow());
    }

    /// <summary>
    /// And the floor and the default both admit their first message, which is
    /// what makes 1.0 the tightest honourable rate rather than an arbitrary one.
    /// </summary>
    [Theory]
    [InlineData(1.0)]
    [InlineData(300.0)]
    public void A_Browser_Bucket_At_Or_Above_The_Floor_Admits_Its_First_Message(double rate)
    {
        Assert.True(new TokenBucket(rate, clock: new ManualClock()).Allow());
    }
}
