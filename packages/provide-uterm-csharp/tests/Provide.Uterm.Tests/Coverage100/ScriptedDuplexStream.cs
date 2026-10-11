//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Threading.Channels;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>
/// A duplex stream whose reads are queued by the test (an empty chunk is EOF)
/// and whose writes are recorded or made to fail. Reads honour cancellation.
/// </summary>
internal sealed class ScriptedDuplexStream : Stream
{
    private readonly Channel<byte[]> _reads = Channel.CreateUnbounded<byte[]>();
    private readonly List<byte> _written = new();
    private readonly TaskCompletionSource _firstWrite = new(TaskCreationOptions.RunContinuationsAsynchronously);

    public Exception? WriteError { get; set; }

    public Task FirstWrite => _firstWrite.Task;

    public byte[] Written
    {
        get { lock (_written) return _written.ToArray(); }
    }

    public void EnqueueRead(byte[] chunk) => _reads.Writer.TryWrite(chunk);

    public void EnqueueEof() => _reads.Writer.TryWrite(Array.Empty<byte>());

    public override bool CanRead => true;
    public override bool CanSeek => false;
    public override bool CanWrite => true;
    public override long Length => throw new NotSupportedException();
    public override long Position
    {
        get => throw new NotSupportedException();
        set => throw new NotSupportedException();
    }

    public override void Flush()
    {
    }

    public override int Read(byte[] buffer, int offset, int count) =>
        ReadAsync(buffer.AsMemory(offset, count)).AsTask().GetAwaiter().GetResult();

    public override async ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default)
    {
        var chunk = await _reads.Reader.ReadAsync(cancellationToken).ConfigureAwait(false);
        chunk.CopyTo(buffer);
        return chunk.Length;
    }

    public override void Write(byte[] buffer, int offset, int count) =>
        WriteAsync(buffer.AsMemory(offset, count)).AsTask().GetAwaiter().GetResult();

    public override ValueTask WriteAsync(ReadOnlyMemory<byte> buffer, CancellationToken cancellationToken = default)
    {
        if (WriteError is not null)
        {
            _firstWrite.TrySetResult();
            return ValueTask.FromException(WriteError);
        }

        lock (_written)
        {
            _written.AddRange(buffer.ToArray());
        }

        _firstWrite.TrySetResult();
        return ValueTask.CompletedTask;
    }

    public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
    public override void SetLength(long value) => throw new NotSupportedException();
}
