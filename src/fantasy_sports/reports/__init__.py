"""The outward-facing layer (ARCHITECTURE §4).

Nothing in ``core/`` or ``providers/`` imports this package. It is the only
layer allowed to reach out of the process: to a model (through ``model-route``)
or, here, to a command the owner configured.
"""
