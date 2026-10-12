"""Code that migrations run, kept outside migrations/versions so tests and command line tools can import it.

A module here is frozen once the revision that runs it has shipped: later code changes must never change what an
applied migration did. Never application code: authz_v2.py (the conversion) is standard library only,
authz_v2_data.py (reading the legacy tables, describing the plan) standard library and SQLAlchemy Core.
"""
