//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"strings"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// buildAuthenticator selects and constructs the Authenticator for the configured
// auth mode / identity provider, mirroring the Python app factory
// (build_identity_provider + the dev_token setup in app/auth.py):
//
//   - mode "dev_token": mint a dev token and mutate auth → jwt so the standard
//     JWT validator accepts it, then fall through to the local provider. The
//     returned devToken is non-empty so the CLI can print it.
//   - identity_provider "webhook" with a webhook_idp_url: the delegated webhook
//     identity provider.
//   - otherwise: the local RBAC provider (covers api_key / header / jwt modes).
func buildAuthenticator(
	cfg *serverconfig.UtermServerConfig,
	apiKeys *serverauth.ApiKeyStore,
) (auth serverauth.Authenticator, devToken string, err error) {
	if strings.EqualFold(strings.TrimSpace(cfg.Auth.Mode), "dev_token") {
		devToken, err = serverauth.SetupDevIDP(&cfg.Auth, serverauth.DevIDPOptions{})
		if err != nil {
			return nil, "", err
		}
		// SetupDevIDP mutated Auth.Mode → "jwt"; fall through to the local provider.
	}

	if cfg.Auth.IdentityProvider == "webhook" && cfg.Auth.WebhookIDPURL != nil && *cfg.Auth.WebhookIDPURL != "" {
		secret := ""
		if cfg.Auth.WebhookIDPSecret != nil { // pragma: allowlist secret
			secret = *cfg.Auth.WebhookIDPSecret
		}
		requireSigned := cfg.Auth.WebhookIDPRequireSignedResponse
		wh, werr := serverauth.NewWebhookIdentityProvider(*cfg.Auth.WebhookIDPURL, serverauth.WebhookIDPOptions{
			Secret:                secret,
			TimeoutS:              cfg.Auth.WebhookIDPTimeoutS,
			OnFailure:             cfg.Auth.WebhookIDPOnFailure,
			RequireSignedResponse: &requireSigned,
			ForwardHeaders:        webhookForwardHeaders(cfg),
			ForwardCookies:        webhookForwardCookies(cfg),
			RequireResponseNonce:  cfg.Auth.WebhookIDPRequireResponseNonce,
		})
		if werr != nil {
			return nil, "", werr
		}
		return wh, devToken, nil
	}

	return serverauth.NewLocalIdentityProvider(&cfg.Auth, apiKeys), devToken, nil
}

// webhookForwardHeaders mirrors build_identity_provider's forward-header set:
// the always-needed auth credentials plus configured extensions (lower-cased).
func webhookForwardHeaders(cfg *serverconfig.UtermServerConfig) serverauth.Set {
	set := serverauth.NewSet(
		"authorization", "x-api-key",
		strings.ToLower(cfg.Auth.PrincipalHeader),
		strings.ToLower(cfg.Auth.RoleHeader),
	)
	for _, h := range cfg.Auth.WebhookIDPForwardHeaders {
		set[strings.ToLower(h)] = struct{}{}
	}
	return set
}

// webhookForwardCookies mirrors build_identity_provider's forward-cookie set.
func webhookForwardCookies(cfg *serverconfig.UtermServerConfig) serverauth.Set {
	set := serverauth.NewSet(cfg.Auth.TokenCookie, cfg.Auth.PrincipalCookie, cfg.Auth.RoleCookie)
	for _, c := range cfg.Auth.WebhookIDPForwardCookies {
		set[c] = struct{}{}
	}
	return set
}
