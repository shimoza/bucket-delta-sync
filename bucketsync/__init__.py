"""bucket-delta-sync: one-way incremental mirror between cloud object stores.

The destination becomes a true mirror of the source: new and changed objects are
copied, and objects removed from the source are removed from the destination.

Public surface is intentionally small. Build adapters with the factory in
``bucketsync.adapters`` and run a sync with ``bucketsync.engine.run_sync``.
"""

__version__ = "0.1.0"
