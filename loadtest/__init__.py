"""The correctness-under-load harness for Tollgate.

A standalone tool that drives high-concurrency reserve / commit / cancel traffic
at a hot shared budget over a deep tree, then runs an invariant oracle over the
resulting ledger. Added incrementally.
"""
