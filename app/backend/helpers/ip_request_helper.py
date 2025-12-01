from starlette.requests import Request


def get_real_client_ip(request: Request) -> str:
    """Get client IP address with proxy support"""
    # Check for forwarded headers (when behind proxy)
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()

    forwarded = request.headers.get("X-Forwarded")
    if forwarded:
        return forwarded.split(",")[0].strip()

    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip

    # Fallback to client host
    return request.client.host if request.client else "unknown"
