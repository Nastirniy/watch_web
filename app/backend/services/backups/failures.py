"""Why a backup command did not do its work: a short code for last-run.json, the CLI and the log"""

# Refused: nothing was started (exit code 3 in the CLI); every other code is a failure (exit code 1)
REFUSED_CODES = frozenset({"no_space", "unsupported_objects", "busy"})


class BackupFailure(Exception):
    """code is one of dump_failed, timeout, no_space, copy_failed, unsupported_objects, tools_missing, busy,
    error; str() is the code only, never a path, SQL or tool output"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
        self.refused = code in REFUSED_CODES
