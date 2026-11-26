"""The schema contract marker, checked on the source of a revision (tests/migrations/test_migration_policy.py).

A revision after the baseline declares CONTRACT = True | False at module level (support/compat.py). In upgrade(), a
statement that removes or narrows something needs CONTRACT = True: drop_column, drop_table, drop_index of an index
the same upgrade() did not create, rename_table, alter_column with new_column_name, nullable=False or type_ (on op or
on a batch_alter_table object). CONTRACT = True needs a call set_min_code_head(op, "<revision>") in upgrade().
"""
import ast
from typing import Optional

from app.backend.utils.alembic_history import History

NARROWING_CALLS = ("drop_column", "drop_table", "rename_table")
NARROWING_ALTER_KEYWORDS = ("new_column_name", "type_")


def _marker(tree: ast.Module) -> Optional[bool]:
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "CONTRACT":
                if isinstance(value, ast.Constant) and isinstance(value.value, bool):
                    return value.value
                return None
    return None


def _upgrade(tree: ast.Module) -> Optional[ast.FunctionDef]:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
            return node
    return None


def _name(call: ast.Call) -> Optional[str]:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _first_text(call: ast.Call) -> Optional[str]:
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    return None


def _narrowing(calls: list[ast.Call]) -> list[str]:
    created_indexes = {_first_text(call) for call in calls if _name(call) == "create_index"}
    found = []
    for call in calls:
        name = _name(call)
        if name in NARROWING_CALLS:
            found.append(name)
        elif name == "drop_index" and _first_text(call) not in created_indexes:
            found.append("drop_index")
        elif name == "alter_column":
            for keyword in call.keywords:
                if keyword.arg in NARROWING_ALTER_KEYWORDS:
                    found.append(f"alter_column({keyword.arg}=...)")
                elif (keyword.arg == "nullable" and isinstance(keyword.value, ast.Constant)
                      and keyword.value.value is False):
                    found.append("alter_column(nullable=False)")
    return found


def check_revision_source(text: str) -> list[str]:
    """Problems of one revision file's source; [] when it follows the contract"""
    tree = ast.parse(text)
    contract = _marker(tree)
    if contract is None:
        return ["no module-level CONTRACT = True | False"]
    upgrade = _upgrade(tree)
    calls = [node for node in ast.walk(upgrade) if isinstance(node, ast.Call)] if upgrade else []
    if contract:
        names_min = any(_name(call) == "set_min_code_head" and len(call.args) == 2
                        and isinstance(call.args[1], ast.Constant) and isinstance(call.args[1].value, str)
                        for call in calls)
        return [] if names_min else ['CONTRACT = True needs set_min_code_head(op, "<revision>") in upgrade()']
    return [f"{statement} in upgrade() needs CONTRACT = True and set_min_code_head (RELEASE.md)"
            for statement in _narrowing(calls)]


def revisions_after(baseline: str, hist: History) -> list[str]:
    """Every revision of hist that has baseline among its ancestors (baseline itself left out)"""
    return sorted(revision for revision in hist.parents
                  if revision != baseline and hist.is_at_or_before(baseline, revision))
