# ⌨️ Команди

[← Вікі](README.md)

**Для щоденної роботи команди майже не потрібні:** служби й звук Pi — в її панелі («Система» → «Пристрої»).
Усе нижче — на **Mac** у `~/Documents/Projects/radio`, якщо не сказано інше.

Підказка в терміналі: `./r345` без параметрів.

## Найчастіші

| Команда | Що робить |
| --- | --- |
| `./r345 up` | уся система; якщо дім на Pi — лише Whisper |
| `Ctrl+C` або `./r345 stop` | зупинити все |
| `./r345 status` | що працює |
| `./r345 link` | посилання з ключем панелі дому (Pi чи Mac) — у буфер обміну; ключ не друкується |
| `./r345 restart panel` | перезапустити одну службу (або `asr`, `node`, `api`, `tunnel`, `worker`) |
| `.venv/bin/python -m server.api.local_dashboard --open` | відкрити панель |

`./r345 up` без параметрів читає з `.env` `RADIO345_PROFILE` і `RADIO345_ONLY` (що запускати; `tunnel` відкриває Mini App назовні).

## Де працює система

| Команда | Що робить |
| --- | --- |
| `./r345 home` | показати, де дім |
| `./r345 home pi` / `mac` | перенести дім (із запитанням); на тому самому домі — увімкнути потрібне й зупинити зайве |
| `./r345 home off` | зупинити ефір, бота, API, тунель і Whisper на обох |
| `./r345 home rollback` | повернути після перерваного перенесення |

Коли дім на Pi, панель Mac не потрібна й не запускається; запасна — `./r345 restart panel`. `home mac` вмикає її сам.

## Перевірка й обслуговування

| Команда | Що робить |
| --- | --- |
| `./r345 update` | Mac — з GitHub, потім служба Whisper і Pi — напряму з цієї папки; `update mac` (папка + служба) / `update pi` — окремо. Для служби й Pi потрібен лише commit, без push. Те саме в панелі |
| `./r345 doctor` | усе одним поглядом: служби, база, Pi, підпис iPhone, копії |
| `./r345 service on` / `off` / `status` | на Mac: служба замість вікна термінала; проєкт має лежати не в `~/Documents` |
| `./r345 pause-model` / `resume-model` | віддати модель лабораторії / повернути |
| `tools/ios_reinstall.sh --force` | поставити застосунок на iPhone зараз |
| `.venv/bin/python -m tools.manage_database export` | словники у файл; `restore --from …` — відновити базу |

Лабораторія — [lab.md](lab.md).

## Служба Whisper на Mac (`~/radio345-mac`)

Окрема папка з git (профіль `mac-worker`: лише файли вузла Whisper, свій `.venv`, `.env` = `RADIO345_PROFILE=mac-worker`).
launchd не бачить `~/Documents`, тому служба живе тут, а розробка — у `~/Documents/Projects/radio`.

| Де | Команда | Що робить |
| --- | --- | --- |
| `~/radio345-mac` | `./r345 service on` / `off` / `status` | Whisper службою: сам після входу й після падіння |
| розробка | `./r345 update` / `update mac` | служба бере закомічений `main` папки розробки (як Pi, без push), перевіряє імпорт своїм Python і перезапускає Whisper |
| `~/radio345-mac` | `./r345 update mac` | запасний шлях: код служби з GitHub (потрібен push) |
| `~/radio345-mac` | `./r345 status` | стан вузла |
| розробка | `./r345 pause-model` / `resume-model`, лабораторія | діють на службу: `RADIO_ASR_LOCK_ROOT=~/radio345-mac` у `.env` розробки — спільні замки моделі |

У папці розробки `worker` не запускайте: одна модель на Mac.

## На Pi (`ssh <користувач>@radio345`, каталог `~/radio345-hub`)

| Команда | Коли |
| --- | --- |
| `./r345 status` або `./r345 doctor` | що працює |
| `journalctl -u radio345-asr -f` | журнал (Ctrl+C — вийти) |
| `sudo sh deploy/hub/install.sh --units` | лише якщо панель просить після оновлення коду |

## Що лежить поза проєктом

Лише знімок застосунку iPhone для продовження підпису: `~/Library/Application Support/radio345/ios` (до ~80 МБ; macOS не пускає фонові
процеси в `~/Documents`). Прибрати: `tools/ios_reinstall.sh --remove-agent`, потім `rm -r ~/Library/Application\ Support/radio345`.

Копії баз Pi панель знімає сама раз на добу, коли дім на Pi: `.local-data/backups/pi`, до 3.
