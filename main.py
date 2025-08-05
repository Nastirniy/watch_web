from fastapi import FastAPI

from config import fastapi_server_config as fastapi_config

app = FastAPI(
    host=fastapi_config.host,
    port=fastapi_config.port,
    workers=fastapi_config.workers,
    reload=fastapi_config.reload,
    debug=fastapi_config.debug,
    title=fastapi_config.title,
    description=fastapi_config.description,
    docs_url=fastapi_config.docs_url,
    redoc_url=fastapi_config.redoc_url,
    # openapi_url=fastapi_config.openapi_url,
)
# uvicorn main:app --reload --port 60800 (WE NEED RUN IT DIRECTLY IN uvicorn.run() on Prod)


@app.get("/")
async def root():
    return {"message": "Hello World"}


@app.get("/hello/{name}")
async def say_hello(name: str):
    return {"message": f"Hello {name}"}
