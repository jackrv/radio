# ⌨️ Команди

[← Вікі](README.md)

**Для щоденної роботи команди майже не потрібні:** служби, перенесення й оновлення Pi — у панелі («Система» → «Пристрої»).
Усе нижче — на **Mac** у `~/Documents/Projects/radio`, якщо не сказано інше.

## Найчастіші

| Команда | Що робить |
| --- | --- |
| `./r345 up` | уся система; якщо дім на Pi — лише Whisper і панель |
| `Ctrl+C` або `./r345 stop` | зупинити все |
| `./r345 status` | що працює |
| `./r345 restart panel` | перезапустити одну службу (або `asr`, `node`, `api`, `tunnel`, `worker`) |
| `.venv/bin/python -m server.api.local_dashboard --open` | відкрити панель |

`./r345 up` без параметрів читає з `.env` `RADIO345_PROFILE` і `RADIO345_ONLY` (що запускати; `tunnel` відкриває Mini App назовні).

## Перевірка й обслуговування

| Команда | Що робить |
| --- | --- |
| `./r345 doctor` | усе одним поглядом: служби, база, Pi, підпис iPhone, копії |
| `./r345 pause-model` / `resume-model` | віддати модель лабораторії / повернути |
| `tools/ios_reinstall.sh --force` | поставити застосунок на iPhone зараз |
| `.venv/bin/python -m tools.manage_database export` | словники у файл; `restore --from …` — відновити базу |

Лабораторія — [lab.md](lab.md).

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
