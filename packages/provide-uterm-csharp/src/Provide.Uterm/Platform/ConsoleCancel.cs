//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Platform;

/// <summary>
/// The interactive "run until Ctrl-C" wait shared by <c>uterm</c> and
/// <c>uterm-manager</c>. Subscription goes through two seams (defaulting to
/// <see cref="Console.CancelKeyPress"/>) so a test can deliver the keypress
/// itself instead of signalling the test host.
/// </summary>
internal static class ConsoleCancel
{
    internal static Action<ConsoleCancelEventHandler> Subscribe { get; set; } =
        static handler => Console.CancelKeyPress += handler;

    internal static Action<ConsoleCancelEventHandler> Unsubscribe { get; set; } =
        static handler => Console.CancelKeyPress -= handler;

    /// <summary>
    /// Block until Ctrl-C. The keypress is consumed (<c>Cancel = true</c>) so the
    /// caller, not the runtime, decides how the process ends.
    /// </summary>
    internal static void WaitForCtrlC()
    {
        var pressed = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        ConsoleCancelEventHandler handler = (_, e) =>
        {
            e.Cancel = true;
            pressed.TrySetResult();
        };
        Subscribe(handler);
        try
        {
            pressed.Task.GetAwaiter().GetResult();
        }
        finally
        {
            Unsubscribe(handler);
        }
    }
}
