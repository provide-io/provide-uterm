//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.ServerAuth;
using static Provide.Uterm.Tests.ServerIntegrationViewerRevocationTests;

namespace Provide.Uterm.Tests;

/// <summary>
/// POST /api/sessions/{id}/viewers/revoke with <c>{"subject": ...}</c> against live
/// sockets: only that share subject's socket is closed with 4410 and only its
/// tokens refused; a token minted for it after the cutoff watches again. Port of
/// tests/server/test_viewer_subject_revocation.py's end-to-end half.
/// </summary>
public sealed class ServerIntegrationViewerSubjectRevocationTests
{
    private const string Alice = "share:bb:blackbetty:7";

    private static async Task<HttpResponseMessage> PostAsync(
        int port, byte[]? body, string subject = "admin1", string role = "admin")
    {
        using var http = new HttpClient();
        using var req = new HttpRequestMessage(HttpMethod.Post, $"http://127.0.0.1:{port}/api/sessions/bb/viewers/revoke");
        Identify((k, v) => req.Headers.TryAddWithoutValidation(k, v), subject, role, 100);
        if (body is not null) req.Content = new ByteArrayContent(body);
        return await http.SendAsync(req);
    }

    private static byte[] Utf8Body(string text) => Encoding.UTF8.GetBytes(text);

    private static byte[] SubjectBody(string subject) =>
        JsonSerializer.SerializeToUtf8Bytes(new Dictionary<string, string> { ["subject"] = subject });

    [Fact]
    public async Task Revoking_one_subject_closes_only_its_socket_and_lets_a_fresh_token_rejoin()
    {
        var (server, port) = await BootAsync();
        try
        {
            using var alice = await ConnectAsync(port, Alice, "viewer", 100);
            using var bob = await ConnectAsync(port, "share:bb:blackbetty:8", "viewer", 100);
            Assert.NotNull(await ReadUntilAsync(alice, "\"hello\""));
            Assert.NotNull(await ReadUntilAsync(bob, "\"hello\""));

            var response = await PostAsync(port, SubjectBody(Alice));
            Assert.Equal(HttpStatusCode.OK, response.StatusCode);
            using var body = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
            var root = body.RootElement;
            Assert.Equal(6, root.EnumerateObject().Count());
            Assert.Equal(Alice, root.GetProperty("subject").GetString());
            Assert.Equal("bb", root.GetProperty("session_id").GetString());
            Assert.Equal(1, root.GetProperty("closed").GetInt32());
            Assert.Equal(4410, root.GetProperty("close_code").GetInt32());
            Assert.Equal("viewer access revoked", root.GetProperty("close_reason").GetString());
            var revokedAt = root.GetProperty("revoked_at").GetDouble();

            Assert.Null(await ReadUntilAsync(alice, "never-sent"));
            Assert.Equal((WebSocketCloseStatus)4410, alice.CloseStatus);
            Assert.Equal("viewer access revoked", alice.CloseStatusDescription);
            var ping = Encoding.UTF8.GetBytes(ControlChannelCodec.EncodeControlFrame(
                new Dictionary<string, object?> { ["type"] = "ping" }));
            await bob.SendAsync(ping, WebSocketMessageType.Text, true, CancellationToken.None);
            Assert.NotNull(await ReadUntilAsync(bob, "\"pong\""));

            var refused = await Assert.ThrowsAsync<WebSocketException>(() => ConnectAsync(port, Alice, "viewer", 100));
            Assert.Contains("403", refused.Message, StringComparison.Ordinal);
            using var fresh = await ConnectAsync(port, Alice, "viewer", revokedAt + 1);
            Assert.NotNull(await ReadUntilAsync(fresh, "\"hello\""));
        }
        finally
        {
            await server.StopAsync();
        }
    }

    [Fact]
    public async Task No_body_a_blank_one_null_or_no_subject_still_revokes_the_whole_session()
    {
        foreach (var body in new[] { null, Utf8Body(" \t\r\n"), Utf8Body("{}"), Utf8Body("null"), Utf8Body(" null\n") })
        {
            var authz = new AuthorizationService();
            var (server, port) = await BootAsync(authz);
            try
            {
                var response = await PostAsync(port, body);
                Assert.Equal(HttpStatusCode.OK, response.StatusCode);
                using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
                Assert.Equal(doc.RootElement.GetProperty("revoked_at").GetDouble(), authz.ShareViewerRevocation("bb"));
                // A whole-session revoke keeps the original answer: no subject key.
                Assert.Equal(
                    new[] { "close_code", "close_reason", "closed", "revoked_at", "session_id" },
                    doc.RootElement.EnumerateObject().Select(field => field.Name).Order(StringComparer.Ordinal));
            }
            finally
            {
                await server.StopAsync();
            }
        }
    }

