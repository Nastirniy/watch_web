"""The schema contract marker of migrations after the baseline (RELEASE.md, "Release rules for developers").

A statement that removes or narrows something in upgrade() (drop a column, a table or an index that existed before,
rename, NOT NULL, a type change) needs CONTRACT = True, and CONTRACT = True needs set_min_code_head(op, "<rev>").
What a parser cannot see (a stored value older code cannot read, a protocol shared through the database) is marked by
hand: the release rule in RELEASE.md.
"""
import textwrap

import pytest

from app.backend.bootstrap.schema_range import BASELINE
from app.backend.migrations.support.policy import check_revision_source, revisions_after
from app.backend.utils.alembic_history import history
from tests.paths import BACKEND_DIR

VERSIONS = BACKEND_DIR / "migrations" / "versions"


def source(body: str, contract: str = "CONTRACT = False") -> str:
    header = (
        "from alembic import op\n"
        "import sqlalchemy as sa\n"
        "from app.backend.migrations.support.compat import set_min_code_head\n\n"
        'revision = "038_x"\n'
        'down_revision = "037_outbox_events"\n'
        f"{contract}\n\n\n"
        "def upgrade():\n"
    )
    footer = '\n\ndef downgrade():\n    op.drop_column("users", "nickname")\n'
    return header + textwrap.indent(textwrap.dedent(body).strip("\n"), "    ") + "\n" + footer


def test_an_expand_migration_passes():
    assert check_revision_source(source('''
        op.add_column("users", sa.Column("nickname", sa.String(32), nullable=True))
        op.create_index("ix_users_nickname", "users", ["nickname"])
        op.drop_index("ix_users_nickname", table_name="users")
        op.alter_column("users", "nickname", nullable=True)
    ''')) == []


@pytest.mark.parametrize("statement, what", [
    ('op.drop_column("users", "old")', "drop_column"),
    ('op.drop_table("old")', "drop_table"),
    ('op.drop_index("ix_old", table_name="users")', "drop_index"),
    ('op.rename_table("a", "b")', "rename_table"),
    ('op.alter_column("users", "a", new_column_name="b")', "new_column_name"),
    ('op.alter_column("users", "a", nullable=False)', "nullable=False"),
    ('op.alter_column("users", "a", type_=sa.String(10))', "type_"),
    ('with op.batch_alter_table("users") as batch:\n    batch.drop_column("old")', "drop_column"),
])
def test_migration_policy_flags_drop_without_contract(statement, what):
    problems = check_revision_source(source(statement))
    assert len(problems) == 1 and what in problems[0] and "CONTRACT = True" in problems[0]


def test_a_contract_without_min_code_head_fails():
    problems = check_revision_source(source('op.drop_column("users", "old")', "CONTRACT = True"))
    assert problems == ["CONTRACT = True needs set_min_code_head(op, \"<revision>\") in upgrade()"]


def test_a_contract_with_min_code_head_passes():
    assert check_revision_source(source('''
        op.drop_column("users", "old")
        set_min_code_head(op, "038_x")
    ''', "CONTRACT = True")) == []


def test_a_missing_marker_fails():
    problems = check_revision_source(source('op.add_column("users", sa.Column("a", sa.Integer))', ""))
    assert problems == ["no module-level CONTRACT = True | False"]


def test_a_marker_that_is_not_a_bool_fails():
    assert check_revision_source(source("pass", 'CONTRACT = "no"')) == ["no module-level CONTRACT = True | False"]


def test_revisions_after_the_baseline():
    real = history()
    after = revisions_after(BASELINE, real)
    assert BASELINE not in after
    assert (real.head in after) == (real.head != BASELINE)


def test_every_migration_after_the_baseline_declares_contract():
    after = revisions_after(BASELINE, history())
    files = {}
    for path in VERSIONS.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for revision in after:
            if f'revision = "{revision}"' in text or f"revision: str = '{revision}'" in text:
                files[revision] = (path, text)
    assert set(files) == set(after), "every revision after the baseline has its file"
    failures = {revision: check_revision_source(text) for revision, (_, text) in files.items()}
    assert {revision: problems for revision, problems in failures.items() if problems} == {}
