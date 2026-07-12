//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Hub;

/// <summary>Simple token-bucket rate limiter. Not internally synchronized.</summary>
public sealed class TokenBucket
{
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
