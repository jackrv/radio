from callsigns import Callsigns
from core.analyzer import correction_is_safe


def test_llm_cannot_invent_command():
    commands = Callsigns.from_lines(["штиль"])

    assert not correction_is_safe(
        "Шо і здесяду вісім",
        "Штиль і здесяду вісім",
        (commands,),
    )


def test_llm_cannot_invent_callsign():
    callsigns = Callsigns.from_lines([
        "ліра: 68",
        "липка: 700",
    ])

    assert not correction_is_safe(
        "ЛЁР 68",
        "Липка 68",
        (callsigns,),
    )


def test_known_callsign_can_be_normalized():
    callsigns = Callsigns.from_lines(["ліра: 68"])

    assert correction_is_safe(
        "лира 68",
        "Ліра 68",
        (callsigns,),
    )
