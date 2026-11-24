# ⚙️ Запуск і служби

[← Вікі](README.md)

## Mac: `r345`

Профіль — `--profile` або `RADIO345_PROFILE` у `.env`: `mac-worker` (Whisper для Pi), `mac-dev` (усе на Mac, без бота),
`mac-all` (усе на Mac з ботом і Mini App, замість Pi), `pi`. Перед `mac-all` на Pi: `sudo sh deploy/hub/install.sh --no-miniapp` і
`sudo systemctl disable --now radio345-asr`.

Набори `mac-all` для `RADIO345_ONLY` (замість переліку служб; перелік теж працює):

| Набір | Що запускає Mac |
| --- | --- |
| `home` | усе: Whisper, ефір, бот, API, панель |
| `home-miniapp` | те саме й публічна адреса Mini App |
| `whisper` | Whisper і панель (дім на Pi) |
| `panel` | лише панель |

Коли дім на Pi, `up` без параметрів однаково запускає лише Whisper; панель — на Pi.

| Команда | Дія |
| --- | --- |
| `./r345 up` | `RADIO345_PROFILE` і `RADIO345_ONLY` з `.env`; `--profile`, `--only worker node` — явно |
| `./r345 status` / `stop` / `restart [процес …]` | стан / зупинити / перезапустити |
| `./r345 home [pi \| mac \| off \| rollback]` | де працює система: показати, перенести, вимкнути ([команди](commands.md)) |
| `./r345 check` | що запуститься й чого бракує |
| `./r345 pause-model [--minutes 120]` / `resume-model` | віддати модель лабораторії / повернути |

`Ctrl+C` зупиняє все; падіння одного процесу не зупиняє інші. `.env` перечитується після `stop` і `up`.
API з публічним тунелем — лише явно: `./r345 up --only api tunnel`.

## Pi: systemd

Служби `radio345-node`, `-asr` (з ботом), `-panel`, `-api`, `-tunnel`: `sudo systemctl restart radio345-asr`,
`journalctl -u radio345-asr -f`. Встановлення й оновлення — [Pi](raspberry-pi.md).

## Замки

Файли `.radio345.lock`, `.radio345-owner.lock`, `.radio345-worker.lock`, `.asr-lab.lock` не видаляйте: система знімає їх сама.
