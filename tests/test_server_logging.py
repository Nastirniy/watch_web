"""The server log as the running app writes it: uvicorn's own messages, and one record per unhandled error."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run(script: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run([sys.executable, "-c", script], cwd=PROJECT_ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


# Runs app/main.py as __main__ with uvicorn.run stubbed, then applies the captured options the way uvicorn.run does
# (Config.configure_logging) and logs one message on each uvicorn logger
MAIN_SCRIPT = f"""
import logging, runpy, sys
import uvicorn
from loguru import logger

captured = {{}}
uvicorn.run = lambda app, **options: captured.update(options)
main = {str(PROJECT_ROOT / "app" / "main.py")!r}
sys.argv = [main]
runpy.run_path(main, run_name="__main__")

uvicorn.Config("app.main:fastapi_app", **captured).configure_logging()
seen = []
logger.add(lambda message: seen.append(message.record["message"]), level="INFO")
logging.getLogger("uvicorn.error").info("Uvicorn running on %s", "probe")
logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", "/probe", "1.1", 200)
print("SEEN=" + "|".join(seen))
"""


def test_uvicorn_messages_reach_the_app_log_when_started_from_main():
    """main.py configured logging (loguru intercepts uvicorn's loggers, propagate=False), then uvicorn.run applied
    its default LOGGING_CONFIG, which removed the handler of uvicorn.error and kept propagate=False: 'Started server
    process', 'Application startup complete' and 'Uvicorn running on ...' went nowhere."""
    result = _run(MAIN_SCRIPT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'SEEN=Uvicorn running on probe|127.0.0.1:1 - "GET /probe HTTP/1.1" 200' in result.stdout, \
        result.stdout + result.stderr


SECRET = "refresh-token-that-must-stay-out-of-the-log"

# The real app (its logging, error handling and middleware) under a real uvicorn server, without the lifespan (no
# database), plus one route that fails with a token in its request body
FAILING_ROUTE_SCRIPT = f"""
import json, socket, threading, time, urllib.error, urllib.request
import uvicorn
from app.main import fastapi_app
from app.backend.api.v1.models.request.auth import RefreshRequest

async def failing_route(refresh_data: RefreshRequest):
    raise RuntimeError("the failing route failed")

fastapi_app.add_api_route("/failing-route", failing_route, methods=["POST"])

sock = socket.socket()
sock.bind(("127.0.0.1", 0))
port = sock.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(fastapi_app, lifespan="off", log_config=None))
thread = threading.Thread(target=server.run, kwargs={{"sockets": [sock]}}, daemon=True)
thread.start()
for _ in range(200):
    if server.started:
        break
    time.sleep(0.05)

request = urllib.request.Request(f"http://127.0.0.1:{{port}}/failing-route", method="POST",
                                 data=json.dumps({{"refresh_token": {SECRET!r}}}).encode(),
                                 headers={{"Content-Type": "application/json"}})
try:
    urllib.request.urlopen(request, timeout=10)
except urllib.error.HTTPError as error:
    answer = {{"status": error.code, "content_type": error.headers["content-type"],
               "request_id": error.headers["x-request-id"], "body": json.loads(error.read())}}
time.sleep(0.3)
server.should_exit = True
thread.join(timeout=10)
print("ANSWER=" + json.dumps(answer))
"""


def test_an_unhandled_error_is_logged_once_under_its_error_id_without_request_values():
    """The error_id of a 500 is what a user quotes: the log must have it once, with the traceback. The traceback
    showed the values of every frame (loguru's diagnose), the request body with its token included, and Starlette
    re-raised the error after the handler answered, so uvicorn logged it a second time without the error_id."""
    result = _run(FAILING_ROUTE_SCRIPT)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    answer = json.loads(re.search(r"^ANSWER=(.*)$", result.stdout, re.M).group(1))
    log = re.sub(r"\x1b\[[0-9;]*m", "", output.replace(f"ANSWER={json.dumps(answer)}", ""))   # no colours

    assert answer["status"] == 500 and answer["content_type"] == "application/problem+json"
    error_id = answer["body"]["error_id"]
    assert answer["body"]["code"] == "internal_error" and re.fullmatch(r"[0-9a-f]{32}", error_id)

    assert log.count(f"Unhandled error {error_id} on POST /failing-route") == 1, log
    assert log.count("Traceback (most recent call last)") == 1, log
    assert "RuntimeError: the failing route failed" in log
    assert "Exception in ASGI application" not in log, "the server logged the error a second time"
    assert SECRET not in log, "request values reached the log"


def test_the_error_id_is_the_request_id_and_every_log_line_of_the_request_carries_it():
    """One id for support: the error_id the user quotes is the X-Request-ID of the response (behind nginx: nginx's
    $request_id, in its access log too), and every API log line of that request carries it"""
    result = _run(FAILING_ROUTE_SCRIPT)
    output = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout + result.stderr)
    assert result.returncode == 0, output
    answer = json.loads(re.search(r"^ANSWER=(.*)$", output, re.M).group(1))
    request_id = answer["request_id"]
    assert answer["body"]["error_id"] == request_id
    lines = [line for line in output.splitlines() if line.startswith("20")]   # log records, not traceback lines
    of_request = [line for line in lines if f"| {request_id} |" in line]
    assert any("Unhandled error" in line for line in of_request), output
    assert any('"POST /failing-route HTTP/1.1" 500' in line for line in of_request), "the access log line"
    assert all(f"| {request_id} |" not in line for line in lines if "Uvicorn running on" in line), \
        "a line outside any request carries no request id"
