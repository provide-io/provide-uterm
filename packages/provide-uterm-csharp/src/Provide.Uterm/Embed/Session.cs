//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Threading.Channels;

namespace Provide.Uterm.Embed;

/// <summary>
/// Ordered multi-client proxy session. All intercept / send paths serialize on a single
/// async gate so re-entrant script output cannot reorder relative to the triggering chunk.
/// </summary>
internal sealed class UtermSession : IUtermSession
{
    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly AsyncLocal<bool> _holdingGate = new();
    private readonly IByteInterceptor _interceptor;
    private readonly ITelnetPolicy _telnetPolicy;
    private readonly Dictionary<string, object?> _services;
    private readonly Dictionary<string, ClientSlot> _clients = new(StringComparer.Ordinal);
    private readonly Queue<DeferredItem> _deferred = new();
    private readonly object _eventGate = new();

    private IUpstreamPipe? _upstream;
    private CancellationTokenSource? _readerCts;
    private Task? _readerTask;
    private int _disposed;

    private async Task WithGateAsync(Func<Task> action, CancellationToken cancellationToken)
    {
        if (_holdingGate.Value)
        {
            await action().ConfigureAwait(false);
            return;
        }

        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        _holdingGate.Value = true;
        try
        {
            await action().ConfigureAwait(false);
        }
        finally
        {
            _holdingGate.Value = false;
            _gate.Release();
        }
    }

    public UtermSession(string sessionId, EmbedSessionOptions options)
    {
        SessionId = sessionId;
        _interceptor = options.Interceptor ?? PassThroughInterceptor.Instance;
        _telnetPolicy = options.TelnetPolicy ?? new DefaultTelnetPolicy();
        _services = options.Services is null
            ? new Dictionary<string, object?>(StringComparer.Ordinal)
            : new Dictionary<string, object?>(options.Services, StringComparer.Ordinal);
        // Policy is available to hosts via Services convention key.
        _services["telnet_policy"] = _telnetPolicy;
        Lifecycle = SessionLifecycle.Created;
    }

    public string SessionId { get; }
    public SessionLifecycle Lifecycle { get; private set; }
    public IReadOnlyDictionary<string, object?> Services => _services;

    public event EventHandler<ByteChunkEventArgs>? ApplicationDataReceived;
    public event EventHandler<ByteChunkEventArgs>? ClientDataReceived;
    public event EventHandler<WireEventArgs>? WireEvents;
    public event EventHandler<SessionLifecycleEventArgs>? LifecycleChanged;

    public Task ConnectUpstreamAsync(IUpstreamPipe upstream, CancellationToken cancellationToken = default) =>
        WithGateAsync(async () =>
        {
            SetLifecycle(SessionLifecycle.Connecting);
            await upstream.ConnectAsync(cancellationToken).ConfigureAwait(false);
            _upstream = upstream;
            StartReader_NoLock();
            SetLifecycle(SessionLifecycle.Connected);
        }, cancellationToken);

    public async Task ReplaceUpstreamAsync(IUpstreamPipe upstream, CancellationToken cancellationToken = default)
    {
        CancellationTokenSource? oldCts = null;
        Task? oldReader = null;
        IUpstreamPipe? oldUp = null;
        await WithGateAsync(() =>
        {
            SetLifecycle(SessionLifecycle.Reconnecting);
            oldCts = _readerCts;
            oldReader = _readerTask;
            _readerCts = null;
            _readerTask = null;
            oldUp = _upstream;
            return Task.CompletedTask;
        }, cancellationToken).ConfigureAwait(false);

        if (oldCts is not null)
        {
            await oldCts.CancelAsync().ConfigureAwait(false);
        }

        if (oldReader is not null)
        {
            try
            {
                await oldReader.ConfigureAwait(false);
            }
            catch
            {
                // cancelled
            }
        }

        oldCts?.Dispose();
        if (oldUp is not null)
        {
            try
            {
                await oldUp.DisconnectAsync(cancellationToken).ConfigureAwait(false);
            }
            catch
            {
                // best-effort
            }
        }

        await WithGateAsync(async () =>
        {
            SetLifecycle(SessionLifecycle.Connecting);
            await upstream.ConnectAsync(cancellationToken).ConfigureAwait(false);
            _upstream = upstream;
            StartReader_NoLock();
            SetLifecycle(SessionLifecycle.Connected);
        }, cancellationToken).ConfigureAwait(false);
    }

