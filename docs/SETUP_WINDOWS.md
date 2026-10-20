# Автомонтаж: запуск з нуля на Windows

Покроково, від порожнього ноутбука до роботи з iPhone. Йди по порядку. Кроки 1–6 обовʼязкові, далі — що потрібно.

Що знадобиться: Windows 10/11, відеокарта NVIDIA, інтернет, ~1 година, 10 ГБ вільного місця (плюс місце під відео).

Усі команди вводяться в **PowerShell**: Пуск → набери `PowerShell` → Enter.

---

## 1. Встановити програми (≈15 хв)

**1.1. Драйвер NVIDIA** — свіжий, щоб працювало апаратне кодування відео (NVENC).
Найпростіше через NVIDIA App: https://www.nvidia.com/en-us/software/nvidia-app/ → «Драйвери» → оновити.
(Або вручну: https://www.nvidia.com/Download/index.aspx)

**1.2. Git, Python 3.12 і FFmpeg** — одним заходом у PowerShell:

```powershell
winget install -e --id Git.Git
winget install -e --id Python.Python.3.12
winget install -e --id Gyan.FFmpeg
```

На кожне питання відповідай `Y`. Потім **закрий PowerShell і відкрий знову** — інакше нові програми не знайдуться.

Перевір:

```powershell
git --version
py -3.12 --version
ffmpeg -version
```

Кожна команда має вивести номер версії. Якщо `winget` не знайдено — постав вручну:
Git https://git-scm.com/download/win · Python 3.12 https://www.python.org/downloads/windows/ (у встановлювачі постав галочку **Add python.exe to PATH**) · FFmpeg https://www.gyan.dev/ffmpeg/builds/ (архів *release full*, розпакуй у `C:\ffmpeg` і додай `C:\ffmpeg\bin` у змінну PATH).

> Саме Python **3.12**: на новіших версіях деякі бібліотеки (MediaPipe) можуть не встановитись.

---

## 2. Завантажити програму (≈1 хв)

Папка — `C:\Montag`. Не клади на Робочий стіл чи в OneDrive: синхронізація заважає великим відео.

```powershell
cd C:\
git clone https://github.com/Envyyy-uk/Montag.git
cd C:\Montag
```

Репозиторій: https://github.com/Envyyy-uk/Montag

---

## 3. Встановити бібліотеки (≈5–10 хв)

```powershell
cd C:\Montag
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e .
```

Наприкінці має бути `Successfully installed ...` без червоних помилок.

> Далі всі команди програми починаються з `.\.venv\Scripts\python` і виконуються з папки `C:\Montag`.

---

## 4. Файл налаштувань `.env` (≈2 хв)

```powershell
copy .env.example .env
notepad .env
```

Поки що нічого не міняй — значень за замовчуванням досить для першого запуску. Збережи й закрий.
Цей файл ніколи не потрапляє в git: туди підуть ключі й PIN.

---

## 5. Перевірка системи

```powershell
.\.venv\Scripts\python -m core check
.\.venv\Scripts\python -m core init
```

`check` покаже FFmpeg, NVENC (кодування на відеокарті) і CUDA. Якщо NVENC «ні» — онови драйвер (крок 1.1); програма все одно працюватиме, лише повільніше.
`init` створить папки `library\`, `inbox\` і базу.

---

## 6. Перший запуск

Подвійний клік по **`C:\Montag\run-windows.bat`** (або в PowerShell: `.\.venv\Scripts\python -m server.app`).

Відкриється чорне вікно з журналом — **не закривай його**, це і є програма. Якщо Windows спитає про брандмауер — **Дозволити** (лише приватні мережі).

Відкрий у браузері: **http://127.0.0.1:8765**

Спробуй: «+» → назва проєкту → вибери 2–3 відео і трек (mp3) → дочекайся завантаження → **● Монтувати**.
Перший раз програма докачає моделі (~500 МБ: розпізнавання мови й облич), тож перший монтаж довший.

Без ключа Gemini кадри оцінюються локально й безкоштовно. Уже можна користуватись на ноутбуці.

---

## 7. Gemini — розумна оцінка кадрів (≈5 хв, за бажанням)

1. Відкрий https://aistudio.google.com/apikey → **Create API key** → скопіюй ключ.
2. Щоб не впертися в безкоштовні ліміти — там же **Set up billing** (привʼязати картку). Програма сама не витратить більше `MONTHLY_BUDGET_USD` ($10 за замовчуванням) і питає підтвердження на суму від $1.
3. У `.env`: `GEMINI_API_KEY=твій_ключ`
4. Перевір:

```powershell
.\.venv\Scripts\python -m core gemini-ping
```

Має бути «Відповідь: 'ок'» і вартість у тисячних цента. Перезапусти програму (закрий вікно → знову `run-windows.bat`).

Ціни моделей: https://ai.google.dev/gemini-api/docs/pricing

---

## 8. Доступ з iPhone (≈15 хв)

**8.1. Tailscale** — приватна мережа між ноутбуком і телефоном (безкоштовно):
- ноутбук: https://tailscale.com/download/windows → встанови, увійди (Google/Apple/GitHub);
- iPhone: https://apps.apple.com/app/tailscale/id1470499037 → увійди **тим самим акаунтом** → увімкни VPN.

**8.2. В адмінці Tailscale** https://login.tailscale.com/admin/dns увімкни:
- **MagicDNS**,
- **HTTPS Certificates** (кнопка Enable HTTPS).

**8.3. Сертифікат HTTPS** (на ноутбуці):

```powershell
.\.venv\Scripts\python -m core tailscale-cert
```

Покаже адресу на кшталт `https://msi-vector.tail1234.ts.net:8443/`. Якщо помилка про доступ — запусти PowerShell **від імені адміністратора** і повтори.
Сертифікат діє 90 днів — повторюй цю команду раз на 2 місяці.

**8.4. PIN** — у `.env`: `ACCESS_PIN=` і 6+ цифр (наприклад `ACCESS_PIN=482913`). Перезапусти програму.

**8.5. На iPhone** у Safari відкрий адресу з кроку 8.3 → введи PIN → «Поділитися» → **«На початковий екран»**.
Тепер запускай з іконки. На екрані «Ліміт» натисни **«увімкнути»** у «Сповіщеннях» — прийде push, коли монтаж готовий.

---

## 9. Google Drive — відео поза домом (≈15 хв, за бажанням)

Коли телефон далеко від дому, відео йдуть через Drive: ноутбук сам забирає їх, перевіряє і видаляє з Drive, а готовий кліп кладе в «Готово».

1. Увімкни Drive API (той самий Google-проєкт, що й ключ Gemini): https://console.cloud.google.com/apis/library/drive.googleapis.com → **Enable**.
2. https://console.cloud.google.com/auth/overview → **Get started**: назва «Автомонтаж», твій email, тип **External** → Create.
3. https://console.cloud.google.com/auth/audience → **Publish app** (статус *In production*; інакше вхід злітає кожні 7 днів). Перевірка Google не потрібна — при вході буде попередження «неперевірений застосунок», натисни «Додатково → Перейти».
4. https://console.cloud.google.com/auth/clients → **Create client** → тип **Desktop app** → Create → **Download JSON**.
5. Збережи файл як `C:\Montag\client_secret.json`.
6. Увійди:

```powershell
.\.venv\Scripts\python -m sources.gdrive auth
```

Відкриється браузер → дозволь доступ. Перезапусти програму. У Drive зʼявиться папка **«Автомонтаж»** з папками проєктів.

---

## 9½. Бібліотека музики (за бажанням)

Щоб AI підбирав трек під відео, склади свою музику в одне місце:
- на ноутбуці — у папку `C:\Montag\music` (можна з підпапками);
- або з телефона — у Google Drive: «Автомонтаж» → «Музика» (папка з'явиться сама після кроку 9).

Формати: mp3, m4a, wav, flac. Програма раз на хвилину знаходить нові треки й аналізує їх (темп, енергія, дропи).
На екрані проєкту → «Музика» → **🎵 Підібрати**: 3 треки з твоєї бібліотеки з поясненням і найкращим шматком,
▶ — прослухати, «Взяти» — поставити в кліп. Плюс ідеї нових пісень з посиланнями на SoundCloud.
Файли в `music` програма ніколи не змінює й не видаляє.

## 10. Автозапуск і сон (≈2 хв)

Щоб програма стартувала сама після входу у Windows:
1. Win+R → `shell:startup` → Enter (відкриється папка автозапуску).
2. Правою кнопкою по `C:\Montag\run-windows.bat` → «Показати додаткові параметри» → **«Створити ярлик»**, перетягни ярлик у папку автозапуску.
3. Властивості ярлика → «Вікно»: **Згорнуте**.

Поки в черзі є робота, Windows не засинає сам. Кришку ноутбука при цьому не закривай (або в «Електроживлення» → «Дія при закритті кришки» → «Нічого не робити» від мережі).

---

## 11. Claude Desktop на ноутбуці (≈3 хв, за бажанням)

1. Встанови: https://claude.ai/download
2. Claude Desktop → Settings → **Developer** → **Edit Config** → встав:

```json
{
  "mcpServers": {
    "avtomontazh": {
      "command": "C:\\Montag\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_server"]
    }
  }
}
```

3. Перезапусти Claude Desktop. Програма (крок 6) має працювати. Пиши: «що в черзі?», «покажи кліп», «зроби швидше на приспіві».

---

## 12. Claude на iPhone (≈5 хв, за бажанням, після кроку 8)

1. У `.env`: `MCP_REMOTE=true` (PIN уже з 6+ цифр). Перезапусти програму.
2. Дозволь Funnel: https://login.tailscale.com/admin/acls/file → у політику додай (або погодься на посилання, яке покаже наступна команда):

```json
"nodeAttrs": [
  { "target": ["autogroup:member"], "attr": ["funnel"] }
]
```

3. Увімкни:

```powershell
.\.venv\Scripts\python -m core tailscale-funnel on
```

Покаже адресу `https://<ноут>.<tailnet>.ts.net/mcp`.
4. На https://claude.ai (або в застосунку Claude) → Settings → **Connectors** → **Add custom connector** → встав адресу → Connect → на сторінці ноутбука введи PIN → «Дозволити».
5. Готово: у чаті Claude на iPhone пиши про свої проєкти. Підключення видно в застосунку на екрані «Ліміт» (кнопка «Відкликати»). Вимкнути все: `.\.venv\Scripts\python -m core tailscale-funnel off`.

---

## Оновлення програми

Закрий вікно програми, потім:

```powershell
cd C:\Montag
git pull
.\.venv\Scripts\python -m pip install -e .
```

і знову `run-windows.bat`. Твої відео, база й `.env` не зачіпаються.

---

## Якщо щось не так

| Що бачиш | Що робити |
|---|---|
| `py` / `git` / `ffmpeg` не знайдено | закрий і відкрий PowerShell; якщо не допомогло — постав вручну за посиланнями з кроку 1.2 |
| помилка під час `pip install` | перевір, що це Python 3.12 (`py -3.12 --version`), видали папку `.venv` і повтори крок 3 |
| `core check`: NVENC — ні | онови драйвер NVIDIA (крок 1.1) |
| http://127.0.0.1:8765 не відкривається | чи відкрите чорне вікно програми? Подивись останні рядки в ньому |
| iPhone не відкриває адресу | Tailscale на iPhone увімкнений? Той самий акаунт? Чи робив крок 8.3? |
| «Потрібен PIN» щоразу | відкривай з іконки на початковому екрані, а не з Safari |
| Gemini: «ключ не задано» | ключ у `.env` без лапок і пробілів, програма перезапущена |
| Drive не забирає відео | файли мають лежати в папці проєкту всередині «Автомонтаж»; перевір вхід кроком 9.6 |

Якщо незрозуміло — скопіюй останні 20 рядків із чорного вікна програми й надішли.
