//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Conformance;

/// <summary>
/// The recording transport the live protocol asks for: it sits under the
/// <see cref="Client.HijackClient"/>'s <see cref="HttpClient"/> and writes down the
/// status and the raw bytes that came back.
///
/// The library still performs the call and still shapes <c>ok</c> and <c>body</c>.
/// Without this, a 401, a 403 and a 404 all reach the driver as the same
/// refusal, and the matrix could not tell three different servers apart.
/// </summary>
public sealed class LiveStatusRecordingHandler : DelegatingHandler
{
    public LiveStatusRecordingHandler()
        : base(new HttpClientHandler())
    {
    }

    /// <summary>True once any response came back — as opposed to no answer at all.</summary>
    public bool HasResponse { get; private set; }

    /// <summary>Status of the last response, or null when nothing answered.</summary>
    public int? StatusCode { get; private set; }

    /// <summary>Whether the last response was a 2xx.</summary>
    public bool Successful { get; private set; }

    /// <summary>Raw body text of the last response, as it came off the wire.</summary>
    public string RawBody { get; private set; } = "";

    protected override async Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request, CancellationToken cancellationToken)
    {
        var response = await base.SendAsync(request, cancellationToken).ConfigureAwait(false);
        // Buffer before reading: the library reads the same content afterwards,
        // and a once-through stream would leave it with nothing.
        await response.Content.LoadIntoBufferAsync(cancellationToken).ConfigureAwait(false);
        RawBody = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
        StatusCode = (int)response.StatusCode;
        Successful = response.IsSuccessStatusCode;
        HasResponse = true;
        return response;
    }
}