    public Task MarkNegotiatedAsync(CancellationToken cancellationToken = default) =>
        WithGateAsync(() =>
        {
            SetLifecycle(SessionLifecycle.Negotiated);
            if (_upstream?.IsConnected == true)
            {
                SetLifecycle(SessionLifecycle.Connected);
            }

            return Task.CompletedTask;
        }, cancellationToken);

    public Task SendToUpstreamAsync(ReadOnlyMemory<byte> data, CancellationToken cancellationToken = default)
    {
        var bytes = data.ToArray();
        return WithGateAsync(
            () => ProcessClientPath_NoLock(bytes, clientId: null, cancellationToken),
            cancellationToken);
    }

    public Task SendToClientsAsync(ReadOnlyMemory<byte> data, ClientFilter? filter = null, CancellationToken cancellationToken = default)
    {
        var bytes = data.ToArray();
        var f = filter ?? ClientFilter.All;
        return WithGateAsync(() =>
        {
            DeliverToClients_NoLock(bytes, f);
            RaiseApp(bytes, ByteDirection.UpstreamToApp, null);
            return Task.CompletedTask;
        }, cancellationToken);
    }

    public async Task<IClientHandle> AttachClientAsync(ClientAttachOptions options, CancellationToken cancellationToken = default)
    {
        IClientHandle? handle = null;
        await WithGateAsync(() =>
        {
            var meta = options.Metadata;
            if (string.IsNullOrEmpty(meta.ClientId))
            {
                throw new ArgumentException("ClientId required", nameof(options));
            }

            if (_clients.ContainsKey(meta.ClientId))
            {
                throw new InvalidOperationException("client already attached: " + meta.ClientId);
            }

            var slot = new ClientSlot(meta);
            _clients[meta.ClientId] = slot;
            // Event only — do not overwrite durable CONNECTED with ClientAttached.
            LifecycleChanged?.Invoke(this, new SessionLifecycleEventArgs
            {
                Phase = SessionLifecycle.ClientAttached,
                Detail = meta.ClientId,
            });
            handle = slot.Handle;
            return Task.CompletedTask;
        }, cancellationToken).ConfigureAwait(false);
        return handle!;
    }

    public Task DetachClientAsync(string clientId, CancellationToken cancellationToken = default) =>
        WithGateAsync(() =>
        {
            if (_clients.Remove(clientId, out var slot))
            {
                slot.Handle.MarkDetached();
                slot.Complete();
            }

            return Task.CompletedTask;
        }, cancellationToken);

    public Task FlushDeferredAsync(CancellationToken cancellationToken = default) =>
        WithGateAsync(async () =>
        {
            while (_deferred.Count > 0)
            {
                var item = _deferred.Dequeue();
                if (item.Direction == ByteDirection.UpstreamToApp)
                {
                    await ProcessUpstreamPath_NoLock(item.Data, cancellationToken, fromDefer: true).ConfigureAwait(false);
                }
                else
                {
                    await ProcessClientPath_NoLock(item.Data, item.ClientId, cancellationToken, fromDefer: true).ConfigureAwait(false);
                }
            }
        }, cancellationToken);

