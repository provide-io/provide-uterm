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
/// <item><c>_log_snapshot</c>: every snapshot's screen, escape sequences
/// removed, as "read", through the shared stateless detector (a snapshot is a
/// whole screen);</item>
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
/// <para>Read-path dedupe (the reference's rule in
/// <c>HostedSessionRuntime.__init__</c>, mirrored exactly), keyed by each
/// annotation's <see cref="Annotation.Annotation.MatchKey"/> — a SHA-256 of the
/// rule id and the full matched text, never the text itself:
/// <list type="bullet">
/// <item><c>_readAnnotationKeys</c>: every key recorded in this recording, by
/// either path.</item>
/// <item><c>_snapshotOnlyKeys</c>: keys the snapshot path recorded since the
/// last processed stream chunk.</item>
/// <item>Snapshot match: key in <c>_readAnnotationKeys</c> → skip; otherwise
/// record it and add the key to BOTH sets.</item>
/// <item>Stream chunk (non-empty, while recording): for each match, key in
/// <c>_snapshotOnlyKeys</c> → remove it and skip; otherwise record it and add
/// the key to <c>_readAnnotationKeys</c>. After the chunk's matches,
/// <c>_snapshotOnlyKeys</c> is cleared, matched or not.</item>
/// </list>
/// So stream-then-snapshot records once, snapshot-then-next-chunk records once,
/// identical snapshots record once, a command streamed twice is annotated
/// twice, and a pending key never suppresses a later, genuinely new match. If a
/// snapshot's text reaches the stream only in a later chunk it is recorded a
/// second time: a duplicate is preferred over a missed annotation. Both sets
/// are bounded by <see cref="MaxReadAnnotationKeys"/> (cleared when full). A
/// match with an EMPTY key is never deduped: it is recorded and added to
/// neither set.</para>
///
/// <para>The escape carry and both key sets belong to one recording: the
/// reference clears them in <c>_stop_recording</c>. This port has no separate
/// recording object — a recording is "the session's status says recording",
/// identified by its <see cref="SessionStatus.RecordingGeneration"/> — so they
/// are cleared when a scan first sees a different recording state from the last
/// one it saw: the recording ending, or a new one (a later generation) having
/// begun, even if no frame arrived while recording was off. The state is read,
/// and any reset applied, inside the same critical section as the scan that
/// relies on it, so a scan holding a stale reading can never reset a newer
/// recording's state. The one lock-free path is an unrecorded session that
/// was already unrecorded at the last scan: it changes nothing and returns.</para>
/// </summary>
internal sealed class SessionAnnotator
{
    /// <summary>
    /// Bound on each per-recording set of read-path annotation keys; past it the
    /// set is cleared and starts over. Reference: <c>_MAX_READ_ANNOTATION_KEYS = 1024</c>.
    /// </summary>
    internal const int MaxReadAnnotationKeys = 1024;

    private readonly object _gate = new();
    private readonly PatternDetector _detector;
    private readonly StreamingDetector _readStream;
    private readonly StreamingDetector _sendStream;
    /// <summary><see cref="ObservedRecording"/>'s value for "not recorded".</summary>
    private const long NotRecording = -1;

    private readonly Func<long?> _recording;
    private readonly Func<Dictionary<string, object?>, Task> _record;
    private readonly HashSet<string> _readAnnotationKeys = new(StringComparer.Ordinal);
    private readonly HashSet<string> _snapshotOnlyKeys = new(StringComparer.Ordinal);
    private string _escapeCarry = "";
    private int _eventSeq;

    /// <summary>
    /// The recording state the last scan saw: a recording's generation, or
    /// <see cref="NotRecording"/>. The carry and keys belong to it. Starts as
    /// not recorded, which has no state to clear.
    /// </summary>
    private long _observedRecording = NotRecording;

    private int _stateResets;

