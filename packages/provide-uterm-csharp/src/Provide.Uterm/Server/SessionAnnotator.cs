//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.RegularExpressions;
using Provide.Uterm.Annotation;
using Provide.Uterm.Screen;

namespace Provide.Uterm.Server;

/// <summary>
/// Automatic annotation for one hosted session: the three scans the reference's
/// <c>HostedSessionRuntime</c> makes (<c>server/runtime.py</c>) —
/// <list type="bullet">
/// <item><c>_log_snapshot</c>: every snapshot's screen, escape sequences
/// removed, as "read", through the shared stateless detector (a snapshot is a
/// whole screen), skipping any match already recorded;</item>
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
///
/// <para>The escape carry and the read-path keys belong to one recording: the
/// reference clears both in <c>_stop_recording</c>. This port has no separate
/// recording object — a recording is "the session's status says recording" —
/// so they are cleared whenever a scan finds the session not recorded, which is
/// the first moment this annotator can see that the recording ended.</para>
/// </summary>
internal sealed class SessionAnnotator
{
    /// <summary>
    /// Longest escape tail held back between chunks, ESC included. Reference:
    /// <c>_MAX_ESCAPE_CARRY = 64</c> (<c>server/runtime_helpers.py</c>). A real
    /// sequence is a handful of bytes; past this it is not one (or is hostile),
    /// and it is released into the text rather than carried forever.
    /// </summary>
    internal const int MaxEscapeCarry = 64;

    /// <summary>
    /// Bound on the per-recording set of read-path annotation keys; past it the
    /// set is cleared and starts over. Reference: <c>_MAX_READ_ANNOTATION_KEYS = 1024</c>.
    /// </summary>
    internal const int MaxReadAnnotationKeys = 1024;

    /// <summary>
    /// An escape sequence cut off at the end of a chunk: a bare ESC, or a CSI
    /// whose parameter/intermediate bytes have not reached a final byte.
    /// Reference: <c>_INCOMPLETE_ESCAPE_TAIL</c>, matched with <c>fullmatch</c>
    /// (hence the anchors).
    /// </summary>
    private static readonly Regex IncompleteEscapeTail = new(@"^\x1b(?:\[[0-?]*[ -/]*)?\z", RegexOptions.Compiled);

    private readonly object _gate = new();
    private readonly PatternDetector _detector;
    private readonly StreamingDetector _readStream;
    private readonly StreamingDetector _sendStream;
    private readonly Func<bool> _isRecording;
    private readonly Func<Dictionary<string, object?>, Task> _record;
    private readonly HashSet<string> _readAnnotationKeys = new(StringComparer.Ordinal);
    private string _escapeCarry = "";
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

    /// <summary>The escape tail held back from the last streamed chunk (tests).</summary>
    internal string EscapeCarry
    {
        get { lock (_gate) return _escapeCarry; }
    }

    /// <summary>How many read-path keys the current recording remembers (tests).</summary>
    internal int ReadAnnotationKeyCount
    {
        get { lock (_gate) return _readAnnotationKeys.Count; }
    }

    /// <summary>
    /// A snapshot frame being sent: its whole screen, escape sequences removed,
    /// as "read". A rendered screen carries SGR codes and ends each row with a
    /// reset, which would split a styled match from the read-path rules
    /// (reference: <c>_log_snapshot</c> strips before detecting).
    ///
    /// <para>The snapshot path DEDUPES: a match the stream (or an earlier
    /// snapshot of the same screen) already recorded is skipped, so neither a
    /// stream+snapshot pair nor a run of identical snapshots records it twice.</para>
    /// </summary>
    public IReadOnlyList<Annotation.Annotation> ScanSnapshot(IReadOnlyDictionary<string, object?> frame)
    {
        if (!IsRecording()) return [];
        var screen = frame.TryGetValue("screen", out var s) ? s?.ToString() ?? "" : "";
        var text = ScreenNormalize.StripAnsi(screen);
        lock (_gate)
        {
            _eventSeq++;
            var fresh = new List<Annotation.Annotation>();
            foreach (var annotation in _detector.Detect("read", text, _eventSeq))
            {
                var key = ReadAnnotationKey(annotation);
                if (_readAnnotationKeys.Contains(key)) continue;
                RememberReadAnnotation(key);
                fresh.Add(annotation);
            }

            return fresh;
        }
    }