    public static TheoryData<byte[]> MalformedBodies() => new()
    {
        Utf8Body("not json"),
        Utf8Body("{\"subject\": "),
        Utf8Body("[]"),
        // Past System.Text.Json's depth limit of 64, yet under the body cap.
        Utf8Body(new string('[', 2000) + new string(']', 2000)),
        Utf8Body("\"share:bb:x\""),
        Utf8Body("{\"subject\": null}"),
        Utf8Body("{\"subject\": 7}"),
        Utf8Body("{\"subject\": \"share:bb:x\", \"extra\": 1}"),
        Utf8Body("{\"subjects\": \"share:bb:x\"}"),
        new byte[] { 0xff },
        Utf8Body("{\"subject\": \"share:bb:").Concat(new byte[] { 0xff, (byte)'"', (byte)'}' }).ToArray(),
        Utf8Body("{} {}"),
        Utf8Body("X"),
        // A form feed, a no-break space and a byte-order mark: not JSON whitespace.
        Utf8Body("\f"),
        Utf8Body("\u00a0"),
        Encoding.UTF8.Preamble.ToArray().Concat(Utf8Body("{}")).ToArray(),
        Encoding.Unicode.GetBytes("{}"),
    };

    [Theory]
    [MemberData(nameof(MalformedBodies))]
    public async Task A_malformed_body_is_a_422_that_revokes_nothing(byte[] body)
    {
        var authz = new AuthorizationService();
        var (server, port) = await BootAsync(authz);
        try
        {
            var response = await PostAsync(port, body);
            Assert.Equal(HttpStatusCode.UnprocessableEntity, response.StatusCode);
            Assert.Equal("{\"detail\":\"invalid request body\"}", await response.Content.ReadAsStringAsync());
            Assert.Null(authz.ShareViewerRevocation("bb"));
        }
        finally
        {
            await server.StopAsync();
        }
    }

    [Theory]
    [InlineData("share:other:7")]
    [InlineData("share:bbx:7")]
    [InlineData("share:bb")]
    [InlineData("share:bb:")]
    [InlineData("alice")]
    public async Task A_subject_that_is_not_this_sessions_share_subject_is_a_422(string subject)
    {
        var authz = new AuthorizationService();
        var (server, port) = await BootAsync(authz);
        try
        {
            var response = await PostAsync(port, SubjectBody(subject));
            Assert.Equal(HttpStatusCode.UnprocessableEntity, response.StatusCode);
            Assert.Equal(
                "{\"detail\":\"subject must be a share subject of this session\"}",
                await response.Content.ReadAsStringAsync());
            Assert.Null(authz.ShareViewerSubjectRevocation("bb", subject));
        }
        finally
        {
            await server.StopAsync();
        }
    }

    [Theory]
    [InlineData("\\ud83d\\ude00", HttpStatusCode.OK)]
    [InlineData("\\\\ud800", HttpStatusCode.OK)]
    [InlineData("\\u0041", HttpStatusCode.OK)]
    [InlineData("\\ud800", HttpStatusCode.UnprocessableEntity)]
    [InlineData("\\udc00", HttpStatusCode.UnprocessableEntity)]
    [InlineData("\\ud800x", HttpStatusCode.UnprocessableEntity)]
    [InlineData("\\ud800\\u0041", HttpStatusCode.UnprocessableEntity)]
    [InlineData("\\ud800\\ud800", HttpStatusCode.UnprocessableEntity)]
    [InlineData("\\ude00\\ud83d", HttpStatusCode.UnprocessableEntity)]
    public async Task A_subject_spelling_a_lone_surrogate_is_malformed(string spelling, HttpStatusCode status)
    {
        var (server, port) = await BootAsync();
        try
        {
            var response = await PostAsync(port, Utf8Body("{\"subject\": \"share:bb:" + spelling + "\"}"));
            Assert.Equal(status, response.StatusCode);
        }
        finally
        {
            await server.StopAsync();
        }
    }

    [Fact]
    public async Task A_session_full_of_revoked_subjects_answers_429_and_still_revokes_them_all()
    {
        var authz = new AuthorizationService();
        var (server, port) = await BootAsync(authz);
        try
        {
            var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
            for (var n = 0; n < AuthorizationService.ShareViewerSubjectRevocationsPerSessionMax; n++)
            {
                authz.RevokeShareViewerSubject("bb", $"share:bb:{n}", now);
            }
            var response = await PostAsync(port, SubjectBody(Alice));
            Assert.Equal((HttpStatusCode)429, response.StatusCode);
            Assert.Equal(
                "{\"detail\":\"too many viewers of this session revoked one by one; revoke them all instead\"}",
                await response.Content.ReadAsStringAsync());
            Assert.Equal(HttpStatusCode.OK, (await PostAsync(port, null)).StatusCode);
        }
        finally
        {
            await server.StopAsync();
        }
    }

