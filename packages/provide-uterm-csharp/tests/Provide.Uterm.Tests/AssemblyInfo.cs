//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Xunit;

// Belt-and-suspenders with xunit.runner.json: never parallelize within a host
// process (CI Ubuntu has hit glibc free()/malloc crashes under native interop).
[assembly: CollectionBehavior(DisableTestParallelization = true, MaxParallelThreads = 1)]
