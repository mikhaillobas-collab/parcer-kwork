@echo off
chcp 65001 > nul
echo ========================================================
echo   Упаковка авторизованной сессии браузера для сервера
echo ========================================================
echo.
cd /d "%~dp0\.."

if not exist "browser_profile" (
    echo [ОШИБКА] Папка browser_profile не найдена!
    echo Сначала запустите бота на вашем ПК и войдите в аккаунт Kwork.
    pause
    exit /b 1
)

echo Закрываем браузерные процессы Chromium/Playwright...
taskkill /f /im chrome.exe /fi "WINDOWTITLE eq about:blank*" >nul 2>&1

echo Создание архива session_kwork.tar.gz...
tar -czf session_kwork.tar.gz browser_profile

if exist "session_kwork.tar.gz" (
    echo.
    echo [УСПЕХ] Архив session_kwork.tar.gz успешно создан!
    echo Теперь загрузите этот файл на ваш сервер и распакуйте:
    echo tar -xzf session_kwork.tar.gz
    echo.
) else (
    echo [ОШИБКА] Не удалось создать архив через tar.
)
pause
