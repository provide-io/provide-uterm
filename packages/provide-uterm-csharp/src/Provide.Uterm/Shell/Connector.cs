//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Security.Cryptography;
using System.Text;
using Provide.Uterm.Connectors;
using Provide.Uterm.Session;
using Provide.Uterm.TermSession;

namespace Provide.Uterm.Shell;

/// <summary>Config for <see cref="UshellConnector"/>. Port of Go shell.ConnectorConfig.</summary>
public sealed class UshellConnectorConfig
{
    public string DisplayName { get; init; } = "";
    public ShellContext? Context { get; init; }
    public Dictionary<string, object?>? ExtraCtx { get; init; }
    public HttpClient? HttpClient { get; init; }
    public Func<byte[], int, int, string, (IReadOnlyList<string> Frames, double Fps)>? RenderImage { get; init; }

    /// <summary>Test hook: replace idle poll sleep (Go pollSleep).</summary>
    public Action<TimeSpan>? PollSleep { get; init; }

    public TimeSpan PollDelay { get; init; } = TimeSpan.FromMilliseconds(50);
}

/// <summary>
/// Interactive REPL connector (no external process). Port of Go shell.UshellConnector
/// and Python UshellConnector: Start/Stop/IsConnected, PollMessages, HandleInput,
/// HandleControl, GetSnapshot, GetAnalysis, Clear, SetMode.
/// Also implements <see cref="IConnector"/> for registry/session wiring.
/// </summary>
public sealed class UshellConnector : IConnector
{
    private readonly object _gate = new();
    private readonly string _sessionId;
    private readonly string _displayName;
    private readonly LineBuffer _buf = new();
    private readonly CommandDispatcher _dispatcher;
    private readonly Dictionary<string, object?> _ctxValues;
    private readonly Action<TimeSpan> _pollSleep;
    private readonly TimeSpan _pollDelay;
    private readonly List<Dictionary<string, object?>> _pending = new();

    private bool _connected;
    private bool _welcomed;
    private bool _flowPaused;
    private CancellationTokenSource? _animCts;
    private Task? _animTask;

    public UshellConnector(string sessionId, UshellConnectorConfig? config = null)
    {
        config ??= new UshellConnectorConfig();
        _sessionId = sessionId;
        _displayName = string.IsNullOrEmpty(config.DisplayName) ? sessionId : config.DisplayName;
        var ctx = config.Context ?? new ShellContext
        {
            Values = config.ExtraCtx is null
                ? new Dictionary<string, object?>()
                : new Dictionary<string, object?>(config.ExtraCtx),
        };
        _ctxValues = ctx.Values;
        _dispatcher = new CommandDispatcher(ctx, config.HttpClient, config.RenderImage);
        _pollSleep = config.PollSleep ?? (d =>
        {
            if (d > TimeSpan.Zero)
            {
                Thread.Sleep(d);
            }
        });
        _pollDelay = config.PollDelay <= TimeSpan.Zero ? TimeSpan.FromMilliseconds(50) : config.PollDelay;
    }

    public string SessionId => _sessionId;
    public string DisplayName => _displayName;

    public void Start()
    {
        lock (_gate)
        {
            _connected = true;
        }
    }

    public void Stop()
    {
        CancellationTokenSource? cts;
        lock (_gate)
        {
            _connected = false;
            cts = _animCts;
            _animCts = null;
        }

        cts?.Cancel();
        cts?.Dispose();
    }

    public bool IsConnected()
    {
        lock (_gate)
        {
            return _connected;
        }
    }

    /// <summary>Welcome frames then pending; idle poll sleeps then returns empty.</summary>
    public IReadOnlyList<Dictionary<string, object?>> PollMessages()
    {
        lock (_gate)
        {
            if (!_connected || _flowPaused)
            {
                return Array.Empty<Dictionary<string, object?>>();
            }

            if (!_welcomed)
            {
                _welcomed = true;
                return new[]
                {
                    ShellFrames.WorkerHello("open"),
                    ShellFrames.Term(ShellOutput.Banner + ShellOutput.Prompt),
                };
            }

            if (_pending.Count > 0)
            {
                var frames = _pending.ToList();
                _pending.Clear();
                return frames;
            }
        }

        _pollSleep(_pollDelay);
        return Array.Empty<Dictionary<string, object?>>();
    }

    public IReadOnlyList<Dictionary<string, object?>> HandleInput(string data)
    {
        string echo;
        IReadOnlyList<string> completed;
        lock (_gate)
        {
            _buf.Feed(data);
            echo = _buf.TakeEcho();
            completed = _buf.TakeCompleted();
        }

        var frames = new List<Dictionary<string, object?>>();
        if (echo.Length > 0)
        {
            frames.Add(ShellFrames.Term(echo));
        }

        foreach (var line in completed)
        {
            var result = _dispatcher.Dispatch(line);
            if (result.Animated is not null)
            {
                StartAnimation(result.Animated);
                continue;
            }

            foreach (var s in result.Text)
            {
                frames.Add(ShellFrames.Term(s));
            }
        }

        return frames;
    }

    public IReadOnlyList<Dictionary<string, object?>> HandleControl(string action)
    {
        switch (action)
        {
            case "flow_pause":
                lock (_gate)
                {
                    _flowPaused = true;
                }

                return Array.Empty<Dictionary<string, object?>>();
            case "flow_resume":
                lock (_gate)
                {
                    _flowPaused = false;
                }

                return Array.Empty<Dictionary<string, object?>>();
            case "snapshot_request":
                return new[] { GetSnapshot() };
            default:
                return Array.Empty<Dictionary<string, object?>>();
        }
    }

