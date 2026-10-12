"""What autogenerate (`migrations_manager revision`) compares with the models.

Some tables belong to the migrations, not to the models: the logs the downgrades of 022 and 023 read and the legacy
tables 023 keeps until a cleanup revision drops them. Without this filter every new revision would propose dropping
them (env.py passes include_object to alembic).
"""
UNMODELLED_TABLES = frozenset({
    "authz_v2_migration_log",       # 023: what its downgrade restores from
    "builtin_roles_migration_log",  # 022: what its downgrade restores from
    "user_roles_v1",                # 023: the legacy user_roles, kept for the downgrade
    "user_post_access_v1",          # 023: the legacy user_post_access, kept for the downgrade
})


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Leaves out the tables above and everything on them (indexes, constraints, columns)"""
    table = name if type_ == "table" else getattr(getattr(obj, "table", None), "name", None)
    return table not in UNMODELLED_TABLES
