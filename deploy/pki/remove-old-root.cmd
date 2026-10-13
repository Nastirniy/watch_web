@echo off
rem Removes the old root certificate from a Windows client after the move to the new client CA (PKI.md).
rem   remove-old-root.cmd THUMBPRINT          machine store, run as administrator
rem   remove-old-root.cmd THUMBPRINT user     store of the current user
rem THUMBPRINT: SHA-1 of the old root certificate itself, exactly 40 hex characters without colons:
rem   openssl x509 -in rootCA.crt -noout -fingerprint -sha1     (remove the colons)
rem   certutil -dump rootCA.crt                                 (the line "Cert Hash(sha1)")
rem Not certutil -hashfile: it hashes the file's bytes, the thumbprint only for a DER file, never for PEM.
rem Anything else is refused: certutil also takes an index, a serial number or a name there and would delete
rem whatever certificate matches it.
setlocal EnableExtensions DisableDelayedExpansion
set "THUMBPRINT=%~1"
set "STORE=%~2"
setlocal EnableDelayedExpansion
if not defined THUMBPRINT goto usage
rem Exactly 40 characters
if "!THUMBPRINT:~39,1!"=="" goto usage
if not "!THUMBPRINT:~40!"=="" goto usage
rem Only hex digits: remove each of them (the replacement ignores case); the sentinel x must be all that is left
set "REST=x!THUMBPRINT!"
for %%c in (0 1 2 3 4 5 6 7 8 9 a b c d e f) do set "REST=!REST:%%c=!"
if not "!REST!"=="x" goto usage
if /i "!STORE!"=="user" (
    echo Removing certificate !THUMBPRINT! from the Root store of the current user
    certutil -user -delstore Root "!THUMBPRINT!"
) else if "!STORE!"=="" (
    echo Removing certificate !THUMBPRINT! from the Root store of this machine ^(needs administrator^)
    certutil -delstore Root "!THUMBPRINT!"
) else (
    goto usage
)
exit /b !errorlevel!

:usage
echo Usage: %~nx0 THUMBPRINT [user]
echo   THUMBPRINT: the SHA-1 thumbprint of the old root, exactly 40 hex characters without colons (PKI.md)
exit /b 1
