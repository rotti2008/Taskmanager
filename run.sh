#!/usr/bin/env bash
# Startet den Taskmanager; legt die venv beim ersten Start an.
cd "$(dirname "$0")"
[ -d .venv ] || python -m venv .venv
.venv/bin/pip install -q -r requirements.txt
exec .venv/bin/python skript.py
