@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Osen Kusna - local bot
python start_local.py
pause
