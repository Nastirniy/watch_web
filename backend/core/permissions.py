"""
Permission definitions for role-based access control (RBAC).
Defines all system permissions and their hierarchies.
"""

from enum import Enum
from typing import Dict, List, Set


class ResourceType(str, Enum):
    """System resource types for permissions."""

    USERS = "users"
    ROLES = "roles"
    PERMISSIONS = "permissions"
    POSTS = "posts"
    SIGNALS = "signals"
    OBSERVATIONS = "observations"
    MEDIA = "media"
    EXPORTS = "exports"
    SYSTEM = "system"
    AUDIT = "audit"
    REPORTS = "reports"


class Action(str, Enum):
    """Permission actions."""

    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    MANAGE = "manage"  # Full control
    EXECUTE = "execute"  # For system actions
    APPROVE = "approve"  # For approval workflows


class SystemPermissions:
    """System permission definitions."""

    # User management permissions
    USER_CREATE = "create:users"
    USER_READ = "read:users"
    USER_UPDATE = "update:users"
    USER_DELETE = "delete:users"
    USER_MANAGE = "manage:users"

    # Role management permissions
    ROLE_CREATE = "create:roles"
    ROLE_READ = "read:roles"
    ROLE_UPDATE = "update:roles"
    ROLE_DELETE = "delete:roles"
    ROLE_MANAGE = "manage:roles"

    # Permission management permissions
    PERMISSION_CREATE = "create:permissions"
    PERMISSION_READ = "read:permissions"
    PERMISSION_UPDATE = "update:permissions"
    PERMISSION_DELETE = "delete:permissions"
    PERMISSION_MANAGE = "manage:permissions"

    # Post management permissions
    POST_CREATE = "create:posts"
    POST_READ = "read:posts"
    POST_UPDATE = "update:posts"
    POST_DELETE = "delete:posts"
    POST_MANAGE = "manage:posts"

    # Signal type management permissions
    SIGNAL_CREATE = "create:signals"
    SIGNAL_READ = "read:signals"
    SIGNAL_UPDATE = "update:signals"
    SIGNAL_DELETE = "delete:signals"
    SIGNAL_MANAGE = "manage:signals"

    # Observation permissions
    OBSERVATION_CREATE = "create:observations"
    OBSERVATION_READ = "read:observations"
    OBSERVATION_UPDATE = "update:observations"
    OBSERVATION_DELETE = "delete:observations"
    OBSERVATION_MANAGE = "manage:observations"
    OBSERVATION_APPROVE = "approve:observations"

    # Media permissions
    MEDIA_CREATE = "create:media"
    MEDIA_READ = "read:media"
    MEDIA_UPDATE = "update:media"
    MEDIA_DELETE = "delete:media"
    MEDIA_MANAGE = "manage:media"

    # Export permissions
    EXPORT_CREATE = "create:exports"
    EXPORT_READ = "read:exports"
    EXPORT_UPDATE = "update:exports"
    EXPORT_DELETE = "delete:exports"
    EXPORT_MANAGE = "manage:exports"
    EXPORT_EXECUTE = "execute:exports"

    # System administration permissions
    SYSTEM_CONFIG = "manage:system"
    SYSTEM_BACKUP = "execute:system"
    SYSTEM_MAINTENANCE = "execute:maintenance"

    # Audit permissions
    AUDIT_READ = "read:audit"
    AUDIT_MANAGE = "manage:audit"

    # Report permissions
    REPORT_CREATE = "create:reports"
    REPORT_READ = "read:reports"
    REPORT_MANAGE = "manage:reports"


