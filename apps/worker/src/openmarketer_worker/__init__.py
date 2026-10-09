"""OpenMarketer Temporal worker: runs the long work of each agent as durable workflows.

One folder per agent holds its workflow, activities and settings; ``main.py`` wires them.
What every agent's graphs share, the cleanup of left-over checkpoints, is beside it.
"""
