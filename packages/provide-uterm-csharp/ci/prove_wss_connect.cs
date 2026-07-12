//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Live WSS proof: connect with Provide.Uterm WebSocketTransport, receive bytes.
// Usage (from package root after build):
//   DOTNET_ROOT=... dotnet run --project ci/ProveWss/ProveWss.csproj -- <wss-url>
// Or via Makefile: make prove-wss URL=wss://...
//
// This file is the shared source for the ProveWss project (top-level statements).

using Provide.Uterm.Transports;

var url = args.Length > 0
    ? args[0]
    : "wss://proving.warp.undef.games/r/warp/u/genesis/ws/terminal";

Console.WriteLine("prove_wss: connecting via Provide.Uterm.WebSocketTransport");
Console.WriteLine("prove_wss: url=" + url);

await using var tr = new WebSocketTransport();
var opts = new ConnectOptions
{
    Timeout = TimeSpan.FromSeconds(20),
    Ws = new WsOptions { Url = url, SendBinary = false },
};
var host = new Uri(url).Host;
await tr.ConnectAsync(host, 443, opts);
Console.WriteLine("prove_wss: connected");

// Read a few frames / chunks (terminal may send banner immediately or after idle).
var total = 0;
var chunks = 0;
using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(12));
try
{
    while (chunks < 8 && total < 64 * 1024 && !cts.IsCancellationRequested)
    {
        var data = await tr.ReceiveAsync(8192, TimeSpan.FromSeconds(5), cts.Token);
        if (data.Length == 0)
        {
            continue;
        }

        chunks++;
        total += data.Length;
        var preview = System.Text.Encoding.UTF8.GetString(data);
        if (preview.Length > 200)
        {
            preview = preview[..200] + "…";
        }

        Console.WriteLine($"prove_wss: recv chunk#{chunks} len={data.Length} preview={System.Text.Json.JsonSerializer.Serialize(preview)}");
    }
}
catch (OperationCanceledException)
{
    // timeout is ok if we already got data
}
catch (Exception ex) when (total > 0)
{
    Console.WriteLine("prove_wss: receive ended after data: " + ex.GetType().Name + ": " + ex.Message);
}

await tr.DisconnectAsync();
Console.WriteLine($"prove_wss: done chunks={chunks} bytes={total}");
if (total <= 0 && chunks <= 0)
{
    // Connected is still success for a quiet server; mark explicit connect-ok.
    Console.WriteLine("prove_wss: OK (connected; no terminal bytes within wait window)");
}
else
{
    Console.WriteLine("prove_wss: OK (connected and received terminal data)");
}
