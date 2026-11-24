# ⚙️ Запуск і служби

[← Вікі](README.md)

## Mac: `r345`

Профіль — `--profile` або `RADIO345_PROFILE` у `.env`: `mac-worker` (Whisper для Pi), `mac-dev` (усе на Mac, без бота), `pi`.

| Команда | Дія |
| --- | --- |
| `./r345 up` | запустити процеси профілю; `--only worker node` — лише вибрані |
| `./r345 status` / `stop` / `restart [процес …]` | стан / зупинити / перезапустити |
| `./r345 check` | що запуститься й чого бракує |
| `./r345 pause-model [--minutes 120]` / `resume-model` | віддати модель лабораторії / повернути |

`Ctrl+C` зупиняє все; падіння одного процесу не зупиняє інші. `.env` перечитується після `stop` і `up`.
API з публічним тунелем — лише явно: `./r345 up --only api tunnel`.

## Pi: systemd

Служби `radio345-node`, `-asr` (з ботом), `-panel`, `-api`, `-tunnel`: `sudo systemctl restart radio345-asr`,
`journalctl -u radio345-asr -f`. Встановлення й оновлення — [Pi](raspberry-pi.md).

## Замки

Файли `.radio345.lock`, `.radio345-owner.lock`, `.radio345-worker.lock`, `.asr-lab.lock` не видаляйте: система знімає їх сама.
