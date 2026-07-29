//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerConfig;
using Xunit;

namespace Provide.Uterm.Tests;

/// <summary>
/// The two REST hijack ceilings an operator may write in server.toml:
/// <c>rest_acquire_rate_limit_per_sec</c> (default 5) guards the expensive,
/// state-changing lease grab, and <c>rest_send_rate_limit_per_sec</c>
/// (default 20) is shared by the hijack send <em>and</em> step endpoints.
/// Burst is one second of the same rate, and each ceiling is applied twice —
/// globally and per calling client. The reference is
/// <c>config_schema.UtermServerConfig.rest_*_rate_limit_per_sec</c> plus its
/// <c>_validate_rest_rate_limit</c> field validator.
///
/// The refusals are the point of the file. A rate limit is trusted once
/// configured, so every value that cannot be honoured verbatim is refused
/// rather than reinterpreted.
///
/// <em>Not finite.</em> <c>inf</c> passes every <c>&gt;=</c> bound, so accepting
/// it would silently mean "no limit at all" — the same fail-open that makes a
/// trusted limit worse than none. <c>-inf</c> and <c>NaN</c> go with it.
///
/// <em>Below the floor.</em> <c>0</c> is ambiguous — "unlimited" disables the
/// limit, "refuse everything" bricks the REST hijack API, and nothing in the
/// file says which was meant. The whole band under the floor is refused for the
/// <em>second</em> of those reasons rather than for ambiguity: a token bucket's
/// burst is one second of its rate, so a sub-1/s bucket never holds a whole
/// token and denies every call forever. <c>0.5</c> is not "one call every two
/// seconds", it is "never" — so it is refused exactly like <c>0</c>. That
/// property is asserted directly below against <see cref="TokenBucket"/>, so
/// nobody can lower the floor again without the bucket contradicting them.
///
/// Every refusal arrives at load. A server that boots with a nonsense limit and
/// discovers it at first use is a server that ran unprotected in between.
///
/// Time is driven by <see cref="ManualClock"/> through the hub's limiter, so a
/// budget that is spent stays spent for the length of the test rather than
/// quietly refilling on a slow runner.
/// </summary>
public sealed class ServerConfigRestRateLimitTests
{
    private const string Worker = "provide-shell";
    private const string AcquireKey = "rest_acquire_rate_limit_per_sec";
    private const string SendKey = "rest_send_rate_limit_per_sec";

