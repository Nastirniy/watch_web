"""Authentication and authorization, shared by every API version.

Every protected route goes through one chain, resolved once per request by FastAPI:

    Depends(require(...))   require.py       permissions of the route, 403
    -> get_auth_context     dependencies.py  permissions, roles, post scope (context.py)
    -> get_current_user     dependencies.py  account, live session, client certificate, touch
    -> get_token_claims     dependencies.py  Bearer access token, bound to a session (sid)

Login, tokens and session revocation are AuthService (service.py); session lifetime and presence are
sessions.py and presence.py; data scope checks are access_control.py. Nothing is imported here: modules,
models and filters import the module they need.
"""
