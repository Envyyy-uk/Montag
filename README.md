# Автомонтаж (auto-editor)

Особиста програма, яка з кількох сирих відео робить майже готовий монтаж: кліп під музику і 3 вертикальні ролики.
Працює локально на ноутбуці (Windows 11, NVIDIA RTX). Платний лише AI-аналіз кадрів (Gemini Flash) у межах місячного ліміту.

## Встановлення (Windows)

1. Python 3.12+, драйвер NVIDIA, FFmpeg з NVENC у `PATH` (наприклад, збірка gyan.dev «full»).
2. У папці репозиторію:
   ```powershell
   py -3.12 -m venv .venv
   .venv\Scripts\activate
   pip install -e ".[dev]"
   copy .env.example .env    # і заповни ключі
   ```
3. Перевірка системи і створення бази:
   ```powershell
   python -m core check
   python -m core init
   ```
4. Сервер: `python -m server.app` → http://127.0.0.1:8765/api/health
5. Тести: `pytest`

## Структура

```
core/      конфіг, SQLite, перевірка системи
server/    FastAPI, API, авторизація, завантаження
worker/    черга, оркестрація етапів
analysis/  сцени, фільтр, звук, whisper, gemini, кадрування
edit/      вибір кадрів, ритм, вставки, переходи, вертикаль
render/    ffmpeg-фільтри, стилі, NVENC, експорт FCPXML
sources/   прямий upload, Google Drive, локальний inbox
styles/    JSON-пресети стилів і переходів
web/       React PWA, Liquid Glass UI
tests/     юніт-тести
library/   дані користувача (не в git)
```

## Безпека

- Секрети лише в `.env` (не в git).
- Сервер слухає тільки на `127.0.0.1` або Tailscale-адресі; `0.0.0.0` заборонено конфігом.
- Оригінали відео ніколи не змінюються і не видаляються.
