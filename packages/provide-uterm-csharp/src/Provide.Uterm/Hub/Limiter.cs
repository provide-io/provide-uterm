//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Hub;

/// <summary>Per-purpose token buckets for hub REST endpoints.</summary>
public sealed class RateLimiter
{
    public const int RestClientCacheMax = 1024;
    public const int RestClientEvictCount = RestClientCacheMax / 2;

    private readonly object _gate = new();
    private readonly double _acquireRate;
    private readonly double _sendRate;
    private readonly TokenBucket _acquireBucket;
    private readonly TokenBucket _sendBucket;
    private readonly LruBuckets _acquirePerClient = new();
    private readonly LruBuckets _sendPerClient = new();
    private readonly IClock _clock;

    public RateLimiter(double restAcquireRate, double restSendRate, IClock? clock = null)
    {
        _clock = ClockUtil.OrDefault(clock);
        _acquireRate = Math.Max(0.1, restAcquireRate);
        _sendRate = Math.Max(0.1, restSendRate);
        _acquireBucket = new TokenBucket(_acquireRate, clock: _clock);
        _sendBucket = new TokenBucket(_sendRate, clock: _clock);
    }

    public double AcquireRate => _acquireRate;
    public double SendRate => _sendRate;

    public bool AllowRestAcquire(string clientId)
    {
        lock (_gate)
        {
            var bucket = _acquirePerClient.Touch(clientId, _acquireRate, _clock);
            if (!bucket.Allow())
            {
                return false;
            }

            return _acquireBucket.Allow();
        }
    }

    public bool AllowRestSend(string clientId)
    {
        lock (_gate)
        {
            var bucket = _sendPerClient.Touch(clientId, _sendRate, _clock);
            if (!bucket.Allow())
            {
                return false;
            }

            return _sendBucket.Allow();
        }
    }

    private sealed class LruBuckets
    {
        private readonly List<string> _order = new();
        private readonly Dictionary<string, TokenBucket> _map = new();

        public TokenBucket Touch(string key, double rate, IClock clock)
        {
            if (_map.TryGetValue(key, out var b))
            {
                _map.Remove(key);
                _order.Remove(key);
            }
            else
            {
                b = new TokenBucket(rate, clock: clock);
            }

            _map[key] = b;
            _order.Add(key);
            EvictIfFull();
            return b;
        }

        private void EvictIfFull()
        {
            if (_map.Count <= RestClientCacheMax)
            {
                return;
            }

            var victims = _order.Take(RestClientEvictCount).ToList();
            foreach (var k in victims)
            {
                _map.Remove(k);
            }

            _order.RemoveRange(0, Math.Min(RestClientEvictCount, _order.Count));
        }
    }
}
