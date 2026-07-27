"""Persistence for graphical-target definitions.

**This package has no caller inside Python, and that is deliberate — do not
delete it as dead code.**

Graphical targets are a C#- and Go-side feature: Python has no target registry
and no REST surface for them. What Python does share is the ``cp_*`` schema, and
SQLite records each CREATE statement's literal text in ``sqlite_master``. A
database is therefore only interchangeable between the three runtimes if all
three emit byte-identical DDL — so ``cp_graphical_targets`` must exist here too,
and a table with no store would be a schema Python could create but never read
or repair.

Concretely, this package is what lets a Python-created database be handed to the
Go or C# server, and lets an operator inspect or fix a row from Python without a
second toolchain. The cross-language guarantee is exercised by the C# suite's
``SqliteCrossCompatTests`` (which reads a Python-written golden database) and by
``SqliteSchemaParityTests`` (which compares the C# DDL against this package's
sibling schema module).

If Python ever grows a graphical-target feature, this is the layer it builds on.
"""

from __future__ import annotations

from provide.uterm.control.plane.graphical_target.store import GraphicalTargetStore
from provide.uterm.control.plane.graphical_target.types import GraphicalTargetRecord

__all__ = ["GraphicalTargetRecord", "GraphicalTargetStore"]
