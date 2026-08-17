//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Hub;
using Provide.Uterm.Server;

namespace Provide.Uterm.Tests;

/// <summary>
/// Registering a webhook after the manager has shut down.
///
/// ShutdownAsync disposes the shared cancellation source, so a registration
/// arriving afterwards throws ObjectDisposedException the moment StartDelivery
/// tries to link a token to it. By then the event-bus subscription already
/// exists — Watch runs first — so the handler has to drop it again, or the
/// subscription outlives the manager with no worker to drain it.
///
/// The leak is observable because EventBus caps subscribers per worker: at a
/// cap of one, a second registration can only succeed if the first one's
/// subscription was actually released.
/// </summary>
public sealed class WebhookLateRegistrationTests
{
    private const string SessionId = "worker-late";

    private static WebhookManager ManagerOn(EventBus bus) =>
        new(allowLoopbackDestinations: true, delivery: new WebhookDeliveryOptions { EventBus = bus });

    [Fact]
    public async Task A_registration_after_shutdown_releases_the_subscription_it_took()
    {
        // A cap of one turns "the subscription leaked" into a thrown exception
        // rather than a slow drift nothing checks.
        var bus = new EventBus(maxSubscribersPerWorker: 1);
        var manager = ManagerOn(bus);
        await manager.ShutdownAsync();

        // Each of these subscribes, then fails to link a token to the disposed
        // shutdown source. If the handler did not unsubscribe, the second call
        // would exceed the cap and Watch would throw.
        var first = manager.Register(SessionId, "http://127.0.0.1:9/first", null, null, null);
        var second = manager.Register(SessionId, "http://127.0.0.1:9/second", null, null, null);

        Assert.NotEqual(first.WebhookId, second.WebhookId);
    }

    [Fact]
    public async Task A_registration_after_shutdown_starts_no_delivery_worker()
    {
        var bus = new EventBus(maxSubscribersPerWorker: 1);
        var manager = ManagerOn(bus);
        await manager.ShutdownAsync();

        manager.Register(SessionId, "http://127.0.0.1:9/late", null, null, null);

        // No worker means nothing is draining the bus. Publishing has to stay a
        // no-op that returns rather than blocking on a queue nobody reads.
        bus.Enqueue(SessionId, new Dictionary<string, object?> { ["type"] = "output" });

        // Shutting down again must stay clean: the late registration recorded no
        // worker, so teardown has nothing extra to release and must not throw on
        // the already-disposed source.
        await manager.ShutdownAsync();
    }

    [Fact]
    public async Task Registering_before_shutdown_still_takes_a_subscription()
    {
        // The negative control. If Register never subscribed at all, the test
        // above would pass for the wrong reason — nothing would ever be at the
        // cap, unsubscribe or not.
        var bus = new EventBus(maxSubscribersPerWorker: 1);
        var manager = ManagerOn(bus);

        manager.Register(SessionId, "http://127.0.0.1:9/live", null, null, null);

        // The live registration holds the only slot, so a direct Watch is refused.
        var refused = Assert.Throws<InvalidOperationException>(() => bus.Watch(SessionId));
        Assert.Contains("max subscribers", refused.Message, StringComparison.Ordinal);

        await manager.ShutdownAsync();
    }
}