    public async Task RaiseWireEventAsync(WireEventKind kind, ReadOnlyMemory<byte> data, string detail = "", CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        WireEventArgs args;
        lock (_eventGate)
        {
            args = new WireEventArgs { Kind = kind, Data = data.ToArray(), Detail = detail };
        }

        WireEvents?.Invoke(this, args);
        await Task.CompletedTask.ConfigureAwait(false);
    }

    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0)
        {
            return;
        }

        CancellationTokenSource? cts = null;
        Task? reader = null;
        IUpstreamPipe? up = null;
        await WithGateAsync(() =>
        {
            cts = _readerCts;
            reader = _readerTask;
            _readerCts = null;
            _readerTask = null;
            up = _upstream;
            _upstream = null;
            foreach (var c in _clients.Values)
            {
                c.Handle.MarkDetached();
                c.Complete();
            }

            _clients.Clear();
            return Task.CompletedTask;
        }, CancellationToken.None).ConfigureAwait(false);

        if (cts is not null)
        {
            await cts.CancelAsync().ConfigureAwait(false);
        }

        if (reader is not null)
        {
            try
            {
                await reader.ConfigureAwait(false);
            }
            catch
            {
                // cancelled
            }
        }

        cts?.Dispose();
        if (up is not null)
        {
            try
            {
                await up.DisconnectAsync().ConfigureAwait(false);
            }
            catch
            {
                // ignore
            }
        }

        await WithGateAsync(() =>
        {
            SetLifecycle(SessionLifecycle.Shutdown);
            return Task.CompletedTask;
        }, CancellationToken.None).ConfigureAwait(false);
        _gate.Dispose();
    }

    private void StartReader_NoLock()
    {
        _readerCts = new CancellationTokenSource();
        var ct = _readerCts.Token;
        var upstream = _upstream ?? throw new InvalidOperationException("no upstream");
        _readerTask = Task.Run(() => ReaderLoopAsync(upstream, ct), CancellationToken.None);
    }

    private async Task ReaderLoopAsync(IUpstreamPipe upstream, CancellationToken ct)
    {
        try
        {
            while (!ct.IsCancellationRequested)
            {
                byte[] chunk;
                try
                {
                    chunk = await upstream.ReceiveAsync(ct).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    break;
                }
                catch
                {
                    await OnUpstreamLostAsync().ConfigureAwait(false);
                    break;
                }

                if (chunk.Length == 0)
                {
                    await OnUpstreamLostAsync().ConfigureAwait(false);
                    break;
                }

                await WithGateAsync(
                    () => ProcessUpstreamPath_NoLock(chunk, ct),
                    ct).ConfigureAwait(false);
            }
        }
        catch (OperationCanceledException)
        {
            // normal
        }
    }

    private Task OnUpstreamLostAsync() =>
        WithGateAsync(() =>
        {
            SetLifecycle(SessionLifecycle.UpstreamLost);
            return Task.CompletedTask;
        }, CancellationToken.None);

    private async Task ProcessUpstreamPath_NoLock(byte[] data, CancellationToken ct, bool fromDefer = false)
    {
        var ctx = new InterceptContext
        {
            Session = this,
            Direction = ByteDirection.UpstreamToApp,
            Data = data,
        };
        var result = await _interceptor.OnUpstreamAsync(ctx, ct).ConfigureAwait(false);
        await ApplyResult_NoLock(result, data, ByteDirection.UpstreamToApp, clientId: null, ct, fromDefer).ConfigureAwait(false);
    }

    private async Task ProcessClientPath_NoLock(byte[] data, string? clientId, CancellationToken ct, bool fromDefer = false)
    {
        RaiseClient(data, clientId);
        var ctx = new InterceptContext
        {
            Session = this,
            Direction = ByteDirection.ClientToUpstream,
            Data = data,
            ClientId = clientId,
        };
        var result = await _interceptor.OnClientAsync(ctx, ct).ConfigureAwait(false);
        await ApplyResult_NoLock(result, data, ByteDirection.ClientToUpstream, clientId, ct, fromDefer).ConfigureAwait(false);
    }

    private async Task ApplyResult_NoLock(
        InterceptResult result,
        byte[] original,
        ByteDirection direction,
        string? clientId,
        CancellationToken ct,
        bool fromDefer)
    {
        switch (result.Action)
        {
            case InterceptAction.Pass:
                await Forward_NoLock(original, direction, clientId, ct).ConfigureAwait(false);
                break;
            case InterceptAction.Replace:
                await Forward_NoLock(result.Payload ?? Array.Empty<byte>(), direction, clientId, ct).ConfigureAwait(false);
                break;
            case InterceptAction.Consume:
                break;
            case InterceptAction.Defer:
                if (!fromDefer)
                {
                    _deferred.Enqueue(new DeferredItem(direction, original, clientId));
                }

                break;
            case InterceptAction.Inject:
                // Drop original; re-enter pipeline with inject payload (ordered after current frame completes).
                var injected = result.Payload ?? Array.Empty<byte>();
                if (direction == ByteDirection.UpstreamToApp)
                {
                    await ProcessUpstreamPath_NoLock(injected, ct).ConfigureAwait(false);
                }
                else
                {
                    await ProcessClientPath_NoLock(injected, clientId, ct).ConfigureAwait(false);
                }

                break;
            default:
                await Forward_NoLock(original, direction, clientId, ct).ConfigureAwait(false);
                break;
        }
    }

    private async Task Forward_NoLock(byte[] data, ByteDirection direction, string? clientId, CancellationToken ct)
    {
        if (data.Length == 0)
        {
            return;
        }

        if (direction == ByteDirection.UpstreamToApp)
        {
            DeliverToClients_NoLock(data, ClientFilter.All);
            RaiseApp(data, direction, clientId);
            return;
        }

        var up = _upstream ?? throw new InvalidOperationException("no upstream");
        if (!up.IsConnected)
        {
            throw new InvalidOperationException("upstream not connected");
        }

        await up.SendAsync(data, ct).ConfigureAwait(false);
    }

    private void DeliverToClients_NoLock(byte[] data, ClientFilter filter)
    {
        List<string>? toDrop = null;
        foreach (var (id, slot) in _clients)
        {
            if (!filter.Matches(slot.Metadata))
            {
                continue;
            }

            if (!slot.TryEnqueue(data))
            {
                if (slot.Metadata.Backpressure == BackpressurePolicy.Disconnect)
                {
                    toDrop ??= new List<string>();
                    toDrop.Add(id);
                }
            }
        }

        if (toDrop is not null)
        {
            foreach (var id in toDrop)
            {
                if (_clients.Remove(id, out var slot))
                {
                    slot.Complete();
                }
            }
        }
    }

    private void RaiseApp(byte[] data, ByteDirection direction, string? clientId) =>
        ApplicationDataReceived?.Invoke(this, new ByteChunkEventArgs
        {
            Direction = direction,
            Data = data,
            ClientId = clientId,
        });

    private void RaiseClient(byte[] data, string? clientId) =>
        ClientDataReceived?.Invoke(this, new ByteChunkEventArgs
        {
            Direction = ByteDirection.ClientToUpstream,
            Data = data,
            ClientId = clientId,
        });

    private void SetLifecycle(SessionLifecycle phase, string detail = "")
    {
        Lifecycle = phase;
        LifecycleChanged?.Invoke(this, new SessionLifecycleEventArgs { Phase = phase, Detail = detail });
    }

    private readonly record struct DeferredItem(ByteDirection Direction, byte[] Data, string? ClientId);

    private sealed class ClientSlot
    {
        private readonly Channel<byte[]> _channel;
        private int _count;
        private readonly int _capacity;

        public ClientSlot(ClientMetadata metadata)
        {
            Metadata = metadata;
            _capacity = Math.Max(1, metadata.QueueCapacity);
            _channel = Channel.CreateUnbounded<byte[]>(new UnboundedChannelOptions
            {
                SingleReader = true,
                SingleWriter = false,
            });
            Handle = new ClientHandle(this);
        }

        public ClientMetadata Metadata { get; }
        public ClientHandle Handle { get; }

        public bool TryEnqueue(byte[] data)
        {
            var copy = data.ToArray();
            if (_count >= _capacity)
            {
                switch (Metadata.Backpressure)
                {
                    case BackpressurePolicy.DropNewest:
                        return false;
                    case BackpressurePolicy.Disconnect:
                        return false;
                    case BackpressurePolicy.DropOldest:
                    default:
                        // Best-effort drop one oldest by reading if available.
                        if (_channel.Reader.TryRead(out _))
                        {
                            Interlocked.Decrement(ref _count);
                        }

                        break;
                }
            }

            if (_channel.Writer.TryWrite(copy))
            {
                Interlocked.Increment(ref _count);
                return true;
            }

            return false;
        }

        public async Task<byte[]> ReceiveAsync(CancellationToken ct)
        {
            var item = await _channel.Reader.ReadAsync(ct).ConfigureAwait(false);
            Interlocked.Decrement(ref _count);
            return item;
        }

        public void Complete() => _channel.Writer.TryComplete();
    }

    private sealed class ClientHandle : IClientHandle
    {
        private readonly ClientSlot _slot;
        private volatile bool _attached = true;

        public ClientHandle(ClientSlot slot) => _slot = slot;

        public string ClientId => _slot.Metadata.ClientId;
        public ClientMetadata Metadata => _slot.Metadata;
        public bool IsAttached => _attached;

        internal void MarkDetached() => _attached = false;

        public async Task<byte[]> ReceiveAsync(CancellationToken cancellationToken = default)
        {
            if (!_attached)
            {
                return Array.Empty<byte>();
            }

            try
            {
                return await _slot.ReceiveAsync(cancellationToken).ConfigureAwait(false);
            }
            catch (ChannelClosedException)
            {
                return Array.Empty<byte>();
            }
        }

        public ValueTask DisposeAsync()
        {
            MarkDetached();
            _slot.Complete();
            return ValueTask.CompletedTask;
        }
    }
}
