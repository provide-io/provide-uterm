//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace Provide.Uterm.Conformance;

/// <summary>
/// A step that needs an earlier step's answer
/// (<c>conformance/live/PROTOCOL.md</c>).
///
/// <c>hijack_send</c> needs the <c>hijack_id</c> that <c>hijack_acquire</c> came
/// back with, so a string field in a step may hold a **reference**:
///
/// <code>
/// { "id": "send", "action": "hijack_send", "hijack_id": "${acquire.body.hijack_id}" }
/// </code>
///
/// This is the one place the "drivers observe, the harness judges" rule does not
/// reach: the driver performs the request, so the driver is the only thing
/// holding the value in time to use it. The harness cannot resolve what it never
/// saw.
///
/// The grammar is deliberately the smallest thing that works — one step id, one
/// dotted path, no expressions, no defaults, no nesting — and the **whole field**
/// must be the reference, so <c>"a${x.y}b"</c> is a value a scenario meant
/// literally and is sent as written.
///
/// Its own type because it is the seam the protocol says to expect trouble on: a
/// resolver whose pattern never matched would leave every reference as written
/// and every step would still *run*, asking a server about a literal
/// <c>${...}</c> — which reads, in a matrix, as the server having refused
/// something.
/// </summary>
public static class LiveReference
{
    /// <summary>
    /// The one shape a reference has.
    ///
    /// A verbatim string literal on purpose: written as an ordinary literal the
    /// backslashes would have to be doubled, and doubling them once too often is
    /// how a resolver comes to match a literal backslash and silently resolve
    /// nothing at all while looking entirely correct.
    /// </summary>
    public static readonly Regex Pattern =
        new(@"^\$\{([a-z0-9_]+)\.([A-Za-z0-9_.]+)\}$", RegexOptions.Compiled, TimeSpan.FromSeconds(1));

    /// <summary>
    /// The step with every reference replaced by what the step it names recorded.
    /// </summary>
    /// <param name="step">The step as the scenario wrote it. Left untouched.</param>
    /// <param name="seen">What each step that has already run recorded, by step id.</param>
    /// <exception cref="LiveDriverException">
    /// When a reference names a step that has not run, or a path that is not
    /// there. That is a malformed scenario rather than something a server did, so
    /// it ends the run: recording it as a field would let the harness compare it
    /// as though the server had answered.
    /// </exception>
    public static LiveStep Resolve(LiveStep step, IReadOnlyDictionary<string, JsonObject> seen)
    {
        var raw = step.Raw.DeepClone().AsObject();
        // Every key, not a hand-kept list of them: a field a scenario can write
        // is a field a reference can fill, and a list would be one more place to
        // forget the next action's argument.
        foreach (var key in raw.Select(pair => pair.Key).ToList())
        {
            if (raw[key] is not JsonValue value || !value.TryGetValue<string>(out var text))
            {
                continue;
            }

            var match = Pattern.Match(text);
            if (!match.Success)
            {
                continue;
            }

            raw[key] = Lookup(step.Id, text, match.Groups[1].Value, match.Groups[2].Value, seen);
        }

        return LiveScenario.ParseStep(raw);
    }

    /// <summary>Read one reference out of what has been recorded so far.</summary>
    private static JsonNode? Lookup(
        string stepId, string reference, string named, string path, IReadOnlyDictionary<string, JsonObject> seen)
    {
        if (!seen.TryGetValue(named, out var fields))
        {
            throw new LiveDriverException($"step {stepId}: {reference} names {named}, which has not run");
        }

        JsonNode? node = fields;
        foreach (var segment in path.Split('.'))
        {
            if (!TryDig(node, segment, out node))
            {
                throw new LiveDriverException($"step {stepId}: {reference} is not there");
            }
        }

        // Detached: the node still belongs to the record of the earlier step,
        // and a node with a parent cannot be attached to this step.
        return node?.DeepClone();
    }

    /// <summary>
    /// One segment of a dotted path: objects by key, arrays by numeric index,
    /// and nothing else.
    ///
    /// A key the record actually has counts even when its value is null —
    /// absent and null are different answers, and only the first is a malformed
    /// reference.
    /// </summary>
    private static bool TryDig(JsonNode? node, string segment, out JsonNode? found)
    {
        switch (node)
        {
            case JsonObject obj when obj.TryGetPropertyValue(segment, out found):
                return true;
            case JsonArray array
                when int.TryParse(segment, out var index) && index >= 0 && index < array.Count:
                found = array[index];
                return true;
            default:
                found = null;
                return false;
        }
    }
}
