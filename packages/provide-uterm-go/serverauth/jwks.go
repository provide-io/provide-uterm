//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"crypto/rsa"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"sync"
	"time"

	jwt "github.com/golang-jwt/jwt/v5"
)

// jwksCache is the process-wide JWKS document cache keyed by URL, mirroring
// auth._JWKS_CLIENT_CACHE (one entry per issuer in practice, capped).
var (
	jwksCacheMu  sync.Mutex
	jwksCache    = map[string]map[string]any{} // url -> kid -> public key
	jwksCacheMax = 16
	// jwksHTTPClient is the fetcher; overridable in tests.
	jwksHTTPClient = &http.Client{Timeout: 10 * time.Second}
)

type jwkKey struct {
	Kty string `json:"kty"`
	Kid string `json:"kid"`
	N   string `json:"n"`
	E   string `json:"e"`
}

type jwkSet struct {
	Keys []jwkKey `json:"keys"`
}

// resolveJWKSKey fetches (and caches) the JWKS document for url and returns the
// public key matching the token's kid header. Ports the JWKS path of
// _resolve_jwt_key (which Python delegates to PyJWKClient).
func resolveJWKSKey(url string, token *jwt.Token) (any, error) {
	kid, _ := token.Header["kid"].(string)
	jwksCacheMu.Lock()
	keys := jwksCache[url]
	jwksCacheMu.Unlock()
	if keys == nil {
		fetched, err := fetchJWKS(url)
		if err != nil {
			return nil, err
		}
		jwksCacheMu.Lock()
		if len(jwksCache) >= jwksCacheMax {
			for k := range jwksCache { // evict one (map order is arbitrary)
				delete(jwksCache, k)
				break
			}
		}
		jwksCache[url] = fetched
		keys = fetched
		jwksCacheMu.Unlock()
	}
	if key, ok := keys[kid]; ok {
		return key, nil
	}
	// Single-key JWKS documents often omit kid; accept the sole key.
	if len(keys) == 1 {
		for _, key := range keys {
			return key, nil
		}
	}
	return nil, fmt.Errorf("no JWKS key matches kid %q", kid)
}

func fetchJWKS(url string) (map[string]any, error) {
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	resp, err := jwksHTTPClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("JWKS fetch failed: status %d", resp.StatusCode)
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	var set jwkSet
	if err := json.Unmarshal(body, &set); err != nil {
		return nil, err
	}
	out := map[string]any{}
	for _, k := range set.Keys {
		if k.Kty != "RSA" {
			continue // only RSA JWKs are supported by this port
		}
		pub, err := rsaFromJWK(k)
		if err != nil {
			return nil, err
		}
		out[k.Kid] = pub
	}
	if len(out) == 0 {
		return nil, errors.New("JWKS document contained no usable RSA keys")
	}
	return out, nil
}

func rsaFromJWK(k jwkKey) (*rsa.PublicKey, error) {
	nBytes, err := base64.RawURLEncoding.DecodeString(k.N)
	if err != nil {
		return nil, err
	}
	eBytes, err := base64.RawURLEncoding.DecodeString(k.E)
	if err != nil {
		return nil, err
	}
	// Left-pad the exponent to 8 bytes for uint64 decoding.
	padded := make([]byte, 8)
	copy(padded[8-len(eBytes):], eBytes)
	return &rsa.PublicKey{
		N: new(big.Int).SetBytes(nBytes),
		E: int(binary.BigEndian.Uint64(padded)),
	}, nil
}

// resetJWKSCache clears the cache (test hook).
func resetJWKSCache() {
	jwksCacheMu.Lock()
	jwksCache = map[string]map[string]any{}
	jwksCacheMu.Unlock()
}