    private static string WriteToml(string body)
    {
        var path = Path.Combine(Path.GetTempPath(), "uterm-rest-rl-" + Guid.NewGuid().ToString("N") + ".toml");
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

    // -- The defaults ------------------------------------------------------

    /// <summary>
    /// A deployment that never heard of these keys must behave exactly as it
    /// did before they existed: the hub's own built-in 5 and 20.
    /// </summary>
    [Fact]
    public void Absent_Keys_Keep_The_Hubs_Built_In_Defaults()
    {
        var fromDefault = UtermServerConfig.Default();
        Assert.Equal(5, fromDefault.RestAcquireRateLimitPerSec);
        Assert.Equal(20, fromDefault.RestSendRateLimitPerSec);

        var fromNoPath = ConfigLoader.Load(null);
        Assert.Equal(5, fromNoPath.RestAcquireRateLimitPerSec);
        Assert.Equal(20, fromNoPath.RestSendRateLimitPerSec);

        var fromSilentToml = LoadToml("""
            [server]
            host = "127.0.0.1"
            """);
        Assert.Equal(5, fromSilentToml.RestAcquireRateLimitPerSec);
        Assert.Equal(20, fromSilentToml.RestSendRateLimitPerSec);
    }

    // -- Reading the keys --------------------------------------------------

    /// <summary>
    /// Both keys are read, independently, and a bare TOML integer counts as a
    /// rate — an operator who writes <c>7</c> rather than <c>7.0</c> must not
    /// have it silently folded back to the default.
    /// </summary>
    [Fact]
    public void Both_Keys_Are_Read_From_Toml()
    {
        var cfg = LoadToml($"""
            {AcquireKey} = 1.5
            {SendKey} = 7
            """);
        Assert.Equal(1.5, cfg.RestAcquireRateLimitPerSec);
        Assert.Equal(7, cfg.RestSendRateLimitPerSec);
    }

    /// <summary>
    /// The floor itself and fractions above it are real policies, not typos:
    /// 1.5/s is a rate a bucket can actually honour.
    /// </summary>
    [Theory]
    [InlineData(1.0)]
    [InlineData(1.5)]
    [InlineData(2.5)]
    public void Rates_At_Or_Above_The_Floor_Are_Allowed(double rate)
    {
        var literal = rate.ToString(System.Globalization.CultureInfo.InvariantCulture);
        var cfg = LoadToml($"""
            {AcquireKey} = {literal}
            {SendKey} = {literal}
            """);
        Assert.Equal(rate, cfg.RestAcquireRateLimitPerSec);
        Assert.Equal(rate, cfg.RestSendRateLimitPerSec);
    }

    // -- The refusals ------------------------------------------------------

    /// <summary>
    /// Zero, negative, and the whole sub-floor band — each refused at load, by
    /// a message that names the key the operator wrote so they can find it.
    /// <c>0.5</c> and <c>0.99</c> are in here rather than among the accepted
    /// fractions because a bucket cannot honour them at all; see
    /// <see cref="A_Bucket_Below_The_Floor_Admits_Nothing_However_Long_You_Wait"/>.
    /// </summary>
    [Theory]
    [InlineData(AcquireKey, "0")]
    [InlineData(AcquireKey, "0.0")]
    [InlineData(AcquireKey, "-1")]
    [InlineData(AcquireKey, "-0.5")]
    [InlineData(AcquireKey, "0.1")]
    [InlineData(AcquireKey, "0.5")]
    [InlineData(AcquireKey, "0.99")]
    [InlineData(SendKey, "0")]
    [InlineData(SendKey, "0.0")]
    [InlineData(SendKey, "-1")]
    [InlineData(SendKey, "-0.5")]
    [InlineData(SendKey, "0.1")]
    [InlineData(SendKey, "0.5")]
    [InlineData(SendKey, "0.99")]
    public void A_Rate_Under_The_Floor_Is_Refused_At_Load(string key, string literal)
    {
        var path = WriteToml($"{key} = {literal}\n");
        try
        {
            var ex = Assert.Throws<ArgumentException>(() => ConfigLoader.Load(path));
            Assert.Contains(key, ex.Message, StringComparison.Ordinal);
            Assert.Contains("must be >= 1.0", ex.Message, StringComparison.Ordinal);
        }
        finally
        {
            File.Delete(path);
        }
    }

    /// <summary>
    /// The non-finite three. <c>inf</c> is the dangerous one: it satisfies
    /// every <c>&gt;=</c> bound, so a range check alone would accept it and
    /// leave a "limit" that never refuses anything — silent disabling, exactly
    /// what refusing <c>0</c> exists to prevent. They get their own message
    /// because "must be &gt;= 1.0" is not useful advice for <c>inf</c>.
    /// </summary>
    [Theory]
    [InlineData(AcquireKey, "nan")]
    [InlineData(AcquireKey, "inf")]
    [InlineData(AcquireKey, "+inf")]
    [InlineData(AcquireKey, "-inf")]
    [InlineData(SendKey, "nan")]
    [InlineData(SendKey, "inf")]
    [InlineData(SendKey, "+inf")]
    [InlineData(SendKey, "-inf")]
    public void A_Non_Finite_Rate_Is_Refused_At_Load(string key, string literal)
    {
        var path = WriteToml($"{key} = {literal}\n");
        try
        {
            var ex = Assert.Throws<ArgumentException>(() => ConfigLoader.Load(path));
            Assert.Contains(key, ex.Message, StringComparison.Ordinal);
            Assert.Contains("must be a finite number >= 1.0", ex.Message, StringComparison.Ordinal);
        }
        finally
        {
            File.Delete(path);
        }
    }

    /// <summary>
    /// NaN specifically, and the shape of the bound behind it. The finiteness
    /// check refuses NaN first, but the range check is still written
    /// <c>!(value &gt;= MIN)</c> rather than <c>value &lt; MIN</c> so that it
    /// refuses NaN too — a second line of defence that costs nothing and
    /// survives someone reordering or dropping the finiteness check. The two
    /// comparison assertions state the trap outright, so the reasoning is
    /// checked rather than merely commented.
    /// </summary>
    [Fact]
    public void NaN_Is_Refused_Rather_Than_Compared_Away()
    {
        Assert.False(double.NaN < TokenBucket.MinRatePerSec);
        Assert.False(double.NaN >= TokenBucket.MinRatePerSec);

        var ex = Assert.Throws<ArgumentException>(() => LoadToml($"{SendKey} = nan\n"));
        Assert.Contains(SendKey, ex.Message, StringComparison.Ordinal);
        Assert.DoesNotContain(AcquireKey, ex.Message, StringComparison.Ordinal);
        Assert.Contains("NaN", ex.Message, StringComparison.Ordinal);
    }

    // -- The property the floor exists for ---------------------------------

    /// <summary>
    /// Why the floor is 1.0 and not a smaller-looking number. A
    /// <see cref="TokenBucket"/> defaults its burst to one second of its rate,
    /// so a bucket configured below 1/s can never accumulate a whole token: it
    /// denies every call forever, however long the caller waits. A configured
    /// 0.5 is not "one call every two seconds", it is "never", which is the
    /// silent bricking the validator refuses <c>0</c> to prevent.
    ///
    /// Driven by <see cref="ManualClock"/>, so "however long you wait" is a day
    /// of simulated time and a millisecond of real time.
    /// </summary>
    [Theory]
    [InlineData(0.1)]
    [InlineData(0.5)]
    [InlineData(0.99)]
    public void A_Bucket_Below_The_Floor_Admits_Nothing_However_Long_You_Wait(double rate)
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
    /// And at the floor the bucket works on its first call — which is what
    /// makes 1.0 the tightest honourable rate rather than an arbitrary choice.
    /// </summary>
    [Fact]
    public void A_Bucket_At_The_Floor_Admits_Its_First_Call()
    {
        Assert.Equal(1.0, TokenBucket.MinRatePerSec);
        var bucket = new TokenBucket(TokenBucket.MinRatePerSec, clock: new ManualClock());
        Assert.True(bucket.Allow());
    }

    /// <summary>
    /// The limiter's clamp and the config's floor read the same constant, so
    /// they cannot drift apart into a window where config accepts a rate the
    /// limiter then silently raises.
    /// </summary>
    [Fact]
    public void The_Limiter_Clamps_To_The_Same_Floor_The_Config_Refuses()
    {
        var limiter = new RateLimiter(0.5, 0.25, new ManualClock());
        Assert.Equal(TokenBucket.MinRatePerSec, limiter.AcquireRate);
        Assert.Equal(TokenBucket.MinRatePerSec, limiter.SendRate);
    }

    /// <summary>
    /// A value that is not a number at all is refused too, and by name. The
    /// loader ignores keys it does not know, so a rate that silently failed to
    /// parse would be indistinguishable from one never written — which is the
    /// exact failure the validator exists to prevent.
    /// </summary>
    [Theory]
    [InlineData("\"fast\"")]
    [InlineData("true")]
    public void A_Non_Numeric_Rate_Is_Refused_At_Load(string literal)
    {
        var ex = Assert.Throws<ArgumentException>(() => LoadToml($"{AcquireKey} = {literal}\n"));
        Assert.Contains(AcquireKey, ex.Message, StringComparison.Ordinal);
    }

    /// <summary>
    /// A numeric string is still a number, as it is for the reference's
    /// (lax-mode) float field.
    /// </summary>
    [Fact]
    public void A_Numeric_String_Is_Still_A_Rate()
    {
        var cfg = LoadToml($"{SendKey} = \"2.5\"\n");
        Assert.Equal(2.5, cfg.RestSendRateLimitPerSec);
    }

    /// <summary>
    /// TOML is not the only way a config is built — the CLI, the conformance
    /// serve driver, and embedding callers all construct one in memory. The
    /// guard lives on the property, so those paths are refused at the moment of
    /// assignment rather than at the first throttled request.
    /// </summary>
    [Fact]
    public void Assigning_A_Bad_Rate_Programmatically_Is_Refused_Too()
    {
        var cfg = UtermServerConfig.Default();
        Assert.Throws<ArgumentException>(() => cfg.RestAcquireRateLimitPerSec = 0);
        Assert.Throws<ArgumentException>(() => cfg.RestAcquireRateLimitPerSec = 0.5);
        Assert.Throws<ArgumentException>(() => cfg.RestSendRateLimitPerSec = -3);
        Assert.Throws<ArgumentException>(() => cfg.RestSendRateLimitPerSec = double.NaN);
        Assert.Throws<ArgumentException>(() => cfg.RestSendRateLimitPerSec = double.PositiveInfinity);
        Assert.Throws<ArgumentException>(() => cfg.RestSendRateLimitPerSec = double.NegativeInfinity);

        // Refused means unchanged, not half-applied.
        Assert.Equal(5, cfg.RestAcquireRateLimitPerSec);
        Assert.Equal(20, cfg.RestSendRateLimitPerSec);
    }

    // -- The values actually reaching the limiter --------------------------

    private sealed class Harness : IAsyncDisposable
    {
        public required UtermServer Server { get; init; }
        public required HttpClient Http { get; init; }
        public required ManualClock Clock { get; init; }
        public string HijackId { get; set; } = "";

        public Task<HttpResponseMessage> Acquire() =>
            Http.PostAsync($"/worker/{Worker}/hijack/acquire", Json("""{"owner":"tester","lease_s":600}"""));

        public Task<HttpResponseMessage> Release() =>
            Http.PostAsync($"/worker/{Worker}/hijack/{HijackId}/release", Json("{}"));

        public Task<HttpResponseMessage> Send() =>
            Http.PostAsync($"/worker/{Worker}/hijack/{HijackId}/send", Json("""{"keys":"x"}"""));

        public async ValueTask DisposeAsync()
        {
            Http.Dispose();
            await Server.DisposeAsync().ConfigureAwait(false);
        }
    }

    private static StringContent Json(string body) => new(body, Encoding.UTF8, "application/json");

    /// <summary>
    /// A live server built the production way — TOML on disk, through
    /// <see cref="ConfigLoader"/> and <see cref="ServerFactory.CreateFromConfig"/>
    /// — so what is measured is the operator's file reaching the hub's buckets,
    /// not a hand-wired <see cref="TermHubConfig"/>.
    /// </summary>
    private static async Task<Harness> StartAsync(string tomlBody)
    {
        var path = WriteToml(tomlBody);
        UtermServerConfig cfg;
        try
        {
            cfg = ConfigLoader.Load(path);
        }
        finally
        {
            File.Delete(path);
        }

        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = 0;
        cfg.Server.PublicBaseUrl = "";
        cfg.Auth.Mode = "dev_token";
        var clock = new ManualClock(wall: 1000);
        var (server, token) = ServerFactory.CreateFromConfig(cfg, "rest-rate-limit", clock: clock);
        server.Build(["http://127.0.0.1:0"]);
        await server.StartAsync();
        var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
        http.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + token);
        return new Harness { Server = server, Http = http, Clock = clock };
    }

