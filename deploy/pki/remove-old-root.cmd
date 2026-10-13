@echo off
rem Removes the old root certificate from a Windows client after the move to the new client CA (PKI.md).
rem   remove-old-root.cmd THUMBPRINT          machine store, run as administrator
rem   remove-old-root.cmd THUMBPRINT user     store of the current user
rem THUMBPRINT: SHA-1 of the old root certificate itself, 40 hex characters without colons:
rem   openssl x509 -in rootCA.crt -noout -fingerprint -sha1     (remove the colons)
rem   certutil -dump rootCA.crt                                 (the line "Cert Hash(sha1)")
rem Not certutil -hashfile: it hashes the file's bytes, the thumbprint only for a DER file, never for PEM.
if "%~1"=="" (
    echo Usage: %~nx0 THUMBPRINT [user]
    exit /b 2
)
if /i "%~2"=="user" (
    echo Removing certificate %~1 from the Root store of the current user
    certutil -user -delstore Root %~1
) else (
    echo Removing certificate %~1 from the Root store of this machine ^(needs administrator^)
    certutil -delstore Root %~1
)
exit /b %errorlevel%
