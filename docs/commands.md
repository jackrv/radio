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

## iPhone

| Команда | Що робить |
| --- | --- |
| `tools/ios_reinstall.sh --status` | строк підпису й агент |
| `tools/ios_reinstall.sh --force` | поставити застосунок зараз |

## Обслуговування

| Команда | Що робить |
| --- | --- |
| `.venv/bin/python -m tools.diagnose` | коротка перевірка бази й служби (`--network` — ще й мережа) |
| `.venv/bin/python -m tools.pull_backup --status` | копії баз Pi; вмикаються в панелі («Пристрої» → Pi) |
| `.venv/bin/python -m tools.manage_database export` | словники у файл; `restore --from …` — відновити базу |
| `./r345 pause-model` / `resume-model` | віддати модель лабораторії / повернути |

Лабораторія — [lab.md](lab.md).

## На Pi (`ssh <користувач>@radio345`, каталог `~/radio345-hub`)

| Команда | Коли |
| --- | --- |
| `systemctl is-active radio345-panel radio345-asr radio345-api radio345-tunnel radio345-node` | що працює |
| `journalctl -u radio345-asr -f` | журнал (Ctrl+C — вийти) |
| `sudo sh deploy/hub/install.sh --units` | лише якщо панель просить після оновлення коду |

## Що лежить поза проєктом

Лише для фонових агентів: macOS не пускає їх у `~/Documents`. Усе — в `~/Library/Application Support/radio345`.

| Що | Місце | Прибрати |
| --- | --- | --- |
| копії баз Pi (до 3, близько 100 МБ кожна) | `pi-backups/` | `.venv/bin/python -m tools.pull_backup --remove-agent`, потім видалити теку |
| знімок застосунку для підпису (до ~80 МБ зі збіркою) | `ios/` | `tools/ios_reinstall.sh --remove-agent`, потім видалити теку |

Повністю прибрати сліди: обидві команди, потім `rm -r ~/Library/Application\ Support/radio345`. Код і дані системи від цього не залежать.
