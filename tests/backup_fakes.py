"""Fake database client tools for the backup tests: Python scripts that behave like mariadb-dump / mariadb.

Each fake reads its behaviour from <name>.json next to it and records argv, environment and the content of the
--defaults-extra-file it was given into <name>.record.json (one JSON object per call, appended as a list).
"""
import json
import os
import sys
from pathlib import Path

from app.backend.services.backups.client_tools import ClientTools

DUMP_BODY = (
    "/*M!999999\\- enable the sandbox mode */\n"
    "-- MariaDB dump 10.19  Distrib 11.8.6-MariaDB, for debian-linux-gnu (x86_64)\n"
    "--\n"
    "-- Host: 127.0.0.1    Database: radio_monitoring\n"
    "-- ------------------------------------------------------\n"
    "-- Server version\t11.8.6-MariaDB-0ubuntu0.26.04.1\n"
    "DROP TABLE IF EXISTS `a`;\n"
    "CREATE TABLE `a` (\n  `id` int(11) NOT NULL\n) ENGINE=InnoDB;\n"
    "INSERT INTO `a` VALUES (1),(2);\n"
    "DROP TABLE IF EXISTS `alembic_version`;\n"
    "CREATE TABLE `alembic_version` (\n  `version_num` varchar(32) NOT NULL\n) ENGINE=InnoDB;\n"
    "INSERT INTO `alembic_version` VALUES ('028_no_actor_foreign_keys');\n"
)

_SCRIPT = r'''
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
name = os.path.splitext(os.path.basename(__file__))[0]
behaviour = json.load(open(os.path.join(here, name + ".json"), encoding="utf-8"))
if "--version" in sys.argv:
    print(behaviour.get("version", name + " from 11.8.6-MariaDB, client 15.2"))
    sys.exit(0)
defaults = ""
for arg in sys.argv[1:]:
    if arg.startswith("--defaults-extra-file="):
        defaults = open(arg.split("=", 1)[1], encoding="utf-8").read()
record_path = os.path.join(here, name + ".record.json")
records = json.load(open(record_path, encoding="utf-8")) if os.path.exists(record_path) else []
records.append({"argv": sys.argv[1:], "env": dict(os.environ), "defaults": defaults,
                "stdin": sys.stdin.read() if behaviour.get("read_stdin") else None})
json.dump(records, open(record_path, "w", encoding="utf-8"))
time.sleep(behaviour.get("sleep", 0))
sys.stdout.buffer.write(behaviour.get("body", "").encode("utf-8"))
sys.stdout.flush()
sys.stderr.write(behaviour.get("stderr", ""))
sys.exit(behaviour.get("exit_code", 0))
'''


class FakeTool:
    def __init__(self, directory: Path, name: str):
        self.directory = directory
        self.name = name
        (directory / f"{name}.py").write_text(_SCRIPT, encoding="utf-8")
        if os.name == "nt":
            self.path = directory / f"{name}.cmd"
            self.path.write_text(f'@"{sys.executable}" "{directory / (name + ".py")}" %*\n', encoding="utf-8")
        else:
            self.path = directory / name
            self.path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{directory / (name + ".py")}" "$@"\n',
                                 encoding="utf-8")
            self.path.chmod(0o755)
        self.configure()

    def configure(self, **behaviour) -> None:
        (self.directory / f"{self.name}.json").write_text(json.dumps(behaviour), encoding="utf-8")

    def records(self) -> list:
        path = self.directory / f"{self.name}.record.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


class Fakes:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.dump = FakeTool(directory, "mariadb-dump")
        self.client = FakeTool(directory, "mariadb")
        self.dump.configure(body=DUMP_BODY)
        self.tools = ClientTools(dump=str(self.dump.path), client=str(self.client.path), product="mariadb")
