"""Application layer: command handlers and the ports they depend on.

It imports the domain and declares the ``CounterStore`` and repository ports as
Protocols. It never imports a concrete adapter; the composition root injects
those.
"""