    public Dictionary<string, object?> GetSnapshot()
    {
        string current;
        string sid;
        lock (_gate)
        {
            current = _buf.CurrentLine();
            sid = _sessionId;
        }

        var screen = "ushell " + sid + "\r\n" + ShellOutput.Prompt + current;
        var x = ShellOutput.Prompt.Length + current.Length; // ASCII-aligned like Go rune count for ASCII prompt
        return new Dictionary<string, object?>
        {
            ["type"] = "snapshot",
            ["screen"] = screen,
            ["cursor"] = new Dictionary<string, object?> { ["x"] = x, ["y"] = 1 },
            ["cols"] = 80,
            ["rows"] = 24,
            ["screen_hash"] = ScreenHash(screen),
            ["cursor_at_end"] = true,
            ["has_trailing_space"] = false,
            ["prompt_detected"] = new Dictionary<string, object?> { ["prompt_id"] = "ushell_prompt" },
            ["ts"] = ShellFrames.NowTs(),
        };
    }

    public string GetAnalysis()
    {
        bool connected;
        string current;
        string sid;
        List<string> names;
        lock (_gate)
        {
            connected = _connected;
            current = _buf.CurrentLine();
            sid = _sessionId;
            names = _ctxValues.Keys.Where(k => !k.StartsWith("__", StringComparison.Ordinal)).OrderBy(k => k, StringComparer.Ordinal).ToList();
        }

        return string.Join(
            "\n",
            $"[ushell analysis — session: {sid}]",
            $"connected: {connected.ToString().ToLowerInvariant()}",
            $"current_line: \"{current}\"",
            $"context_names: [{string.Join(' ', names)}]");
    }

    public IReadOnlyList<Dictionary<string, object?>> ClearScreen()
    {
        lock (_gate)
        {
            _buf.Clear();
        }

        return new[] { ShellFrames.Term(ShellOutput.ClearScreen + ShellOutput.Prompt) };
    }

    public IReadOnlyList<Dictionary<string, object?>> SetMode(string mode) =>
        new[] { ShellFrames.WorkerHello(mode) };

    // --- IConnector surface (session registry / hosted runtime) ---

    public Task StartAsync(CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        Start();
        return Task.CompletedTask;
    }

    public Task StopAsync(CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        Stop();
        return Task.CompletedTask;
    }

    public Task HandleInputAsync(string data, CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var frames = HandleInput(data);
        lock (_gate)
        {
            _pending.AddRange(frames);
        }

        return Task.CompletedTask;
    }

    void IConnector.HandleControl(string action) => _ = HandleControl(action);

    void IConnector.SetMode(string mode)
    {
        if (mode is not ("open" or "hijack"))
        {
            throw new ArgumentException($"invalid mode: {mode}");
        }

        lock (_gate)
        {
            _pending.AddRange(SetMode(mode));
        }
    }

    void IConnector.Clear()
    {
        lock (_gate)
        {
            _pending.AddRange(ClearScreen());
        }
    }

    Snapshot IConnector.Snapshot()
    {
        var f = GetSnapshot();
        return new Snapshot
        {
            Screen = f.TryGetValue("screen", out var s) ? s?.ToString() ?? "" : "",
            Cols = 80,
            Rows = 24,
        };
    }

    string IConnector.Analysis() => GetAnalysis();

    IReadOnlyList<Dictionary<string, object?>> IConnector.Events()
    {
        // Hosted runtime drains frames via Events(); reuse PollMessages without idle sleep.
        lock (_gate)
        {
            if (!_connected || _flowPaused)
            {
                return Array.Empty<Dictionary<string, object?>>();
            }

            if (!_welcomed)
            {
                _welcomed = true;
                return new List<Dictionary<string, object?>>
                {
                    ShellFrames.WorkerHello("open"),
                    ShellFrames.Term(ShellOutput.Banner + ShellOutput.Prompt),
                };
            }

            if (_pending.Count == 0)
            {
                return Array.Empty<Dictionary<string, object?>>();
            }

            var copy = _pending.ToList();
            _pending.Clear();
            return copy;
        }
    }

    TransportSession? IConnector.Session() => null;

    private void StartAnimation(AnimatedResult result)
    {
        CancellationTokenSource? old;
        lock (_gate)
        {
            old = _animCts;
            _animCts = new CancellationTokenSource();
            var ct = _animCts.Token;
            _animTask = Task.Run(() => StreamAnimationAsync(result, ct), CancellationToken.None);
        }

        try
        {
            old?.Cancel();
        }
        catch
        {
            // ignore
        }

        old?.Dispose();
    }

    private async Task StreamAnimationAsync(AnimatedResult result, CancellationToken ct)
    {
        var delay = result.Fps > 0
            ? TimeSpan.FromSeconds(1.0 / result.Fps)
            : TimeSpan.FromMilliseconds(100);
        try
        {
            while (true)
            {
                foreach (var frame in result.Frames)
                {
                    await Task.Delay(delay, ct).ConfigureAwait(false);
                    AppendPending(ShellFrames.Term(frame));
                }

                if (!result.Loop)
                {
                    break;
                }
            }

            AppendPending(ShellFrames.Term(ShellOutput.Prompt));
        }
        catch (OperationCanceledException)
        {
            AppendPending(ShellFrames.Term(ShellOutput.Prompt));
        }
    }

    private void AppendPending(Dictionary<string, object?> frame)
    {
        lock (_gate)
        {
            _pending.Add(frame);
        }
    }

    private static string ScreenHash(string screen)
    {
        var hash = SHA256.HashData(Encoding.UTF8.GetBytes(screen));
        // Deterministic short decimal-like hint (not FNV but stable across process)
        var n = BitConverter.ToUInt64(hash, 0);
        var s = n.ToString(System.Globalization.CultureInfo.InvariantCulture);
        return s.Length > 16 ? s[..16] : s;
    }
}
