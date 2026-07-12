//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;

namespace Provide.Uterm.Shell;

/// <summary>
/// Basic ushell line buffer + ANSI output helpers.
/// Port of packages/provide-uterm-go/shell (linebuffer + output surface).
/// </summary>
public sealed class LineBuffer
{
    private readonly StringBuilder _buf = new();
    public int MaxLength { get; set; } = 4096;

    public string Text => _buf.ToString();

    /// <summary>
    /// Feed a keystroke chunk. Returns a completed line when the user submits,
    /// or null while still editing. Ctrl-C returns "\x03"; empty Ctrl-D returns "".
    /// </summary>
    public string? Feed(string chunk)
    {
        var i = 0;
        while (i < chunk.Length)
        {
            var ch = chunk[i];
            if (ch == '\x1b')
            {
                i++;
                if (i < chunk.Length && chunk[i] == '[')
                {
                    i++;
                    while (i < chunk.Length && chunk[i] is < '@' or > '~')
                    {
                        i++;
                    }

                    if (i < chunk.Length)
                    {
                        i++;
                    }

                    continue;
                }

                if (i < chunk.Length && chunk[i] == 'O')
                {
                    i += 2;
                    continue;
                }

                continue;
            }

            if (ch is '\r' or '\n')
            {
                // swallow CRLF pair
                if (ch == '\r' && i + 1 < chunk.Length && chunk[i + 1] == '\n')
                {
                    i++;
                }

                var line = _buf.ToString();
                _buf.Clear();
                return line;
            }

            if (ch is '\x7f' or '\x08')
            {
                if (_buf.Length > 0)
                {
                    _buf.Length--;
                }

                i++;
                continue;
            }

            if (ch == '\x03')
            {
                _buf.Clear();
                return "\x03";
            }

            if (ch == '\x04')
            {
                if (_buf.Length == 0)
                {
                    return "";
                }

                i++;
                continue;
            }

            if (ch == '\t' || !char.IsControl(ch))
            {
                if (_buf.Length < MaxLength)
                {
                    _buf.Append(ch);
                }
            }

            i++;
        }

        return null;
    }

    public void Clear() => _buf.Clear();
}

public static class ShellOutput
{
    public const string Reset = "\x1b[0m";
    public const string Bold = "\x1b[1m";
    public const string Red = "\x1b[31m";
    public const string Green = "\x1b[32m";
    public const string Yellow = "\x1b[33m";
    public const string Cyan = "\x1b[36m";
    public const string Prompt = Cyan + "ushell> " + Reset;
    public const string Banner = Bold + "provide-uterm ushell" + Reset + "\r\n";

    public static string ErrorMsg(string msg) => Red + "error: " + msg + Reset + "\r\n";
    public static string InfoMsg(string msg) => Cyan + msg + Reset + "\r\n";
    public static string SuccessMsg(string msg) => Green + msg + Reset + "\r\n";
    public static string Heading(string msg) => Bold + msg + Reset + "\r\n";

    public static string FmtKv(IReadOnlyDictionary<string, string> kv)
    {
        var sb = new StringBuilder();
        foreach (var (k, v) in kv)
        {
            sb.Append(k).Append('=').Append(v).Append("\r\n");
        }

        return sb.ToString();
    }
}

public sealed class CommandResult
{
    public bool Ok { get; set; } = true;
    public string Output { get; set; } = "";
    public string Error { get; set; } = "";
}

public sealed class CommandDispatcher
{
    private readonly Dictionary<string, Func<string[], CommandResult>> _commands = new(StringComparer.OrdinalIgnoreCase);

    public CommandDispatcher()
    {
        Register("help", _ => new CommandResult
        {
            Output = ShellOutput.Heading("commands") + string.Join("\r\n", _commands.Keys.OrderBy(k => k)) + "\r\n",
        });
        Register("clear", _ => new CommandResult { Output = "\x1b[2J\x1b[H" });
        Register("env", _ =>
        {
            var kv = Environment.GetEnvironmentVariables()
                .Cast<System.Collections.DictionaryEntry>()
                .Take(20)
                .ToDictionary(e => e.Key?.ToString() ?? "", e => e.Value?.ToString() ?? "");
            return new CommandResult { Output = ShellOutput.FmtKv(kv) };
        });
    }

    public void Register(string name, Func<string[], CommandResult> handler) => _commands[name] = handler;

    public CommandResult Dispatch(string line)
    {
        var trimmed = line.Trim();
        if (trimmed.Length == 0)
        {
            return new CommandResult();
        }

        var parts = trimmed.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        var name = parts[0];
        var args = parts.Skip(1).ToArray();
        if (!_commands.TryGetValue(name, out var handler))
        {
            return new CommandResult
            {
                Ok = false,
                Error = ShellOutput.ErrorMsg($"{name}: unknown command"),
            };
        }

        return handler(args);
    }
}
