//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.IdentityModel.Tokens;

namespace Provide.Uterm.ServerAuth;

/// <summary>
/// Process-wide JWKS document cache (url → kid → RSA public key), mirroring Go
/// <c>serverauth/jwks.go</c>: RSA-only, kid match with single-key fallback, max 16 URLs.
/// </summary>
internal static class Jwks
{
    private const int CacheMax = 16;
    private static readonly TimeSpan FetchTimeout = TimeSpan.FromSeconds(10);
    private static readonly object CacheLock = new();
    private static Dictionary<string, Dictionary<string, RsaSecurityKey>> _cache = new(StringComparer.Ordinal);
    private static HttpClient? _httpClient;

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
    };

    /// <summary>Override HTTP client for tests. Pass null to restore the default 10s client.</summary>
    internal static void SetHttpClient(HttpClient? client)
    {
        lock (CacheLock)
        {
            _httpClient = client;
        }
    }

    /// <summary>Clear the process-wide JWKS cache (test hook).</summary>
    internal static void ResetCache()
    {
        lock (CacheLock)
        {
            _cache = new Dictionary<string, Dictionary<string, RsaSecurityKey>>(StringComparer.Ordinal);
        }
    }

    /// <summary>
    /// Fetch (and cache) the JWKS document for <paramref name="url"/> and return the
    /// RSA public key matching <paramref name="kid"/>. Single-key documents accept
    /// tokens with empty/missing kid.
    /// </summary>
    internal static RsaSecurityKey ResolveKey(string url, string? kid)
    {
        if (string.IsNullOrWhiteSpace(url))
        {
            throw new SecurityTokenException("jwt_jwks_url is empty");
        }

        Dictionary<string, RsaSecurityKey>? keys;
        lock (CacheLock)
        {
            _cache.TryGetValue(url, out keys);
        }

        if (keys is null)
        {
            keys = FetchJwks(url);
            lock (CacheLock)
            {
                if (_cache.Count >= CacheMax)
                {
                    // Evict one arbitrary entry (map order is arbitrary).
                    foreach (var existing in _cache.Keys)
                    {
                        _cache.Remove(existing);
                        break;
                    }
                }

                _cache[url] = keys;
            }
        }

        var want = kid ?? "";
        if (keys.TryGetValue(want, out var matched))
        {
            return matched;
        }

        // Single-key JWKS documents often omit kid; accept the sole key.
        if (keys.Count == 1)
        {
            foreach (var sole in keys.Values)
            {
                return sole;
            }
        }

        throw new SecurityTokenException($"no JWKS key matches kid \"{want}\"");
    }

    private static HttpClient Http
    {
        get
        {
            lock (CacheLock)
            {
                return _httpClient ??= CreateDefaultClient();
            }
        }
    }

    private static HttpClient CreateDefaultClient()
    {
        return new HttpClient { Timeout = FetchTimeout };
    }

    private static Dictionary<string, RsaSecurityKey> FetchJwks(string url)
    {
        // Use the async HTTP path so injectable HttpMessageHandler stubs only need SendAsync.
        using var response = Http.GetAsync(url).GetAwaiter().GetResult();
        if (response.StatusCode != System.Net.HttpStatusCode.OK)
        {
            throw new SecurityTokenException($"JWKS fetch failed: status {(int)response.StatusCode}");
        }

        using var stream = response.Content.ReadAsStream();
        var set = JsonSerializer.Deserialize<JwkSet>(stream, JsonOptions)
                  ?? throw new SecurityTokenException("JWKS document was empty or invalid");

        var outKeys = new Dictionary<string, RsaSecurityKey>(StringComparer.Ordinal);
        if (set.Keys is null || set.Keys.Count == 0)
        {
            throw new SecurityTokenException("JWKS document contained no usable RSA keys");
        }

        foreach (var k in set.Keys)
        {
            if (!string.Equals(k.Kty, "RSA", StringComparison.Ordinal))
            {
                continue; // only RSA JWKs are supported
            }

            var pub = RsaFromJwk(k);
            outKeys[k.Kid ?? ""] = pub;
        }

        if (outKeys.Count == 0)
        {
            throw new SecurityTokenException("JWKS document contained no usable RSA keys");
        }

        return outKeys;
    }

    internal static RsaSecurityKey RsaFromJwk(JwkKey k)
    {
        if (string.IsNullOrEmpty(k.N) || string.IsNullOrEmpty(k.E))
        {
            throw new SecurityTokenException("RSA JWK missing n or e");
        }

        byte[] nBytes;
        byte[] eBytes;
        try
        {
            nBytes = Base64UrlEncoder.DecodeBytes(k.N);
            eBytes = Base64UrlEncoder.DecodeBytes(k.E);
        }
        catch (Exception ex)
        {
            throw new SecurityTokenException("invalid base64url in RSA JWK n/e", ex);
        }

        var rsa = RSA.Create();
        try
        {
            rsa.ImportParameters(new RSAParameters
            {
                Modulus = nBytes,
                Exponent = eBytes,
            });
            return new RsaSecurityKey(rsa) { KeyId = k.Kid };
        }
        catch
        {
            rsa.Dispose();
            throw;
        }
    }

    /// <summary>JSON shape for a JWKS document (RSA subset).</summary>
    internal sealed class JwkSet
    {
        [JsonPropertyName("keys")]
        public List<JwkKey>? Keys { get; set; }
    }

    /// <summary>Single JWK entry (RSA fields only).</summary>
    internal sealed class JwkKey
    {
        [JsonPropertyName("kty")]
        public string? Kty { get; set; }

        [JsonPropertyName("kid")]
        public string? Kid { get; set; }

        [JsonPropertyName("n")]
        public string? N { get; set; }

        [JsonPropertyName("e")]
        public string? E { get; set; }
    }
}
