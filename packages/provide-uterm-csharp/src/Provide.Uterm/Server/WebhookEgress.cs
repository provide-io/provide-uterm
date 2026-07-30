//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Server;

/// <summary>
/// Resolves a hostname to addresses. Injected so the guard can be tested without
/// a resolver, and so a deployment can supply one that goes somewhere specific.
/// </summary>
/// <remarks>
/// The Go port models the same seam as a <c>HostResolver</c> function type. It is
/// an interface here only because that is how the rest of this port spells its
/// seams (<c>IClock</c>, <c>ISessionRegistry</c>).
/// </remarks>
public interface IHostResolver
{
    /// <summary>
    /// Every address <paramref name="host"/> resolves to. Throwing, or returning
    /// nothing, is a refusal — never a pass (§5).
    /// </summary>
    Task<IReadOnlyList<IPAddress>> ResolveAsync(string host, CancellationToken cancellationToken = default);
}

/// <summary>The production resolver: the platform DNS client.</summary>
public sealed class DnsHostResolver : IHostResolver
{
    public async Task<IReadOnlyList<IPAddress>> ResolveAsync(
        string host,
        CancellationToken cancellationToken = default) =>
        await Dns.GetHostAddressesAsync(host, cancellationToken).ConfigureAwait(false);
}

/// <summary>
/// The outcome of classifying one webhook destination.
/// </summary>
/// <param name="Allowed">Whether the destination may be used at all.</param>
/// <param name="Loopback">
/// Whether the destination is loopback, reported <em>independently</em> of
/// <paramref name="Allowed"/>. The delivery-time tunnel check (§4) needs to know
/// "this is loopback" even in the case where loopback is currently permitted,
/// which is precisely the case where <paramref name="Allowed"/> is true.
/// </param>
/// <param name="Reason">Why it was refused; empty when allowed.</param>
public sealed record WebhookEgressDecision(bool Allowed, bool Loopback, string Reason);

/// <summary>
/// The SSRF egress guard for webhook destinations, implementing
/// <c>conformance/EGRESS_GUARD.md</c> §1, §2 and §5.
/// </summary>
/// <remarks>
/// <para>
/// A webhook destination is a URL the server fetches, chosen by whoever can
/// mutate a session. The server sits in a more privileged network position than
/// that caller: it can reach the metadata service, private admin ports, and
/// anything bound to loopback. Validating the destination is what stops the
/// caller borrowing that position.
/// </para>
/// <para>
/// Everything in §1 is refused with no key to re-open it. Loopback is the single
/// conditional case, because binding to <c>127.0.0.1</c> is itself an access
/// control — services listen there precisely so the network cannot reach them,
/// and skip authentication on that basis — so loopback SSRF converts
/// "unreachable" into "reachable" in a way that a private-range service, which
/// at least chose a routable interface, does not.
/// </para>
/// </remarks>
public sealed class WebhookEgressPolicy
{
    /// <summary>
    /// Hard bound on one resolution, so a slow or hostile resolver cannot stall
    /// registration. Matches the reference's <c>_REGISTER_DNS_TIMEOUT_S</c>.
    /// </summary>
    public static readonly TimeSpan DefaultResolveTimeout = TimeSpan.FromSeconds(2);

    private const string ReasonRequired = "url is required";
    private const string ReasonScheme = "url must be absolute http(s)";
    private const string ReasonNoHost = "webhook url must include a host";
    private const string ReasonLoopback = "loopback webhook destinations are not allowed";
    private const string ReasonNotAllowed = "webhook url host is not allowed";
    private const string ReasonUnresolvable = "webhook url host could not be resolved";

    private readonly IHostResolver _resolver;
    private readonly TimeSpan _resolveTimeout;

    public WebhookEgressPolicy(
        bool allowLoopbackDestinations,
        IHostResolver? resolver = null,
        TimeSpan? resolveTimeout = null)
    {
        AllowLoopbackDestinations = allowLoopbackDestinations;
        _resolver = resolver ?? new DnsHostResolver();
        _resolveTimeout = resolveTimeout ?? DefaultResolveTimeout;
    }

    /// <summary>The effective permission this policy was built with (§3).</summary>
    public bool AllowLoopbackDestinations { get; }

