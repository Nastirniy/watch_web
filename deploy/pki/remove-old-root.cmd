@echo off
rem Removes the old root certificate from a Windows client after the move to the new client CA (PKI.md).
rem   remove-old-root.cmd THUMBPRINT          machine store, run as administrator
rem   remove-old-root.cmd THUMBPRINT user     store of the current user
rem THUMBPRINT: SHA-1 of the old root, hex without colons (certutil -hashfile rootCA.crt SHA1)
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
