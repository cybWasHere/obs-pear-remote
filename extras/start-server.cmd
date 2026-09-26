@echo off
rem Serves this repo on http://127.0.0.1:9870 (loopback only) for the OBS dock and card.
rem Double-click to start, close the window to stop. To start it at login, put a shortcut to
rem this file in the Startup folder (Win+R, shell:startup) and set the shortcut to Run: Minimized.
title obs-pear-remote server
rem use the first Python that actually runs: py/python can exist but point at a removed install
set PY=
for %%p in (py python python3) do if not defined PY (%%p -c "import http.server" >NUL 2>NUL && set "PY=%%p")
if not defined PY (
  echo Python 3 was not found, or it does not start.
  echo Install it from https://www.python.org/downloads/ and run this file again.
  pause
  exit /b 1
)
%PY% -m http.server 9870 --bind 127.0.0.1 --directory "%~dp0.."
if errorlevel 1 pause
