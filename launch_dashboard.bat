@echo off
echo ==========================================================
echo   Study Sentinel Problem 1 - ATLAS Interactive Prototype
echo ==========================================================
echo Starting ATLAS Web Console at http://localhost:8080 ...
start http://localhost:8080
python app.py --port 8080
pause
