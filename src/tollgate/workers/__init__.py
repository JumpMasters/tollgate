"""Background workers: the reservation and idempotency reapers.

Both are small, polled, idempotent workers operating in bounded per-item
transactions. Added incrementally.
"""
