#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Record what the reference's hosted session runtime reports, state by state.

The TypeScript port has to decide four things about a session's lifecycle, and
every one of them is the reference's decision rather than the port's:

* which names ``lifecycle_state`` may take at all;
* what a session that has never been started reports;
* what it reports between being asked to start and being up;
* what it reports when the connector could not be built.

Guessing any of them is how ``paused`` — a name that appears nowhere in
``bridge/contracts.py`` — ended up in the port's own type. So the answers are
taken from the reference by running it: a real
:class:`~provide.uterm.server.runtime.HostedSessionRuntime` is driven through
each transition and its ``status()`` is recorded.

Only the fields the runtime itself owns are kept. The rest of the status object
comes straight off the definition and is already held to the reference by
``serverhttp_golden``; recording it twice would only give it two places to
drift from.

The failure case needs no network. ``connector_type`` is a name nothing has
registered, so ``build_connector`` refuses before a socket is opened — which
makes it the one failure that is the same on every machine, and therefore the
one worth committing.

Regenerate from the repository root::

    uv run python packages/provide-uterm-ts/testdata/gen_sessionruntime_golden.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, get_args

from provide.uterm.bridge.contracts import SessionLifecycle
from provide.uterm.server.models import RecordingConfig, SessionDefinition
from provide.uterm.server.runtime import HostedSessionRuntime

OUT = Path(__file__).with_name("sessionruntime_golden.json")

# The fields ``HostedSessionRuntime`` decides for itself. Everything else on a
# status is copied from the definition.
RUNTIME_FIELDS = ("lifecycle_state", "connected", "stopped_at", "last_error")

# How long to wait for the runtime's own task to finish before giving up. The
# failing path never touches the network, so this is generous by a wide margin
# and exists only so a broken reference fails the generator instead of hanging
# it.
TASK_TIMEOUT_S = 10.0


def _runtime(connector_type: str) -> HostedSessionRuntime:
    """A runtime over one session, with nothing optional attached."""
    definition = SessionDefinition.model_validate(
        {
            "session_id": "probe",
            "display_name": "Probe",
            "connector_type": connector_type,
            "connector_config": {},
            "input_mode": "open",
            "auto_start": True,
            "tags": ["probe"],
            "visibility": "public",
        }
    )
    return HostedSessionRuntime(
        definition,
        # Never dialled: the recorded paths stop before the worker link.
        public_base_url="http://127.0.0.1:1",
        recording=RecordingConfig.model_validate({"directory": tempfile.mkdtemp()}),
    )


def _observed(runtime: HostedSessionRuntime) -> dict[str, Any]:
    """The runtime-owned fields of a status, as the wire would carry them."""
    status = runtime.status().model_dump(mode="python")
    observed = {field: status[field] for field in RUNTIME_FIELDS}
    # The instant a session stopped is a clock reading, so only whether it was
    # written down can be committed.
    observed["stopped_at_set"] = observed.pop("stopped_at") is not None
    return observed


async def _collect() -> dict[str, Any]:
    supported = _runtime("shell")
    initial = _observed(supported)

    await supported.start()
    # Read before yielding to the loop: ``start`` sets the state itself and
    # hands the rest to a task, so this is the state a request arriving in
    # between would be answered with.
    starting = _observed(supported)

    # Asking a starting session to start again must change nothing — the
    # reference returns early on a task that is still running.
    await supported.start()
    restarted = _observed(supported)

    await supported.stop()
    stopped = _observed(supported)

    failing = _runtime("no-such-connector")
    await failing.start()
    await asyncio.wait_for(failing._task, TASK_TIMEOUT_S)
    failed = _observed(failing)

    return {
        "generator": "packages/provide-uterm-ts/testdata/gen_sessionruntime_golden.py",
        "lifecycles": list(get_args(SessionLifecycle)),
        "initial": initial,
        "starting": starting,
        "started_twice": restarted,
        "stopped": stopped,
        "unsupported_connector": failed,
    }


def main() -> None:
    corpus = asyncio.run(_collect())
    OUT.write_text(json.dumps(corpus, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