    /// <summary>
    /// Classify a destination URL. Never throws: the caller decides whether a
    /// refusal is a 422 (registration) or a dropped delivery with a counter.
    /// </summary>
    public WebhookEgressDecision Classify(string? url)
    {
        if (string.IsNullOrWhiteSpace(url))
        {
            return Refuse(ReasonRequired);
        }

        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri) ||
            (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
        {
            return Refuse(ReasonScheme);
        }

        // Trailing dots are stripped and the name lowercased before comparison:
        // `metadata.google.internal.` and `METADATA.GOOGLE.INTERNAL` name the
        // same host to a resolver, so a guard that compares the raw string is
        // bypassed by typing one extra character.
        var host = uri.DnsSafeHost.Trim().Trim('[', ']').TrimEnd('.').ToLowerInvariant();
        if (host.Length == 0)
        {
            return Refuse(ReasonNoHost);
        }

        // Refused by name, before resolution. On GCE it answers with
        // 169.254.169.254 and would be caught below anyway, but the name is how
        // the attack is written, and a split-horizon resolver is free to answer
        // with something that classifies as public.
        if (host == "metadata.google.internal")
        {
            return Refuse(ReasonNotAllowed);
        }

        // `localhost` and `*.localhost` are loopback by definition (RFC 6761),
        // whatever the local resolver happens to say, so they are classified
        // rather than resolved.
        if (host == "localhost" || host.EndsWith(".localhost", StringComparison.Ordinal))
        {
            return AllowLoopbackDestinations
                ? new WebhookEgressDecision(true, true, "")
                : new WebhookEgressDecision(false, true, ReasonLoopback);
        }

        if (IPAddress.TryParse(host, out var literal))
        {
            return CheckAddresses(new[] { literal });
        }

        // A DNS name: resolve it and check every answer (§5). This is what stops
        // rebinding-style SSRF, where a name the operator vouched for answers
        // with metadata or private space.
        IReadOnlyList<IPAddress> addresses;
        try
        {
            addresses = Resolve(host);
        }
        catch (Exception)
        {
            // Deliberately broad, and deliberately a refusal: a resolver that
            // fails, times out, or throws something unexpected leaves the
            // destination unclassified, and "unclassified" must never mean
            // "allowed". Fail closed.
            return Refuse(ReasonUnresolvable);
        }

        return addresses.Count == 0 ? Refuse(ReasonUnresolvable) : CheckAddresses(addresses);
    }

    /// <summary>
    /// The effective loopback permission (§3), computed from config in one place.
    /// </summary>
    /// <remarks>
    /// <para>
    /// <c>webhooks.allow_loopback_destinations</c> OR the server being bound to
    /// loopback. The bind term is there because the default bind <em>is</em>
    /// <c>127.0.0.1</c>: without it the default configuration listens only on
    /// loopback and refuses loopback webhook destinations, which protects
    /// nothing — no remote caller can reach the listener — at the cost of
    /// breaking every single-box deployment.
    /// </para>
    /// <para>
    /// Deriving permissiveness from the bind address is the established idiom
    /// here; <c>GatewayBindPolicy.IsLoopbackBindHost</c> already answers exactly
    /// this question (and answers <c>false</c> for <c>0.0.0.0</c> / <c>::</c>,
    /// which are bind wildcards and not loopback), so it is reused rather than
    /// re-implemented a second way that could drift from the first.
    /// </para>
    /// </remarks>
    public static bool EffectiveAllowLoopbackDestinations(UtermServerConfig cfg) =>
        cfg.Webhooks.AllowLoopbackDestinations ||
        Gateway.GatewayBindPolicy.IsLoopbackBindHost(cfg.Server.Host);

    /// <summary>
    /// Resolve under a hard timeout, even if the resolver ignores the token.
    /// </summary>
    /// <remarks>
    /// Blocking, like the reference's <c>validate_webhook_url</c>: registration
    /// is an operator-rate REST call rather than a hot path, the wait is bounded
    /// by <see cref="DefaultResolveTimeout"/>, and ASP.NET Core has no
    /// synchronization context for this to deadlock against. If a background
    /// delivery loop is ever added to this port, that path should take an async
    /// classification instead of borrowing this one.
    /// </remarks>
    private IReadOnlyList<IPAddress> Resolve(string host)
    {
        using var cts = new CancellationTokenSource(_resolveTimeout);
        return _resolver
            .ResolveAsync(host, cts.Token)
            .WaitAsync(_resolveTimeout)
            .GetAwaiter()
            .GetResult();
    }

    private WebhookEgressDecision CheckAddresses(IReadOnlyList<IPAddress> addresses)
    {
        var loopback = false;
        foreach (var address in addresses)
        {
            // Decode first: an IPv6 wrapper carrying a metadata or private IPv4
            // must be classified as what it reaches, not as "some IPv6 address".
            var ip = EgressAddressPolicy.DecodeEmbeddedIPv4(address) ?? address;

            if (EgressAddressPolicy.IsMetadata(ip))
            {
                return new WebhookEgressDecision(false, loopback, ReasonNotAllowed);
            }

            // Before the private check, not after: loopback is inside
            // 127.0.0.0/8 and ::1, both of which the blocked lists contain, and
            // it is the one classification the operator can permit.
            if (EgressAddressPolicy.IsLoopback(ip))
            {
                loopback = true;
                if (!AllowLoopbackDestinations)
                {
                    return new WebhookEgressDecision(false, true, ReasonLoopback);
                }

                continue;
            }

            if (EgressAddressPolicy.IsBlockedPrivate(ip))
            {
                return new WebhookEgressDecision(false, loopback, ReasonNotAllowed);
            }
        }

        return new WebhookEgressDecision(true, loopback, "");
    }

    private static WebhookEgressDecision Refuse(string reason) => new(false, false, reason);
}
