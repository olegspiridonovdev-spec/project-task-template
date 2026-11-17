@echo off
rem Windows has no shebang, so `bin\guard` cannot be executed there directly.
rem This is the same program under the name cmd.exe will start; PATHEXT means
rem that `bin\guard` typed without an extension finds this file, so a command
rem written for a Mac works unchanged: .tasks\bin\guard list --status ready
rem
rem There is still exactly one implementation - the extensionless file beside
rem this one. A shim that did anything but hand it to a python would be a second
rem board to keep in step.
rem
rem Written as if/else rather than `&& ... || ...`: in cmd the `||` branch also
rem fires when the program itself exits non-zero, and a non-zero exit is the
rem normal answer from `bin\task lint` and `bin\task doctor`. The chain would
rem run the board a second time and report the wrong verdict.
where /q py.exe
if %ERRORLEVEL%==0 (
  py -3 "%~dp0guard" %*
) else (
  python "%~dp0guard" %*
)
exit /b %ERRORLEVEL%
