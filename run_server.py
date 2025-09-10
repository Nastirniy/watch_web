#!/usr/bin/env python3
"""
Simple server runner to test if the app works
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:fastapi_app",
        host="127.0.0.1",
        port=60800,
        reload=False,
        log_level="info"
    )