class PermissionHierarchy:
    """Permission hierarchy definitions."""

    # Define permission implications (higher permissions include lower ones)
    HIERARCHY = {
        # User permissions
        SystemPermissions.USER_MANAGE: [
            SystemPermissions.USER_CREATE,
            SystemPermissions.USER_READ,
            SystemPermissions.USER_UPDATE,
            SystemPermissions.USER_DELETE,
        ],

        # Role permissions
        SystemPermissions.ROLE_MANAGE: [
            SystemPermissions.ROLE_CREATE,
            SystemPermissions.ROLE_READ,
            SystemPermissions.ROLE_UPDATE,
            SystemPermissions.ROLE_DELETE,
        ],

        # Permission permissions
        SystemPermissions.PERMISSION_MANAGE: [
            SystemPermissions.PERMISSION_CREATE,
            SystemPermissions.PERMISSION_READ,
            SystemPermissions.PERMISSION_UPDATE,
            SystemPermissions.PERMISSION_DELETE,
        ],

        # Post permissions
        SystemPermissions.POST_MANAGE: [
            SystemPermissions.POST_CREATE,
            SystemPermissions.POST_READ,
            SystemPermissions.POST_UPDATE,
            SystemPermissions.POST_DELETE,
        ],

        # Signal permissions
        SystemPermissions.SIGNAL_MANAGE: [
            SystemPermissions.SIGNAL_CREATE,
            SystemPermissions.SIGNAL_READ,
            SystemPermissions.SIGNAL_UPDATE,
            SystemPermissions.SIGNAL_DELETE,
        ],

        # Observation permissions
        SystemPermissions.OBSERVATION_MANAGE: [
            SystemPermissions.OBSERVATION_CREATE,
            SystemPermissions.OBSERVATION_READ,
            SystemPermissions.OBSERVATION_UPDATE,
            SystemPermissions.OBSERVATION_DELETE,
            SystemPermissions.OBSERVATION_APPROVE,
        ],

        # Media permissions
        SystemPermissions.MEDIA_MANAGE: [
            SystemPermissions.MEDIA_CREATE,
            SystemPermissions.MEDIA_READ,
            SystemPermissions.MEDIA_UPDATE,
            SystemPermissions.MEDIA_DELETE,
        ],

        # Export permissions
        SystemPermissions.EXPORT_MANAGE: [
            SystemPermissions.EXPORT_CREATE,
            SystemPermissions.EXPORT_READ,
            SystemPermissions.EXPORT_UPDATE,
            SystemPermissions.EXPORT_DELETE,
            SystemPermissions.EXPORT_EXECUTE,
        ],

        # System permissions
        SystemPermissions.SYSTEM_CONFIG: [
            SystemPermissions.SYSTEM_BACKUP,
            SystemPermissions.SYSTEM_MAINTENANCE,
        ],

        # Audit permissions
        SystemPermissions.AUDIT_MANAGE: [
            SystemPermissions.AUDIT_READ,
        ],

        # Report permissions
        SystemPermissions.REPORT_MANAGE: [
            SystemPermissions.REPORT_CREATE,
            SystemPermissions.REPORT_READ,
        ],
    }