    private static async Task<Harness> StartHijackedAsync(string tomlBody)
    {
        var h = await StartAsync(tomlBody);
        (await h.Http.PostAsync($"/api/sessions/{Worker}/mode", Json("""{"input_mode": "hijack"}""")))
            .EnsureSuccessStatusCode();
        var acquire = await h.Acquire();
        acquire.EnsureSuccessStatusCode();
        h.HijackId = (await acquire.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("hijack_id").GetString()!;
        return h;
    }

    /// <summary>
    /// The configured acquire ceiling is the number of lease grabs the route
    /// serves before it starts refusing — one, when the operator writes one.
    /// </summary>
    [Fact]
    public async Task A_Configured_Acquire_Rate_Is_What_The_Acquire_Route_Spends()
    {
        await using var h = await StartAsync($"{AcquireKey} = 1\n");

        Assert.NotEqual(HttpStatusCode.TooManyRequests, (await h.Acquire()).StatusCode);
        Assert.Equal(HttpStatusCode.TooManyRequests, (await h.Acquire()).StatusCode);

        // Spent means spent until the clock says otherwise: ten seconds at 1/s.
        h.Clock.SetMonotonic(10);
        Assert.NotEqual(HttpStatusCode.TooManyRequests, (await h.Acquire()).StatusCode);
    }

    /// <summary>
    /// And with the key absent the budget is five, which is the default the
    /// cross-port conformance scenario <c>008_rate_limits</c> floods.
    /// </summary>
    [Fact]
    public async Task The_Default_Acquire_Budget_Is_Five()
    {
        await using var h = await StartAsync("environment = \"test\"\n");

        for (var i = 0; i < 5; i++)
        {
            Assert.NotEqual(HttpStatusCode.TooManyRequests, (await h.Acquire()).StatusCode);
        }

        Assert.Equal(HttpStatusCode.TooManyRequests, (await h.Acquire()).StatusCode);
    }

    /// <summary>
    /// The send ceiling is separately configurable and separately spent: one
    /// send at a configured 1/s, then refusal, while the acquire budget — sized
    /// generously here — is untouched by it.
    /// </summary>
    [Fact]
    public async Task A_Configured_Send_Rate_Is_What_The_Send_Route_Spends()
    {
        await using var h = await StartHijackedAsync($"""
            {AcquireKey} = 50
            {SendKey} = 1
            """);

        Assert.Equal(HttpStatusCode.OK, (await h.Send()).StatusCode);
        Assert.Equal(HttpStatusCode.TooManyRequests, (await h.Send()).StatusCode);

        // The acquire budget is its own: re-leasing still works with send spent.
        Assert.Equal(HttpStatusCode.OK, (await h.Release()).StatusCode);
        Assert.Equal(HttpStatusCode.OK, (await h.Acquire()).StatusCode);
    }

    /// <summary>
    /// With the key absent the send budget is twenty — enough that a normal
    /// typing burst is never throttled, which is why it is far above acquire's.
    /// </summary>
    [Fact]
    public async Task The_Default_Send_Budget_Is_Twenty()
    {
        await using var h = await StartHijackedAsync("environment = \"test\"\n");

        for (var i = 0; i < 20; i++)
        {
            Assert.Equal(HttpStatusCode.OK, (await h.Send()).StatusCode);
        }

        Assert.Equal(HttpStatusCode.TooManyRequests, (await h.Send()).StatusCode);
    }
}
