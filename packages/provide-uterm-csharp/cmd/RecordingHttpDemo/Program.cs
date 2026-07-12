//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Thin server recording surface demo (C#).
// Starts an in-process UtermServer, seeds a LocalFileStore session, then
// exercises POST annotate + GET recording / entries / download — same HTTP
// contract as Python routes/sessions.py and the Go server_recording surface.

using System.Net;
using System.Net.Sockets;
using System.Text;
using Provide.Uterm.Hub;
using Provide.Uterm.Recording;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

const string magenta = "\u001b[1;35m";
const string green = "\u001b[1;32m";
const string cyan = "\u001b[1;36m";
const string dim = "\u001b[2m";
const string reset = "\u001b[0m";
const string bold = "\u001b[1m";

static void Banner(string title)
{
    var bar = new string('═', title.Length + 4);
    Console.WriteLine();
    Console.WriteLine($"{magenta}{bar}{reset}");
    Console.WriteLine($"{magenta}  {bold}{title}{reset}{magenta}  {reset}");
    Console.WriteLine($"{magenta}{bar}{reset}");
    Console.WriteLine();
}

static void Info(string msg) => Console.WriteLine($"{cyan}  → {msg}{reset}");
static void Ok(string msg) => Console.WriteLine($"{green}  ✓ {msg}{reset}");
static void Kv(string key, object? value) =>
    Console.WriteLine($"    {dim}{key}:{reset} {bold}{value}{reset}");

static string Trim(string s, int n)
{
    s = s.Replace('\n', ' ');
    return s.Length > n ? s[..n] + "…" : s;
}

static int FreePort()
{
    var l = new TcpListener(IPAddress.Loopback, 0);
    l.Start();
    var port = ((IPEndPoint)l.LocalEndpoint).Port;
    l.Stop();
    return port;
}

Banner("provide-uterm recording HTTP — C#");
Info("surface=annotate+meta+entries+download  store=LocalFileStore");

var tmp = Path.Combine(Path.GetTempPath(), "uterm-rec-http-cs-" + Guid.NewGuid().ToString("N")[..8]);
Directory.CreateDirectory(tmp);
const string sid = "demo-http-cs";

try
{
    using var store = new LocalFileStore(tmp);
    await store.StartSessionAsync(sid, new Dictionary<string, object?>
    {
        ["lang"] = "csharp",
        ["feature"] = "recording_http",
        ["demo"] = "thin_server_surface",
    });
    await store.AppendEventsAsync(sid, new[]
    {
        new Event
        {
            ["ts"] = 1.0,
            ["event"] = "snapshot",
            ["data"] = new Dictionary<string, object?> { ["screen"] = "=== recording HTTP demo ===\n" },
            ["session_id"] = sid,
        },
        new Event
        {
            ["ts"] = 2.0,
            ["event"] = "output",
            ["data"] = "hello from csharp\n",
            ["session_id"] = sid,
        },
    });
    Ok("seeded JSONL under " + tmp);

    var port = FreePort();
    var cfg = UtermServerConfig.Default();
    cfg.Server.Host = "127.0.0.1";
    cfg.Server.Port = port;
    cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
    cfg.Auth.Mode = "dev_token";
    cfg.Recording.StoreType = "local";
    cfg.Recording.Directory = tmp;
    cfg.Sessions.Add(new SessionDefinition
    {
        SessionId = sid,
        DisplayName = sid,
        ConnectorType = "shell",
        Visibility = "public",
        Owner = "demo-admin",
    });

    var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
    {
        TokenPath = Path.Combine(tmp, "dev.token"),
        Subject = "demo-admin",
        Roles = new[] { "admin" },
    });

    var clock = new RealClock();
    var server = new UtermServer(new ServerDeps
    {
        Hub = new TermHub(new TermHubConfig { Clock = clock }),
        Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
        Authz = new AuthorizationService(),
        Config = cfg,
        Registry = new InMemorySessionRegistry(cfg.Sessions),
        Version = "demo",
        Clock = clock,
        Recording = store,
    });
    server.Build(new[] { $"http://127.0.0.1:{port}" });
    await server.StartAsync();
    await using (server)
    {
        var baseUrl = $"http://127.0.0.1:{port}";
        Ok("server listening " + baseUrl);

        using var client = new HttpClient { BaseAddress = new Uri(baseUrl) };
        client.DefaultRequestHeaders.Authorization =
            new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", token);

        async Task<(int Code, string Body)> Do(HttpMethod method, string path, string? json = null)
        {
            using var req = new HttpRequestMessage(method, path);
            if (json is not null)
            {
                req.Content = new StringContent(json, Encoding.UTF8, "application/json");
            }

            using var resp = await client.SendAsync(req);
            var body = await resp.Content.ReadAsStringAsync();
            return ((int)resp.StatusCode, body);
        }

        var (code, body) = await Do(HttpMethod.Get, $"/api/sessions/{sid}/recording");
        Kv("GET /recording", $"{code} {Trim(body, 120)}");
        if (code != 200) Environment.Exit(1);
        Ok("recording meta");

        (code, body) = await Do(HttpMethod.Get, $"/api/sessions/{sid}/recording/entries?limit=10");
        Kv("GET /recording/entries", $"{code} bytes={body.Length}");
        if (code != 200) Environment.Exit(1);
        Ok("recording entries");

        (code, body) = await Do(HttpMethod.Post, $"/api/sessions/{sid}/annotate",
            """{"label":"http-demo","description":"thin surface","severity":"info"}""");
        Kv("POST /annotate", $"{code} {Trim(body, 80)}");
        if (code != 200) Environment.Exit(1);
        Ok("annotate");

        (code, body) = await Do(HttpMethod.Get, $"/api/sessions/{sid}/recording/download");
        Kv("GET /recording/download", $"{code} bytes={body.Length}");
        if (code != 200) Environment.Exit(1);
        var firstLine = body.Split('\n', 2)[0];
        Kv("jsonl[0]", Trim(firstLine, 100));
        Ok("download JSONL from " + Path.Combine(tmp, sid + ".jsonl"));

        Console.WriteLine();
        Ok("thin recording HTTP surface demo complete (C#)");
    }
}
finally
{
    try { Directory.Delete(tmp, true); } catch { /* best effort */ }
}
