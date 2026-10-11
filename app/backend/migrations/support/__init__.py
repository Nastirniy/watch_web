"""Code that migrations run, kept outside migrations/versions so tests and command line tools can import it.

A module here is frozen once the revision that runs it has shipped: later code changes must never change what an
applied migration did. Standard library only.
"""
