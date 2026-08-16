//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Text;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests.ServerAuth;

public class WebhookAuthorizationTests
{
    private const string Secret = "authz-shared-secret-32-bytes!!"; // pragma: allowlist secret

    private sealed class StubHandler : HttpMessageHandler
    {
        public Func<HttpRequestMessage, Task<HttpResponseMessage>> Responder { get; set; } =
            _ => Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent("""{"allow":true}""", Encoding.UTF8, "application/json"),
            });

        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request,
            CancellationToken cancellationToken) => Responder(request);
    }

    private static Principal AliceAdmin() => new()
    {
        SubjectId = "alice",
        Roles = StringSet.Of("admin"),
    };

    [Fact]
    public void BuildAndVerify_WebhookSignature_RoundTrip()
    {
        var body = Encoding.UTF8.GetBytes("""{"a":1}""");
        const string ts = "1700000000.0";
        var sig = WebhookSigning.BuildWebhookSignature(Secret, body, ts);
        Assert.StartsWith("sha256=", sig, StringComparison.Ordinal);
        Assert.True(WebhookSigning.VerifyWebhookSignature(Secret, body, sig, ts, WebhookSigning.DefaultMaxAgeS, 1700000000.0));
    }

    [Fact]
    public void VerifyWebhookSignature_FailClosed()
    {
        var body = Encoding.UTF8.GetBytes("""{"a":1}""");
        const string ts = "1700000000.0";
        const double now = 1700000000.0;
        var sig = WebhookSigning.BuildWebhookSignature(Secret, body, ts);

        Assert.False(WebhookSigning.VerifyWebhookSignature("", body, sig, ts, WebhookSigning.DefaultMaxAgeS, now));
        Assert.False(WebhookSigning.VerifyWebhookSignature(Secret, body, "", ts, WebhookSigning.DefaultMaxAgeS, now));
        Assert.False(WebhookSigning.VerifyWebhookSignature(Secret, body, sig, "", WebhookSigning.DefaultMaxAgeS, now));
        Assert.False(WebhookSigning.VerifyWebhookSignature(Secret, body, sig, ts, WebhookSigning.DefaultMaxAgeS, now + 10000));
        Assert.False(WebhookSigning.VerifyWebhookSignature(Secret, Encoding.UTF8.GetBytes("""{"a":2}"""), sig, ts, WebhookSigning.DefaultMaxAgeS, now));
        Assert.False(WebhookSigning.VerifyWebhookSignature(Secret, body, sig, "not-a-number", WebhookSigning.DefaultMaxAgeS, now));
        Assert.False(WebhookSigning.VerifyWebhookSignature(Secret, body, "sha256=", ts, WebhookSigning.DefaultMaxAgeS, now));

        // Bare hex (no sha256= prefix) still verifies.
        var bare = sig["sha256=".Length..];
        Assert.True(WebhookSigning.VerifyWebhookSignature(Secret, body, bare, ts, WebhookSigning.DefaultMaxAgeS, now));
    }

    [Fact]
    public void WebhookAuthz_RejectsUnsigned_WhenSecretSet()
    {
        var handler = new StubHandler
        {
            Responder = _ => Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent("""{"allow":true}""", Encoding.UTF8, "application/json"),
            }),
        };
        using var http = new HttpClient(handler) { BaseAddress = new Uri("http://authz.test/") };
        using var p = new WebhookAuthorizationProvider("http://authz.test/check", Secret, 2, http);
        Assert.True(p.RequireSignedResponse);
        Assert.False(p.HasCapability(AliceAdmin(), "session.read"));
    }

    [Fact]
    public void WebhookAuthz_AcceptsSignedAllow()
    {
        var handler = new StubHandler
        {
            Responder = _ =>
            {
                var body = Encoding.UTF8.GetBytes("""{"allow":true}""");
                var ts = WebhookSigning.FormatTimestamp(WebhookSigning.WallClock());
                var resp = new HttpResponseMessage(HttpStatusCode.OK)
                {
                    Content = new ByteArrayContent(body),
                };
                resp.Content.Headers.ContentType = new System.Net.Http.Headers.MediaTypeHeaderValue("application/json");
                resp.Headers.TryAddWithoutValidation("X-Uterm-Timestamp", ts);
                resp.Headers.TryAddWithoutValidation("X-Uterm-Signature", WebhookSigning.BuildWebhookSignature(Secret, body, ts));
                return Task.FromResult(resp);
            },
        };
        using var http = new HttpClient(handler) { BaseAddress = new Uri("http://authz.test/") };
        using var p = new WebhookAuthorizationProvider("http://authz.test/check", Secret, 2, http);
        var prin = AliceAdmin();
        Assert.True(p.HasCapability(prin, "session.read"));
        Assert.True(p.IsAdmin(prin));
        Assert.True(p.CanCreateSession(prin));

        var session = new SessionDefinition { SessionId = "s1", Owner = "alice" };
        Assert.True(p.IsOwner(prin, session));
        Assert.True(p.CanReadSession(prin, session));
        Assert.True(p.CanReadRecording(prin, session));
        Assert.True(p.CanMutateSession(prin, session, "session.control.delete"));
        // Same signed body has no "role" field → resolve_role fails closed to viewer.
        Assert.Equal("viewer", p.ResolveBrowserRole(prin, session));
    }

    [Fact]
    public void WebhookAuthz_NoSecret_AllowsUnsigned()
    {
        var handler = new StubHandler
        {
            Responder = _ => Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent("""{"allow":true}""", Encoding.UTF8, "application/json"),
            }),
        };
        using var http = new HttpClient(handler) { BaseAddress = new Uri("http://authz.test/") };
        using var p = new WebhookAuthorizationProvider("http://authz.test/check", "", 2, http);
        Assert.False(p.RequireSignedResponse);
        Assert.True(p.HasCapability(AliceAdmin(), "session.read"));
    }

    [Fact]
    public void WebhookAuthz_FailClosed_OnHttpError()
    {
        var handler = new StubHandler
        {
            Responder = _ => Task.FromResult(new HttpResponseMessage(HttpStatusCode.InternalServerError)
            {
                Content = new StringContent("""{"allow":true}""", Encoding.UTF8, "application/json"),
            }),
        };
        using var http = new HttpClient(handler);
        using var p = new WebhookAuthorizationProvider("http://authz.test/check", "", 2, http);
        Assert.False(p.HasCapability(AliceAdmin(), "session.read"));
    }

    [Fact]
    public void WebhookAuthz_CapabilitiesFor_ParsesList()
    {
        var handler = new StubHandler
        {
            Responder = _ => Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent("""{"capabilities":["session.read","session.control.create"]}""", Encoding.UTF8, "application/json"),
            }),
        };
        using var http = new HttpClient(handler);
        using var p = new WebhookAuthorizationProvider("http://authz.test/check", "", 2, http);
        var caps = p.CapabilitiesFor(AliceAdmin());
        Assert.True(caps.Has("session.read"));
        Assert.True(caps.Has("session.control.create"));
        Assert.False(caps.Has("session.control.hijack"));
    }

    [Fact]
    public void WebhookAuthz_ResolveBrowserRole_ParsesRole()
    {
        var handler = new StubHandler
        {
            Responder = _ => Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent("""{"role":"operator"}""", Encoding.UTF8, "application/json"),
            }),
        };
        using var http = new HttpClient(handler);
        using var p = new WebhookAuthorizationProvider("http://authz.test/check", "", 2, http);
        var role = p.ResolveBrowserRole(AliceAdmin(), new SessionDefinition { SessionId = "s1" });
        Assert.Equal("operator", role);
    }

    [Fact]
    public void WebhookAuthz_ResolveBrowserRole_InvalidDefaultsViewer()
    {
        var handler = new StubHandler
        {
            Responder = _ => Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent("""{"role":"superuser"}""", Encoding.UTF8, "application/json"),
            }),
        };
        using var http = new HttpClient(handler);
        using var p = new WebhookAuthorizationProvider("http://authz.test/check", "", 2, http);
        Assert.Equal("viewer", p.ResolveBrowserRole(AliceAdmin(), new SessionDefinition { SessionId = "s1" }));
    }

    [Fact]
    public void WebhookAuthz_SignsRequest_WhenSecretSet()
    {
        string? gotSig = null;
        string? gotTs = null;
        byte[]? gotBody = null;
        const double frozen = 1700000000.0;
        var handler = new StubHandler
        {
            Responder = async req =>
            {
                gotBody = await req.Content!.ReadAsByteArrayAsync();
                if (req.Headers.TryGetValues("X-Uterm-Signature", out var sigs)) gotSig = sigs.First();
                if (req.Headers.TryGetValues("X-Uterm-Timestamp", out var tss)) gotTs = tss.First();

                // Sign response with the same frozen clock the provider uses for verify.
                var body = Encoding.UTF8.GetBytes("""{"allow":true}""");
                var ts = WebhookSigning.FormatTimestamp(frozen);
                var resp = new HttpResponseMessage(HttpStatusCode.OK) { Content = new ByteArrayContent(body) };
                resp.Headers.TryAddWithoutValidation("X-Uterm-Timestamp", ts);
                resp.Headers.TryAddWithoutValidation("X-Uterm-Signature", WebhookSigning.BuildWebhookSignature(Secret, body, ts));
                return resp;
            },
        };
        using var http = new HttpClient(handler);
        using var p = new WebhookAuthorizationProvider("http://authz.test/check", Secret, 2, http)
        {
            Now = () => frozen,
        };
        Assert.True(p.HasCapability(AliceAdmin(), "session.read"));
        Assert.False(string.IsNullOrEmpty(gotSig));
        Assert.False(string.IsNullOrEmpty(gotTs));
        Assert.NotNull(gotBody);
        Assert.True(WebhookSigning.VerifyWebhookSignature(Secret, gotBody, gotSig, gotTs, WebhookSigning.DefaultMaxAgeS, frozen));
    }

    [Fact]
    public void AuthorizationService_FromConfig_NilOrEmpty_IsLocal()
    {
        var local = AuthorizationService.FromConfig(null);
        var admin = AliceAdmin();
        Assert.True(local.IsAdmin(admin));
        Assert.True(local.HasCapability(admin, "session.control.hijack"));

        var cfg = UtermServerConfig.Default();
        var stillLocal = AuthorizationService.FromConfig(cfg);
        Assert.True(stillLocal.IsAdmin(admin));
    }

    [Fact]
    public void AuthorizationService_FromConfig_UsesWebhook()
    {
        var handler = new StubHandler
        {
            Responder = _ => Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent("""{"allow":false}""", Encoding.UTF8, "application/json"),
            }),
        };
        // Build service with injected webhook provider (FromConfig creates its own HttpClient;
        // exercise FromConfig wiring + deny path via explicit provider).
        var cfg = UtermServerConfig.Default();
        cfg.Governance.AuthzWebhookUrl = "http://authz.test/check";
        cfg.Governance.AuthzWebhookSecret = "";
        cfg.Governance.AuthzWebhookTimeoutS = 1.5;

        // FromConfig path with real handler via custom provider construction mirrors factory.
        using var http = new HttpClient(handler);
        var provider = new WebhookAuthorizationProvider(cfg.Governance.AuthzWebhookUrl, "", cfg.Governance.AuthzWebhookTimeoutS, http);
        var svc = new AuthorizationService(provider);
        Assert.False(svc.HasCapability(AliceAdmin(), "session.read"));

        // Confirm FromConfig selects webhook type (deny on unreachable URL = fail closed).
        cfg.Governance.AuthzWebhookUrl = "http://127.0.0.1:1/no-listener";
        var remote = AuthorizationService.FromConfig(cfg);
        Assert.False(remote.HasCapability(AliceAdmin(), "session.read"));
    }

    [Fact]
    public void AuthorizationService_HasRole_NeverDelegated()
    {
        var handler = new StubHandler
        {
            Responder = _ => Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent("""{"allow":false}""", Encoding.UTF8, "application/json"),
            }),
        };
        using var http = new HttpClient(handler);
        using var p = new WebhookAuthorizationProvider("http://authz.test/check", "", 2, http);
        var svc = new AuthorizationService(p);
        Assert.True(svc.HasRole(AliceAdmin(), "admin"));
        Assert.False(svc.IsAdmin(AliceAdmin())); // delegated → allow:false
    }

    [Fact]
    public void Load_Governance_FromToml()
    {
        var tmp = Path.Combine(Path.GetTempPath(), "uterm-gov-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(tmp, """
            [governance]
            authz_webhook_url = "http://127.0.0.1:9999/authz"
            authz_webhook_secret = "s3cret-key-for-tests-only!!!!" # pragma: allowlist secret
            authz_webhook_timeout_s = 3.5
            """);
        try
        {
            var cfg = ConfigLoader.Load(tmp);
            Assert.Equal("http://127.0.0.1:9999/authz", cfg.Governance.AuthzWebhookUrl);
            Assert.Equal("s3cret-key-for-tests-only!!!!", cfg.Governance.AuthzWebhookSecret);
            Assert.Equal(3.5, cfg.Governance.AuthzWebhookTimeoutS);
        }
        finally
        {
            File.Delete(tmp);
        }
    }

    [Fact]
    public void WebhookAuthz_BadJson_FailClosed()
    {
        var handler = new StubHandler
        {
            Responder = _ => Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent("not-json", Encoding.UTF8, "application/json"),
            }),
        };
        using var http = new HttpClient(handler);
        using var bad = new WebhookAuthorizationProvider("http://authz.test/check", "", 2, http);
        Assert.False(bad.HasCapability(AliceAdmin(), "session.read"));
        Assert.Empty(bad.CapabilitiesFor(AliceAdmin()));
        Assert.Equal("viewer", bad.ResolveBrowserRole(AliceAdmin(), new SessionDefinition { SessionId = "x" }));
    }

    [Fact]
    public void WebhookAuthz_DefaultTimeout_AndToString()
    {
        using var p = new WebhookAuthorizationProvider("http://authz.test/check", "x", 0);
        Assert.Equal(2.0, p.TimeoutS);
        Assert.Contains("http://authz.test/check", p.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public void LocalAuthorizationService_StillWorks()
    {
        var svc = new AuthorizationService();
        var admin = AliceAdmin();
        Assert.True(svc.IsAdmin(admin));
        Assert.True(svc.HasCapability(admin, "session.control.hijack"));
        Assert.True(svc.CanCreateSession(admin));
        var session = new SessionDefinition { SessionId = "s1", Visibility = "private", Owner = "alice" };
        Assert.True(svc.CanReadSession(admin, session));
        Assert.Equal("admin", svc.ResolveBrowserRole(admin, session));
    }
}
