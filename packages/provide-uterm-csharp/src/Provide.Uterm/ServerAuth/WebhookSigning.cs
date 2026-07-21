//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Globalization;
using System.Security.Cryptography;
using System.Text;

namespace Provide.Uterm.ServerAuth;

/// <summary>
/// HMAC-SHA256 webhook request/response signing (Go serverauth/webhooksign.go parity).
/// Signature is over <c>timestamp + "." + body</c> and rendered as <c>sha256=&lt;hex&gt;</c>.
/// </summary>
public static class WebhookSigning
{
    /// <summary>Default max age for timestamp freshness (seconds).</summary>
    public const double DefaultMaxAgeS = 300.0;

    /// <summary>
    /// Build <c>sha256=&lt;hex&gt;</c> of HMAC-SHA256 over <c>timestamp + "." + body</c>.
    /// Byte-for-byte compatible with the Python/Go signers.
    /// </summary>
    public static string BuildWebhookSignature(string secret, ReadOnlySpan<byte> body, string timestamp)
    {
        var prefix = Encoding.UTF8.GetBytes(timestamp + ".");
        var signed = new byte[prefix.Length + body.Length];
        prefix.CopyTo(signed, 0);
        body.CopyTo(signed.AsSpan(prefix.Length));

        var key = Encoding.UTF8.GetBytes(secret);
        var hash = HMACSHA256.HashData(key, signed);
        return "sha256=" + Convert.ToHexString(hash).ToLowerInvariant();
    }

    /// <summary>
    /// Verify <c>X-Uterm-Signature</c> over ts.body and that the timestamp is fresh.
    /// Fails closed when the secret is empty (empty-key HMAC is forgeable).
    /// When <paramref name="now"/> is null, uses the wall clock.
    /// </summary>
    public static bool VerifyWebhookSignature(
        string secret,
        ReadOnlySpan<byte> body,
        string? signatureHeader,
        string? timestampHeader,
        double maxAgeS = DefaultMaxAgeS,
        double? now = null)
    {
        if (string.IsNullOrWhiteSpace(secret))
        {
            return false;
        }

        if (string.IsNullOrEmpty(signatureHeader) || string.IsNullOrEmpty(timestampHeader))
        {
            return false;
        }

        if (!double.TryParse(timestampHeader, NumberStyles.Float, CultureInfo.InvariantCulture, out var tsVal))
        {
            return false;
        }

        var current = now ?? WallClock();
        if (Math.Abs(current - tsVal) > maxAgeS)
        {
            return false;
        }

        var supplied = signatureHeader.Trim();
        if (supplied.StartsWith("sha256=", StringComparison.OrdinalIgnoreCase))
        {
            supplied = supplied["sha256=".Length..].Trim();
        }

        if (supplied.Length == 0)
        {
            return false;
        }

        var expectedFull = BuildWebhookSignature(secret, body, timestampHeader);
        var eq = expectedFull.IndexOf('=');
        var expected = eq >= 0 ? expectedFull[(eq + 1)..] : expectedFull;
        // Hex digests are ASCII; compare as UTF-8 bytes in fixed time when lengths match.
        var suppliedBytes = Encoding.UTF8.GetBytes(supplied);
        var expectedBytes = Encoding.UTF8.GetBytes(expected);
        if (suppliedBytes.Length != expectedBytes.Length)
        {
            return false;
        }

        return CryptographicOperations.FixedTimeEquals(suppliedBytes, expectedBytes);
    }

    /// <summary>Unix epoch seconds as float64 (Go wallClock parity).</summary>
    public static double WallClock() =>
        DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;

    /// <summary>Format a timestamp for the X-Uterm-Timestamp header (shortest decimal form).</summary>
    public static string FormatTimestamp(double ts) =>
        ts.ToString("0.#############################", CultureInfo.InvariantCulture);
}