class DefaultRoles:
    """Default system roles and their permissions."""

    # Superuser role (has all permissions)
    SUPERUSER = {
        "name": "superuser",
        "display_name": "System Superuser",
        "description": "Full system access with all permissions",
        "is_system": True,
        "priority": 1000,
        "permissions": ["*"],  # Special case: all permissions
    }

    # Administrator role
    ADMINISTRATOR = {
        "name": "administrator",
        "display_name": "System Administrator",
        "description": "Full administrative access to system management",
        "is_system": True,
        "priority": 900,
        "permissions": [
            SystemPermissions.USER_MANAGE,
            SystemPermissions.ROLE_MANAGE,
            SystemPermissions.PERMISSION_MANAGE,
            SystemPermissions.POST_MANAGE,
            SystemPermissions.SIGNAL_MANAGE,
            SystemPermissions.OBSERVATION_MANAGE,
            SystemPermissions.MEDIA_MANAGE,
            SystemPermissions.EXPORT_MANAGE,
            SystemPermissions.SYSTEM_CONFIG,
            SystemPermissions.AUDIT_MANAGE,
            SystemPermissions.REPORT_MANAGE,
        ],
    }

    # Operator role
    OPERATOR = {
        "name": "operator",
        "display_name": "System Operator",
        "description": "Operational access to posts and observations",
        "is_system": True,
        "priority": 800,
        "permissions": [
            SystemPermissions.USER_READ,
            SystemPermissions.POST_READ,
            SystemPermissions.POST_UPDATE,
            SystemPermissions.SIGNAL_READ,
            SystemPermissions.OBSERVATION_MANAGE,
            SystemPermissions.MEDIA_MANAGE,
            SystemPermissions.EXPORT_CREATE,
            SystemPermissions.EXPORT_READ,
            SystemPermissions.REPORT_CREATE,
            SystemPermissions.REPORT_READ,
        ],
    }

    # Analyst role
    ANALYST = {
        "name": "analyst",
        "display_name": "Signal Analyst",
        "description": "Analysis and reporting access",
        "is_system": True,
        "priority": 700,
        "permissions": [
            SystemPermissions.POST_READ,
            SystemPermissions.SIGNAL_READ,
            SystemPermissions.OBSERVATION_READ,
            SystemPermissions.OBSERVATION_UPDATE,
            SystemPermissions.OBSERVATION_APPROVE,
            SystemPermissions.MEDIA_READ,
            SystemPermissions.EXPORT_CREATE,
            SystemPermissions.EXPORT_READ,
            SystemPermissions.REPORT_MANAGE,
        ],
    }

    # Observer role
    OBSERVER = {
        "name": "observer",
        "display_name": "Signal Observer",
        "description": "Basic observation creation and viewing",
        "is_system": True,
        "priority": 600,
        "permissions": [
            SystemPermissions.POST_READ,
            SystemPermissions.SIGNAL_READ,
            SystemPermissions.OBSERVATION_CREATE,
            SystemPermissions.OBSERVATION_READ,
            SystemPermissions.OBSERVATION_UPDATE,  # Own observations only
            SystemPermissions.MEDIA_CREATE,
            SystemPermissions.MEDIA_READ,
            SystemPermissions.EXPORT_CREATE,
            SystemPermissions.EXPORT_READ,
        ],
    }

    # Viewer role
    VIEWER = {
        "name": "viewer",
        "display_name": "Read-Only Viewer",
        "description": "Read-only access to observations and reports",
        "is_system": True,
        "priority": 500,
        "permissions": [
            SystemPermissions.POST_READ,
            SystemPermissions.SIGNAL_READ,
            SystemPermissions.OBSERVATION_READ,
            SystemPermissions.MEDIA_READ,
            SystemPermissions.REPORT_READ,
        ],
    }

    # Guest role
    GUEST = {
        "name": "guest",
        "display_name": "Guest User",
        "description": "Limited access for guest users",
        "is_system": True,
        "priority": 100,
        "permissions": [
            SystemPermissions.OBSERVATION_READ,  # Public observations only
            SystemPermissions.REPORT_READ,  # Public reports only
        ],
    }


def get_all_permissions() -> List[Dict[str, str]]:
    """
    Get all system permissions with metadata.

    Returns:
        List of permission dictionaries
    """
    permissions = []

    # Get all permission constants from SystemPermissions
    for attr_name in dir(SystemPermissions):
        if not attr_name.startswith('_'):
            permission_name = getattr(SystemPermissions, attr_name)
            if isinstance(permission_name, str) and ':' in permission_name:
                action, resource = permission_name.split(':', 1)

                permissions.append({
                    "name": permission_name,
                    "display_name": f"{action.title()} {resource.title()}",
                    "description": f"Permission to {action} {resource}",
                    "resource": resource,
                    "action": action,
                })

    return permissions


def get_all_default_roles() -> List[Dict]:
    """
    Get all default system roles.

    Returns:
        List of role dictionaries
    """
    roles = []

    for attr_name in dir(DefaultRoles):
        if not attr_name.startswith('_'):
            role_data = getattr(DefaultRoles, attr_name)
            if isinstance(role_data, dict):
                roles.append(role_data)

    return roles


def expand_permissions(permissions: List[str]) -> Set[str]:
    """
    Expand permissions based on hierarchy.

    Args:
        permissions: List of permission names

    Returns:
        Set of expanded permissions including implied permissions
    """
    expanded = set(permissions)

    # Handle wildcard permission
    if "*" in permissions:
        # Get all system permissions
        all_perms = get_all_permissions()
        return {perm["name"] for perm in all_perms}

    # Expand based on hierarchy
    for permission in permissions:
        if permission in PermissionHierarchy.HIERARCHY:
            implied_permissions = PermissionHierarchy.HIERARCHY[permission]
            expanded.update(implied_permissions)

            # Recursively expand implied permissions
            expanded.update(expand_permissions(implied_permissions))

    return expanded


def check_permission(user_permissions: List[str], required_permission: str) -> bool:
    """
    Check if user has required permission.

    Args:
        user_permissions: List of user's permissions
        required_permission: Required permission to check

    Returns:
        True if user has permission, False otherwise
    """
    # Expand user permissions based on hierarchy
    expanded_permissions = expand_permissions(user_permissions)

    return required_permission in expanded_permissions


