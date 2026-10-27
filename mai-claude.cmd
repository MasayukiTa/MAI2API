@echo off
rem MAI2API launcher: start Claude Code against the local MAI proxy.
rem Ensures the proxy is running first. Any arguments are passed to claude.
setlocal
set "HERE=%~dp0"

curl -s http://127.0.0.1:18787/health >nul 2>&1
if errorlevel 1 (
  echo Starting MAI2API proxy...
  where pythonw >nul 2>&1
  if errorlevel 1 (
    start "" /min python "%HERE%mai_proxy.py"
  ) else (
    start "" /min pythonw "%HERE%mai_proxy.py"
  )
  timeout /t 3 /nobreak >nul
)

set "ANTHROPIC_BASE_URL=http://127.0.0.1:18787"
set "ANTHROPIC_API_KEY=dummy"
set "ANTHROPIC_MODEL=sonnet"
claude %*