    /// <param name="detector">Shared across sessions; it holds no state.</param>
    /// <param name="isRecording">
    /// Read on every frame, as the reference checks its logger. With no
    /// generation to tell recordings apart, a recording that stops and starts
    /// again between two frames reads as the same one.
    /// </param>
    /// <param name="record">Writes one annotation payload to the session's recording.</param>
    public SessionAnnotator(
        PatternDetector detector, Func<bool> isRecording, Func<Dictionary<string, object?>, Task> record)
        : this(detector, () => isRecording() ? 0 : null, record)
    {
    }

    /// <param name="detector">Shared across sessions; it holds no state.</param>
    /// <param name="recording">
    /// Read on every frame: the generation of the session's current recording
    /// (non-negative), or null when it is not recorded.
    /// </param>
    /// <param name="record">Writes one annotation payload to the session's recording.</param>
    public SessionAnnotator(
        PatternDetector detector, Func<long?> recording, Func<Dictionary<string, object?>, Task> record)
    {
        _detector = detector;
        // One stream per direction, so typed text never bridges into printed text.
        _readStream = new StreamingDetector(detector);
        _sendStream = new StreamingDetector(detector);
        _recording = recording;
        _record = record;
    }

    /// <summary>
    /// The dedupe key of a read-path annotation: <see cref="ReadAnnotationKey"/>.
    /// Replaceable only by tests, to stand in for an annotation the detector
    /// did not build (whose key is empty).
    /// </summary>
    internal Func<Annotation.Annotation, string> KeyOf { get; init; } = ReadAnnotationKey;

    /// <summary>The escape tail held back from the last streamed chunk (tests).</summary>
    internal string EscapeCarry
    {
        get { lock (_gate) return _escapeCarry; }
    }

    /// <summary>How many times a change of recording cleared the carry and keys (tests).</summary>
    internal int StateResets
    {
        get { lock (_gate) return _stateResets; }
    }

    /// <summary>How many read-path keys the current recording remembers (tests).</summary>
    internal int ReadAnnotationKeyCount
    {
        get { lock (_gate) return _readAnnotationKeys.Count; }
    }

    /// <summary>How many snapshot keys wait for the next stream chunk (tests).</summary>
    internal int PendingSnapshotKeyCount
    {
        get { lock (_gate) return _snapshotOnlyKeys.Count; }
    }

