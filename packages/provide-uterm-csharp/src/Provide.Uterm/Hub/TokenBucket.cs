//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Hub;

/// <summary>Simple token-bucket rate limiter. Not internally synchronized.</summary>
public sealed class TokenBucket
{
    /// <summary>
    /// Tightest rate (tokens/sec) any bucket-backed policy may be configured
    /// with.
    ///
    /// This is 1.0 for a structural reason, not a taste one:
    /// <see cref="TokenBucket"/> defaults <c>burst</c> to one second of the
    /// rate, so a bucket configured below 1.0 can never hold a whole token and
    /// therefore denies <em>every</em> call forever, however long the caller
    /// waits. A rate in <c>[0, 1)</c> is a bricked endpoint wearing the costume
    /// of a rate limit, so the server config refuses the whole band rather than
    /// accepting a number that silently means "never".
    ///
    /// <see cref="RateLimiter"/> also clamps to this floor. Config refusing
    /// below it keeps the clamp from quietly handing back a <em>looser</em>
    /// limit than the operator wrote.
    ///
    /// Making sub-1 rates meaningful would mean decoupling burst from rate
    /// (<c>burst = max(1.0, rate)</c>) — a change to token-bucket semantics
    /// across every port and their recorded goldens. Worth doing deliberately
    /// if a sub-1 policy is ever actually wanted; not worth doing by accident
    /// here. Port of <c>bridge.ratelimit.MIN_RATE_PER_SEC</c>.
    /// </summary>
    public const double MinRatePerSec = 1.0;

    private readonly double _rate;
    private readonly double _burst;
    private double _tokens;
    private double _lastRefill;
    private readonly IClock _clock;

    public TokenBucket(double ratePerSec, double? burst = null, IClock? clock = null)
    {
        _clock = ClockUtil.OrDefault(clock);
        _rate = ratePerSec;
        _burst = burst ?? ratePerSec;
        _tokens = _burst;
        _lastRefill = _clock.Monotonic();
    }

    public bool Allow()
    {
        var now = _clock.Monotonic();
        var elapsed = now - _lastRefill;
        _tokens = Math.Min(_burst, _tokens + elapsed * _rate);
        _lastRefill = now;
        if (_tokens >= 1.0)
        {
            _tokens -= 1.0;
            return true;
        }

        return false;
    }
}
