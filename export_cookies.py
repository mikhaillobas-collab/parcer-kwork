import sys
import json
import base64
from pathlib import Path
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
from playwright.sync_api import sync_playwright
import config


def export_kwork_cookies():
    print("=" * 60)
    print("🔑 ЭКСПОРТ КУКОВ АВТОРИЗАЦИИ KWORK ДЛЯ BOTHOST")
    print("=" * 60)

    if not config.USER_DATA_DIR.exists():
        print("❌ ОШИБКА: Папка browser_profile не найдена.")
        print("Сначала войдите в Kwork на вашем компьютере.")
        return

    print("Извлечение сессии из браузерного профиля...")
    import shutil
    import tempfile
    
    # Создаем временную копию профиля, чтобы избежать блокировки запущенным процессом
    temp_dir = tempfile.mkdtemp()
    try:
        shutil.copytree(str(config.USER_DATA_DIR), temp_dir, dirs_exist_ok=True, ignore=shutil.ignore_patterns("*.lock", "Singleton*"))
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=temp_dir,
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            cookies = context.cookies(["https://kwork.ru", "https://api.kwork.ru"])
            context.close()
    except Exception as e:
        print(f"\n⚠️ Файлы браузера сейчас заблокированы запущенным ботом.")
        print("Пожалуйста, нажмите Ctrl+C в окне main.py, чтобы остановить бота на 5 секунд,")
        print("запустите этот скрипт еще раз, и затем перезапустите main.py.\n")
        return
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)



    if not cookies:
        print("❌ Куки не найдены. Возможно, вы еще не вошли в Kwork.")
        return

    # Сериализуем в base64 для надежной вставки в панель переменных Bothost (без проблем с кавычками)
    json_str = json.dumps(cookies, ensure_ascii=False)
    b64_str = "base64:" + base64.b64encode(json_str.encode("utf-8")).decode("utf-8")

    out_file = Path(__file__).parent / "kwork_cookies_for_bothost.txt"
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(b64_str)

    print(f"\n✅ Найдено {len(cookies)} куков авторизации Kwork!")
    print(f"💾 Строка скопирована в файл: {out_file.name}\n")
    print("-" * 60)
    print("Скопируйте значение ниже и вставьте в Bothost как KWORK_COOKIES:")
    print("-" * 60)
    print(b64_str)
    print("-" * 60)
    print("\nВ панели Bothost добавьте переменную:")
    print("Имя: KWORK_COOKIES")
    print("Значение: (вставьте строку выше)")

if __name__ == "__main__":
    export_kwork_cookies()
