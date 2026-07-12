//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.Http;

namespace Provide.Uterm.Shell;

/// <summary>
/// Parses and dispatches ushell command lines.
/// Port of packages/provide-uterm-go/shell/dispatcher.go.
/// </summary>
public sealed class CommandDispatcher
{
    private readonly ShellContext _ctx;
    private readonly HttpClient _client;
    private readonly Func<byte[], int, int, string, (IReadOnlyList<string> Frames, double Fps)>? _renderImage;
    private readonly bool _ownsClient;

    public CommandDispatcher(
        ShellContext? ctx = null,
        HttpClient? client = null,
        Func<byte[], int, int, string, (IReadOnlyList<string> Frames, double Fps)>? renderImage = null)
    {
        _ctx = ctx ?? new ShellContext();
        _ownsClient = client is null;
        _client = client ?? new HttpClient();
        _renderImage = renderImage;
    }

    /// <summary>Synchronous dispatch for simple commands (tests / line-oriented callers).</summary>
    public ShellResult Dispatch(string line) =>
        DispatchAsync(line).GetAwaiter().GetResult();

    public async Task<ShellResult> DispatchAsync(string line, CancellationToken ct = default)
    {
        line = StrUtil.PyStrip(line);
        if (line.Length == 0 || line == "\x03")
        {
            return ShellResult.OfText(ShellOutput.Prompt);
        }

        var parts = StrUtil.PySplit1(line);
        if (parts is null || parts.Length == 0)
        {
            return ShellResult.OfText(ShellOutput.Prompt);
        }

        var cmd = parts[0].ToLowerInvariant();
        var arg = parts.Length > 1 ? StrUtil.PyStrip(parts[1]) : "";

        switch (cmd)
        {
            case "exit":
            case "quit":
            case "\x04":
                return ShellResult.OfText(ShellOutput.InfoMsg("Goodbye.\r\n") + ShellOutput.Prompt);

            case "help":
                if (arg.Length > 0)
                {
                    if (!Help.CommandHelp.TryGetValue(arg.ToLowerInvariant(), out var detail))
                    {
                        return ShellResult.OfText(ShellOutput.ErrorMsg("no help for '" + arg + "'") + ShellOutput.Prompt);
                    }

                    return ShellResult.OfText(detail + ShellOutput.Prompt);
                }

                return ShellResult.OfText(Help.HelpText + ShellOutput.Prompt);

            case "clear":
                return ShellResult.OfText(ShellOutput.ClearScreen + ShellOutput.Prompt);

            case "py":
                return Commands.CmdPy(arg);

            case "sessions":
                if (arg.StartsWith("kill ", StringComparison.Ordinal) || arg == "kill")
                {
                    var id = arg.StartsWith("kill ", StringComparison.Ordinal) ? StrUtil.PyStrip(arg[5..]) : "";
                    return await Commands.CmdSessionsKillAsync(_ctx, id, ct).ConfigureAwait(false);
                }

                return await Commands.CmdSessionsAsync(_ctx, ct).ConfigureAwait(false);

            case "kv":
                return await Commands.CmdKvAsync(_ctx, arg, ct).ConfigureAwait(false);

            case "fetch":
                return await Commands.CmdFetchAsync(_client, arg, ct).ConfigureAwait(false);

            case "storage":
                return await Commands.CmdStorageAsync(_ctx, arg, ct).ConfigureAwait(false);

            case "env":
                return CmdEnv();

            case "render":
                return await Commands.CmdRenderAsync(_client, _renderImage, arg, ct).ConfigureAwait(false);

            case "cast":
                return await Commands.CmdCastAsync(_client, arg, ct).ConfigureAwait(false);

            default:
                return ShellResult.OfText(
                    ShellOutput.ErrorMsg("unknown command: '" + cmd + "' — type " + ShellOutput.Bold + "help" + ShellOutput.Reset) +
                    ShellOutput.Prompt);
        }
    }

    private ShellResult CmdEnv()
    {
        var lines = new List<string>();
        if (_ctx.Env is not null)
        {
            var attrs = _ctx.Env.Attrs();
            foreach (var name in attrs.Keys.OrderBy(k => k, StringComparer.Ordinal))
            {
                lines.Add(ShellOutput.FmtKvDefault(name, attrs[name]));
            }
        }
        else
        {
            foreach (var k in _ctx.Values.Keys.Where(k => !k.StartsWith('_')).OrderBy(k => k, StringComparer.Ordinal))
            {
                lines.Add(ShellOutput.FmtKvDefault(k, ""));
            }
        }

        var output = lines.Count > 0
            ? ShellOutput.Heading("context") + string.Join("", lines)
            : ShellOutput.InfoMsg("(empty context)");
        return ShellResult.OfText(output + ShellOutput.Prompt);
    }
}