    /// <summary>
    /// Streamed terminal output, escape sequences removed, as "read".
    ///
    /// <para>An escape sequence split across chunks (<c>...\x1b[1</c> |
    /// <c>msudo ...</c>) would leave <c>msudo</c> behind if each chunk were
    /// stripped alone, so an unterminated trailing sequence is held back and
    /// prepended to the next chunk (reference: <c>_scan_output</c>).</para>
    ///
    /// <para>The stream path NEVER suppresses a match — a command run twice is
    /// annotated twice — but remembers each one so the snapshot path, which
    /// sees the same text again, does not record it a second time.</para>
    /// </summary>
    public IReadOnlyList<Annotation.Annotation> ScanOutput(string data)
    {
        if (string.IsNullOrEmpty(data) || !IsRecording()) return [];
        lock (_gate)
        {
            (var text, _escapeCarry) = SplitIncompleteEscape(_escapeCarry + data);
            var found = _readStream.Detect("read", ScreenNormalize.StripAnsi(text), _eventSeq);
            foreach (var annotation in found)
            {
                RememberReadAnnotation(ReadAnnotationKey(annotation));
            }

            return found;
        }
    }

    /// <summary>Input on its way to the connector, as "send".</summary>
    public IReadOnlyList<Annotation.Annotation> ScanInput(string data)
    {
        if (!IsRecording()) return [];
        lock (_gate)
        {
            _eventSeq++;
            return _sendStream.Detect("send", data, _eventSeq);
        }
    }

    /// <summary>
    /// Split <paramref name="text"/> into (complete, carry): carry is an
    /// unterminated trailing escape. Reference: <c>_split_incomplete_escape</c>.
    /// Only the LAST ESC can start an unterminated tail (CSI parameter bytes
    /// never include ESC). A tail longer than <see cref="MaxEscapeCarry"/> is
    /// not carried. (Lengths are UTF-16 units, the reference's code points: a
    /// tail the grammar accepts is all ASCII, and any other tail is not carried
    /// under either count.)
    /// </summary>
    internal static (string Complete, string Carry) SplitIncompleteEscape(string text)
    {
        var start = text.LastIndexOf('\x1b');
        if (start < 0 || text.Length - start > MaxEscapeCarry) return (text, "");
        if (!IncompleteEscapeTail.IsMatch(text.AsSpan(start))) return (text, "");
        return (text[..start], text[start..]);
    }

    /// <summary>
    /// Identity of a read-path annotation for snapshot dedupe: rule + matched
    /// text. Reference: <c>_read_annotation_key</c> — the label is the rule's
    /// category and the description is the rule's template formatted with the
    /// match, so the pair names the rule and (for every rule that embeds the
    /// match) the matched text.
    /// </summary>
    internal static string ReadAnnotationKey(Annotation.Annotation annotation) =>
        annotation.Label + "\0" + annotation.Description;

    /// <summary>Add a key to the bounded read-path set, clearing it first when full.</summary>
    private void RememberReadAnnotation(string key)
    {
        if (_readAnnotationKeys.Count >= MaxReadAnnotationKeys) _readAnnotationKeys.Clear();
        _readAnnotationKeys.Add(key);
    }

    /// <summary>
    /// Whether the session is recorded now. When it is not, the recording the
    /// carry and keys belonged to has ended, so they go with it (reference:
    /// <c>_stop_recording</c>).
    /// </summary>
    private bool IsRecording()
    {
        if (_isRecording()) return true;
        lock (_gate)
        {
            _escapeCarry = "";
            _readAnnotationKeys.Clear();
        }

        return false;
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
