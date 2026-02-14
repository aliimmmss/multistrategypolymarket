@echo off
cd /d "%~dp0"
echo Starting Polymarket Bot Dashboard...
pip install fastapi uvicorn jinja2 aiofiles
python app.py
pause