def get_permission_description(permission: str) -> str:
    """
    Get human-readable description for a permission.

    Args:
        permission: Permission name

    Returns:
        Human-readable description
    """
    if ':' not in permission:
        return permission

    action, resource = permission.split(':', 1)

    action_map = {
        "create": "Create",
        "read": "View",
        "update": "Edit",
        "delete": "Delete",
        "manage": "Manage",
        "execute": "Execute",
        "approve": "Approve",
    }

    resource_map = {
        "users": "users",
        "roles": "roles",
        "permissions": "permissions",
        "posts": "monitoring posts",
        "signals": "signal types",
        "observations": "observations",
        "media": "media files",
        "exports": "data exports",
        "system": "system settings",
        "audit": "audit logs",
        "reports": "reports",
    }

    action_desc = action_map.get(action, action)
    resource_desc = resource_map.get(resource, resource)

    return f"{action_desc} {resource_desc}"


def validate_permission_name(permission: str) -> bool:
    """
    Validate permission name format.

    Args:
        permission: Permission name to validate

    Returns:
        True if valid, False otherwise
    """
    if permission == "*":
        return True

    if ':' not in permission:
        return False

    try:
        action, resource = permission.split(':', 1)

        # Check if action and resource are valid
        valid_actions = [a.value for a in Action]
        valid_resources = [r.value for r in ResourceType]

        return action in valid_actions and resource in valid_resources

    except ValueError:
        return False


# Permission groups for easier management
PERMISSION_GROUPS = {
    "User Management": [
        SystemPermissions.USER_CREATE,
        SystemPermissions.USER_READ,
        SystemPermissions.USER_UPDATE,
        SystemPermissions.USER_DELETE,
        SystemPermissions.USER_MANAGE,
    ],
    "Role Management": [
        SystemPermissions.ROLE_CREATE,
        SystemPermissions.ROLE_READ,
        SystemPermissions.ROLE_UPDATE,
        SystemPermissions.ROLE_DELETE,
        SystemPermissions.ROLE_MANAGE,
    ],
    "Post Management": [
        SystemPermissions.POST_CREATE,
        SystemPermissions.POST_READ,
        SystemPermissions.POST_UPDATE,
        SystemPermissions.POST_DELETE,
        SystemPermissions.POST_MANAGE,
    ],
    "Signal Management": [
        SystemPermissions.SIGNAL_CREATE,
        SystemPermissions.SIGNAL_READ,
        SystemPermissions.SIGNAL_UPDATE,
        SystemPermissions.SIGNAL_DELETE,
        SystemPermissions.SIGNAL_MANAGE,
    ],
    "Observation Management": [
        SystemPermissions.OBSERVATION_CREATE,
        SystemPermissions.OBSERVATION_READ,
        SystemPermissions.OBSERVATION_UPDATE,
        SystemPermissions.OBSERVATION_DELETE,
        SystemPermissions.OBSERVATION_MANAGE,
        SystemPermissions.OBSERVATION_APPROVE,
    ],
    "Media Management": [
        SystemPermissions.MEDIA_CREATE,
        SystemPermissions.MEDIA_READ,
        SystemPermissions.MEDIA_UPDATE,
        SystemPermissions.MEDIA_DELETE,
        SystemPermissions.MEDIA_MANAGE,
    ],
    "Export Management": [
        SystemPermissions.EXPORT_CREATE,
        SystemPermissions.EXPORT_READ,
        SystemPermissions.EXPORT_UPDATE,
        SystemPermissions.EXPORT_DELETE,
        SystemPermissions.EXPORT_MANAGE,
        SystemPermissions.EXPORT_EXECUTE,
    ],
    "System Administration": [
        SystemPermissions.SYSTEM_CONFIG,
        SystemPermissions.SYSTEM_BACKUP,
        SystemPermissions.SYSTEM_MAINTENANCE,
        SystemPermissions.AUDIT_MANAGE,
    ],
    "Reporting": [
        SystemPermissions.REPORT_CREATE,
        SystemPermissions.REPORT_READ,
        SystemPermissions.REPORT_MANAGE,
    ],
}
