# 🆘 Якщо щось не так

[← Вікі](README.md)

| Симптом | Перевірте |
| --- | --- |
| iPhone не чує ефір | чи працює ефір: на Pi `systemctl is-active radio345-node`, на Mac `./r345 status`; у застосунку вибрано той самий пристрій; Tailscale увімкнено |
| «Наживо» є, текстів немає | Mac: `./r345 status` (вузол Whisper); фраза має пройти відбір `RADIO_STREAM_MODE` (типово `watch`) |
| Фрази не розпізнаються | тиха рація: рівень нижче `MIN_RMS` (655) відкидається; хрип і телефон уголос поруч із мікрофоном псують текст |
| Панель Pi не відкривається | лише `http://` (не https), посилання з ключем: `ssh <Pi> cat radio345-hub/.radio345-local-admin/access-url.txt` |
| Mini App: сервіс недоступний | на Pi `systemctl is-active radio345-api radio345-tunnel`; тунель не має працювати на іншому пристрої |
| Бот мовчить | бот запущено на двох пристроях або `radio345-asr` не активна |
| Застосунок не відкривається | закінчився підпис: `tools/ios_reinstall.sh --status`, потім `--force` |
| Немає копій Pi | `.venv/bin/python -m tools.pull_backup --status` |

Журнал: на Pi `journalctl -u radio345-asr -f`, на Mac — вікно `r345` і `radio.log`.
