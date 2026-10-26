"""The audit log: one AuditLog row per audited event, added to the session of the change.

writer.py: create_audit_log (event, category, params, scope, request id) and serialize_for_audit; legacy.py: the
keyword form of the writers that do not name their event yet (action=, resource_type=, details=)."""
from app.backend.services.audit import legacy
from app.backend.services.audit.subjects import home_node, home_scope, node_name, role_name, user_facts
from app.backend.services.audit.writer import create_audit_log, serialize_for_audit

__all__ = ["create_audit_log", "serialize_for_audit", "legacy", "home_node", "home_scope", "node_name", "role_name",
           "user_facts"]
