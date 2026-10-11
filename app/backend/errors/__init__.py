"""RFC 9457 problems, shared by every API version; the codes themselves live in enums/errors.py.

    coded.py     CodedError, invalid(): what services and request validators raise, no FastAPI
    problems.py  ApiError (from_coded), field_problem, the problem bodies
    handlers.py  FastAPI's own errors and unhandled exceptions answered as problems
    openapi.py   problems(): the problem types a route declares in the OpenAPI document

Nothing is imported here: modules import the one they need.
"""
