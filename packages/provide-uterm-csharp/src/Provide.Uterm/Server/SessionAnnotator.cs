//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Annotation;
using Provide.Uterm.Screen;

namespace Provide.Uterm.Server;

/// <summary>
/// Automatic annotation for one hosted session: the three scans the reference's
/// <c>HostedSessionRuntime</c> makes (<c>server/runtime.py</c>) —
/// <list type="bullet">
/// <item><c>_log_snapshot</c>: every snapshot's screen, as "read", through the
/// shared stateless detector (a snapshot is a whole screen);</item>
/// <item><c>_scan_output</c>: every streamed <c>term</c> frame, escape
/// sequences removed, as "read", through this session's output stream;</item>
/// <item><c>_log_send</c>: every chunk of input, as "send", through this
/// session's input stream.</item>
/// </list>
/// All three run only while the session is recorded: annotations are recording
/// entries, and with no recording there is nowhere for one to go.
///
/// <para>The span carries the reference's event sequence: one step per snapshot
/// and per chunk of input recorded, with streamed output taking the current
/// value. Scanning is synchronous and serialised here so it can run inside a
/// caller's lock; <see cref="RecordAsync"/> writes the matches afterwards.</para>
/// </summary>
internal sealed class SessionAnnotator
{
    private readonly object _gate = new();
    private readonly PatternDetector _detector;
    private readonly StreamingDetector _readStream;
    private readonly StreamingDetector _sendStream;
    private readonly Func<bool> _isRecording;
    private readonly Func<Dictionary<string, object?>, Task> _record;
    private int _eventSeq;

    /// <param name="detector">Shared across sessions; it holds no state.</param>
    /// <param name="isRecording">Read on every frame, as the reference checks its logger.</param>
    /// <param name="record">Writes one annotation payload to the session's recording.</param>
    public SessionAnnotator(
        PatternDetector detector, Func<bool> isRecording, Func<Dictionary<string, object?>, Task> record)
    {
        _detector = detector;
        // One stream per direction, so typed text never bridges into printed text.
        _readStream = new StreamingDetector(detector);
        _sendStream = new StreamingDetector(detector);
        _isRecording = isRecording;
        _record = record;
    }

    /// <summary>A snapshot frame being sent: its whole screen, as "read".</summary>
    public IReadOnlyList<Annotation.Annotation> ScanSnapshot(IReadOnlyDictionary<string, object?> frame)
    {
        if (!_isRecording()) return [];
        var screen = frame.TryGetValue("screen", out var s) ? s?.ToString() ?? "" : "";
        lock (_gate)
        {
            _eventSeq++;
            return _detector.Detect("read", screen, _eventSeq);
        }
    }

    /// <summary>Streamed terminal output, escape sequences removed, as "read".</summary>
    public IReadOnlyList<Annotation.Annotation> ScanOutput(string data)
    {
        if (string.IsNullOrEmpty(data) || !_isRecording()) return [];
        var text = ScreenNormalize.StripAnsi(data);
        lock (_gate)
        {
            return _readStream.Detect("read", text, _eventSeq);
        }
    }

    /// <summary>Input on its way to the connector, as "send".</summary>
    public IReadOnlyList<Annotation.Annotation> ScanInput(string data)
    {
        if (!_isRecording()) return [];
        lock (_gate)
        {
            _eventSeq++;
            return _sendStream.Detect("send", data, _eventSeq);
        }
    }

    /// <summary>Record each match as an <c>annotation</c> event, payload as the reference's <c>to_dict()</c>.</summary>
    public async Task RecordAsync(IReadOnlyList<Annotation.Annotation> annotations)
    {
        foreach (var annotation in annotations)
        {
            await _record(annotation.ToDict()).ConfigureAwait(false);
        }
    }
}
