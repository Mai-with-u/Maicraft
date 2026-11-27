@echo off
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
  echo uv not found. Install it first: https://docs.astral.sh/uv/
  exit /b 1
)
uv sync
uv run maicraft %*