    [Fact]
    public async Task A_refused_caller_is_refused_before_its_body_is_judged()
    {
        var (server, port) = await BootAsync();
        try
        {
            var response = await PostAsync(port, Utf8Body("not json"), "alice", "viewer");
            Assert.Equal(HttpStatusCode.Forbidden, response.StatusCode);
        }
        finally
        {
            await server.StopAsync();
        }
    }

    [Fact]
    public async Task Deleting_a_session_forgets_its_subject_cutoffs()
    {
        var authz = new AuthorizationService();
        var (server, port) = await BootAsync(authz);
        try
        {
            Assert.Equal(HttpStatusCode.OK, (await PostAsync(port, SubjectBody(Alice))).StatusCode);
            using var http = new HttpClient();
            using var req = new HttpRequestMessage(HttpMethod.Delete, $"http://127.0.0.1:{port}/api/sessions/bb");
            Identify((k, v) => req.Headers.TryAddWithoutValidation(k, v), "admin1", "admin", 100);
            Assert.Equal(HttpStatusCode.OK, (await http.SendAsync(req)).StatusCode);
            Assert.Null(authz.ShareViewerSubjectRevocation("bb", Alice));
        }
        finally
        {
            await server.StopAsync();
        }
    }

    [Fact]
    public async Task A_body_past_the_cap_is_a_422_and_one_at_it_is_read()
    {
        var (server, port) = await BootAsync();
        try
        {
            var cap = AuthorizationService.ShareViewerRevokeBodyMaxBytes;
            var over = await PostAsync(port, Utf8Body(new string(' ', cap - 1) + "{}"));
            Assert.Equal(HttpStatusCode.UnprocessableEntity, over.StatusCode);
            Assert.Equal("{\"detail\":\"invalid request body\"}", await over.Content.ReadAsStringAsync());
            var at = await PostAsync(port, Utf8Body(new string(' ', cap - 2) + "{}"));
            Assert.Equal(HttpStatusCode.OK, at.StatusCode);
            Assert.Equal(4096, cap);
        }
        finally
        {
            await server.StopAsync();
        }
    }

    [Fact]
    public async Task A_full_subject_table_answers_429()
    {
        var authz = new AuthorizationService();
        var (server, port) = await BootAsync(authz);
        try
        {
            var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
            const int per = AuthorizationService.ShareViewerSubjectRevocationsPerSessionMax;
            for (var n = 0; n < AuthorizationService.ShareViewerSubjectRevocationsMax; n++)
            {
                authz.RevokeShareViewerSubject($"s{n / per}", $"share:s{n / per}:{n % per}", now);
            }
            var response = await PostAsync(port, SubjectBody(Alice));
            Assert.Equal((HttpStatusCode)429, response.StatusCode);
            Assert.Equal(
                "{\"detail\":\"too many viewers revoked one by one; try again later\"}",
                await response.Content.ReadAsStringAsync());
        }
        finally
        {
            await server.StopAsync();
        }
    }

    [Fact]
    public async Task A_whole_session_revoke_frees_the_sessions_subject_slots()
    {
        var authz = new AuthorizationService();
        var (server, port) = await BootAsync(authz);
        try
        {
            var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
            for (var n = 0; n < AuthorizationService.ShareViewerSubjectRevocationsPerSessionMax; n++)
            {
                authz.RevokeShareViewerSubject("bb", $"share:bb:{n}", now);
            }
            Assert.Equal(HttpStatusCode.OK, (await PostAsync(port, null)).StatusCode);
            Assert.Equal(HttpStatusCode.OK, (await PostAsync(port, SubjectBody(Alice))).StatusCode);
        }
        finally
        {
            await server.StopAsync();
        }
    }

    [Fact]
    public async Task Health_advertises_the_subject_revoke_feature()
    {
        var (server, port) = await BootAsync();
        try
        {
            using var http = new HttpClient();
            using var health = JsonDocument.Parse(await http.GetStringAsync($"http://127.0.0.1:{port}/api/health"));
            var root = health.RootElement;
            Assert.Equal(
                new[] { "active_sessions", "control_plane_backend", "features", "ok", "ready", "service", "status", "uptime_s", "version" },
                root.EnumerateObject().Select(field => field.Name).Order(StringComparer.Ordinal));
            Assert.Equal("ok", root.GetProperty("status").GetString());
            Assert.True(root.GetProperty("ok").GetBoolean());
            Assert.True(root.GetProperty("ready").GetBoolean());
            Assert.Equal("uterm-server", root.GetProperty("service").GetString());
            Assert.Equal("revoke", root.GetProperty("version").GetString());
            Assert.Equal("memory", root.GetProperty("control_plane_backend").GetString());
            Assert.Equal("[\"viewers.revoke.subject\"]", root.GetProperty("features").GetRawText());
        }
        finally
        {
            await server.StopAsync();
        }
    }
}
