from unittest.mock import Mock

import services.dictionary_loader as module


def test_load_dictionaries(monkeypatch):
    data = {
        "callsigns": ["Альфа", "Бета"],
        "commands": ["Старт"],
        "telegram": ["Бета", "Контроль"],
    }

    init_db = Mock()
    monkeypatch.setattr(module, "init_db", init_db)

    get_lines = Mock(side_effect=lambda kind, db_path: data[kind])
    monkeypatch.setattr(module, "get_dictionary_lines", get_lines)

    log = Mock()

    callsigns, commands, watch, prompt = module.load_dictionaries(
        "test.db", log
    )

    init_db.assert_called_once_with("test.db")

    assert [call.args[0] for call in get_lines.call_args_list] == [
        "callsigns",
        "commands",
        "telegram",
    ]

    assert all(
        call.args[1] == "test.db"
        for call in get_lines.call_args_list
    )

    expected_prompt = ", ".join(
        sorted(set(callsigns.names) | set(commands.names) | set(watch.names))
    )

    assert prompt == expected_prompt
    assert len(prompt.split(", ")) == len(set(prompt.split(", ")))

    log.info.assert_called_once_with(
        "Словари загружены из SQLite: позывные=%d, команды=%d, Telegram=%d",
        len(callsigns.names),
        len(commands.names),
        len(watch.names),
    )


def test_empty_dictionaries(monkeypatch):
    monkeypatch.setattr(module, "init_db", Mock())
    monkeypatch.setattr(
        module,
        "get_dictionary_lines",
        Mock(return_value=[]),
    )

    log = Mock()

    try:
        module.load_dictionaries("test.db", log)
    except RuntimeError as error:
        assert "Словарь позывных пуст" in str(error)
    else:
        raise AssertionError(
            "Пустой словарь позывных должен останавливать запуск"
        )

    log.error.assert_called_once()
