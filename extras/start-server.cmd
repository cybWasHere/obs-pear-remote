@echo off
rem Serves this repo on http://127.0.0.1:9870 (loopback only) for the OBS dock and card.
rem Double-click to start, close the window to stop. To start it at login, put a shortcut to
rem this file in the Startup folder (Win+R, shell:startup) and set the shortcut to Run: Minimized.
title obs-pear-remote server
where py >NUL 2>NUL
if %errorlevel%==0 (set PY=py) else (set PY=python)
%PY% -m http.server 9870 --bind 127.0.0.1 --directory "%~dp0.."
if errorlevel 1 pause
