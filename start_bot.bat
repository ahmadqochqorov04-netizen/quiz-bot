@echo off
chcp 65001 >nul
title Lug'at Quiz Boti
set PY=python
python --version >nul 2>&1
if errorlevel 1 (
  py --version >nul 2>&1
  if errorlevel 1 (
    echo Python topilmadi.
    echo 1^) https://www.python.org/downloads/ saytidan Python yuklab oling.
    echo 2^) O'rnatishda "Add python.exe to PATH" katagini ALBATTA belgilang.
    echo 3^) Keyin shu faylni qayta ishga tushiring.
    pause
    exit /b
  )
  set PY=py
)
%PY% -m pip install --quiet --upgrade "python-telegram-bot>=20"
echo.
echo Bot ishga tushmoqda... (to'xtatish uchun shu oynani yoping)
%PY% lugat_quiz_bot.py
pause
