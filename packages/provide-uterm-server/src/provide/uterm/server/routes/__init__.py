#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Route modules for the hosted terminal server.

Mutation-enforced at killed==100 (see [tool.mutmut].source_paths). mutmut skips the
@router.* handlers, so the mutable surface is the UNDECORATED helpers (accessors,
create_*_router bodies, nested helpers); the bound suite is
tests/server/test_routes_mutation_killing.py, which exercises them via router-endpoint
extraction with a mocked Request (no TestClient/full-app lifespan).
"""