    /// <summary>
    /// A snapshot frame being sent: its whole screen, escape sequences removed,
    /// as "read". A rendered screen carries SGR codes and ends each row with a
    /// reset, which would split a styled match from the read-path rules
    /// (reference: <c>_log_snapshot</c> strips before detecting).
    ///
    /// <para>The snapshot path DEDUPES (rule in the class remarks): a match
    /// already recorded by the stream or an earlier snapshot is skipped; a new
    /// one is recorded and left pending for the next stream chunk.</para>
    /// </summary>
    public IReadOnlyList<Annotation.Annotation> ScanSnapshot(IReadOnlyDictionary<string, object?> frame)
    {
        if (SettledUnrecorded()) return [];
        var screen = frame.TryGetValue("screen", out var s) ? s?.ToString() ?? "" : "";
        var text = ScreenNormalize.StripAnsi(screen);
        lock (_gate)
        {
            if (!ObserveRecording()) return [];
            _eventSeq++;
            var fresh = new List<Annotation.Annotation>();
            foreach (var annotation in _detector.Detect("read", text, _eventSeq))
            {
                var key = KeyOf(annotation);
                if (key.Length == 0)
                {
                    fresh.Add(annotation);
                    continue;
                }

                if (_readAnnotationKeys.Contains(key)) continue;
                AddBounded(_readAnnotationKeys, key);
                AddBounded(_snapshotOnlyKeys, key);
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
    /// <para>The stream path records every match — a command run twice is
    /// annotated twice — EXCEPT a match in the first chunk after a snapshot
    /// that already recorded it, which it consumes once instead; the pending
    /// keys are then cleared, matched or not. Every match it records is
    /// remembered so the snapshot path, which sees the same text again, does
    /// not record it a second time.</para>
    /// </summary>
    public IReadOnlyList<Annotation.Annotation> ScanOutput(string data)
    {
        if (string.IsNullOrEmpty(data) || SettledUnrecorded()) return [];
        lock (_gate)
        {
            if (!ObserveRecording()) return [];
            (var text, _escapeCarry) = ScreenNormalize.SplitIncompleteEscape(_escapeCarry + data);
            var fresh = new List<Annotation.Annotation>();
            foreach (var annotation in _readStream.Detect("read", ScreenNormalize.StripAnsi(text), _eventSeq))
            {
                var key = KeyOf(annotation);
                if (key.Length == 0)
                {
                    fresh.Add(annotation);
                    continue;
                }

                if (_snapshotOnlyKeys.Remove(key)) continue;
                AddBounded(_readAnnotationKeys, key);
                fresh.Add(annotation);
            }

            // Pending snapshot keys expire with the first chunk after the
            // snapshot, matched or not, so they never suppress a later match.
            _snapshotOnlyKeys.Clear();
            return fresh;
        }
    }

    /// <summary>Input on its way to the connector, as "send".</summary>
    public IReadOnlyList<Annotation.Annotation> ScanInput(string data)
    {
        if (SettledUnrecorded()) return [];
        lock (_gate)
        {
            if (!ObserveRecording()) return [];
            _eventSeq++;
            return _sendStream.Detect("send", data, _eventSeq);
        }
    }

    /// <summary>
    /// Identity of a read-path annotation for stream/snapshot dedupe: the
    /// detector's <see cref="Annotation.Annotation.MatchKey"/> (SHA-256 of rule
    /// id + full matched text). It tells apart two different credentials of one
    /// rule, which share a label and description, and it is a digest, so the
    /// key sets never hold a raw secret. It is "" for an annotation the
    /// detector did not build, and an empty key is never deduped (it would
    /// collapse every such annotation into one). Reference: <c>_read_annotation_key</c>.
    /// </summary>
    internal static string ReadAnnotationKey(Annotation.Annotation annotation) => annotation.MatchKey;

    /// <summary>Add a key to a bounded key set, clearing it first when full.</summary>
    private static void AddBounded(HashSet<string> keys, string key)
    {
        if (keys.Count >= MaxReadAnnotationKeys) keys.Clear();
        keys.Add(key);
    }

    /// <summary>
    /// Whether the session is recorded now; the caller holds <c>_gate</c> and
    /// scans under the same hold. When the recording state differs from the
    /// last one seen — the recording ended, or another began — the carry and
    /// keys belonged to a recording that is over, so they go with it
    /// (reference: <c>_stop_recording</c>). Reading the state under the lock
    /// that the scan uses is what keeps a stale reading from resetting a newer
    /// recording: every reading is applied in the order the lock grants it.
    /// </summary>
    private bool ObserveRecording()
    {
        var current = _recording() ?? NotRecording;
        if (_observedRecording != current)
        {
            _escapeCarry = "";
            _readAnnotationKeys.Clear();
            _snapshotOnlyKeys.Clear();
            _stateResets++;
            Volatile.Write(ref _observedRecording, current);
        }

        return current != NotRecording;
    }

    /// <summary>
    /// The lock-free path: the session is not recorded and was not at the last
    /// scan either, so there is nothing to reset and nothing to scan. Safe
    /// without the lock because it changes nothing: it is the same as a scan
    /// that ran, under the lock, at the moment the delegate was read — the
    /// last observation being "not recorded" too, no state existed to clear.
    /// Any other case takes the lock and decides there.
    /// </summary>
    private bool SettledUnrecorded() =>
        _recording() is null && Volatile.Read(ref _observedRecording) == NotRecording;

    /// <summary>Record each match as an <c>annotation</c> event, payload as the reference's <c>to_dict()</c>.</summary>
    public async Task RecordAsync(IReadOnlyList<Annotation.Annotation> annotations)
    {
        foreach (var annotation in annotations)
        {
            await _record(annotation.ToDict()).ConfigureAwait(false);
        }
    }
}
