@echo off
echo [1/3] cd to project dir
cd /d D:\workspace\MediaCrawler
set UV_PYTHON_INSTALL_DIR=D:\uv-python
echo [2/3] ensure Python 3.11.14 at D:\uv-python
if not exist "D:\uv-python\cpython-3.11.14-windows-x86_64-none\python.exe" (
  if exist "D:\uv-python" rmdir /s /q "D:\uv-python"
)
uv python install 3.11.14
echo [3/3] starting uvicorn on port 8081
uv run uvicorn api.main:app --host 127.0.0.1 --port 8081
echo.
echo done, press any key to close
pause
