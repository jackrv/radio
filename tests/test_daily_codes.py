"""Коди доби: ручне введення, зберігання за датою й підказка «Можливо, 48 62?»."""

import datetime

import pytest
from fastapi.testclient import TestClient

from shared.callsigns import Callsigns, command_dictionary
from shared import daily_codes
from shared.text_check import check_text
from tests.test_text_check import api  # noqa: F401  (фікстура Mini App API)
from tests.test_web_api import auth_header

CALLSIGNS = Callsigns.from_lines(["береза: 56 68"])
COMMANDS = command_dictionary(["злива 4242"])
CODES = ["48 62", "53 19", "31 05"]


def hints(text, codes=CODES):
    return [(i["message"], i["fixed_text"]) for i in check_text(text, CALLSIGNS, COMMANDS, None, codes=codes)["issues"]
            if "код доби" in i["message"]]


def test_parse_codes_accepts_spoken_and_merged_forms():
    assert daily_codes.parse_codes("48 62, 3105; 72-59\n48,62") == ["48 62", "31 05", "72 59"]
    with pytest.raises(ValueError, match="чотирьох цифр"):
        daily_codes.parse_codes("48 6, 31 05")


def test_comma_is_removed_only_between_pairs_of_a_confirmed_code():
    text = "Клен 15, 60, 70 67, прийом"
    assert daily_codes.remove_confirmed_code_commas(text, ["60 70"], (CALLSIGNS,)) == \
        "Клен 15, 60 70 67, прийом"
    assert daily_codes.remove_confirmed_code_commas(text, ["61 70"], (CALLSIGNS,)) == text


def test_text_check_formats_confirmed_codes_but_preserves_callsign_numbers():
    text = "Береза 68, 48, 62, прийом"
    result = check_text(text, CALLSIGNS, COMMANDS, None, codes=CODES)
    assert result["formatted"] == "Береза 68, 48 62, прийом"
    assert any(issue["message"] == "Кома всередині підтвердженого коду доби" for issue in result["issues"])

    callsigns = Callsigns.from_lines(["береза: 48"])
    callsign_number = check_text("Береза 48, 62, прийом", callsigns, COMMANDS, None, codes=["48 62"])
    assert callsign_number["formatted"] is None


def test_hint_for_one_wrong_digit_only():
    assert hints("Береза 68, 48, 42, прийом") == [("Можливо, «48 62» — код доби, а не «48, 42»?", "Береза 68, 48 62, прийом")]
    assert hints("Береза 68, 48 62, прийом") == []
    assert hints("Береза 68, 15 57") == [], "дві цифри інші — не підказуємо"
    assert hints("Береза 68, 63 27") == [], "68 — номер позивного, 63 27 не схоже на коди"
    assert hints("Береза 68, 48, 42", codes=[]) == [], "без кодів доби — без підказок"


def test_daily_code_hint_is_manual_only():
    issues = check_text("Береза 68, 48, 42", CALLSIGNS, COMMANDS, None, codes=CODES)["issues"]
    hint = next(issue for issue in issues if "код доби" in issue["message"])
    assert hint["manual_only"] is True
    assert hint["fixed_text"] == "Береза 68, 48 62"


def test_store_by_day_and_event_day(tmp_path):
    from shared.event_store import init_db
    db_path = tmp_path / "events.db"
    init_db(db_path)
    assert daily_codes.write(db_path, "2026-11-17", "48 62, 31 05") == ["48 62", "31 05"]
    daily_codes.write(db_path, "2026-11-16", "53 19")
    assert [i["day"] for i in daily_codes.recent(db_path)] == ["2026-11-17", "2026-11-16"]
    from shared.sqlite_connection import get_db
    with get_db(db_path) as db:
        db.execute("INSERT INTO radio_events (id, ts, callsigns, commands, recipient, sender, text) "
                   "VALUES (5, ?, '', '', '', '', 'x')", (
                       datetime.datetime(2026, 11, 16, 12, 0, tzinfo=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),))
        assert daily_codes.for_fix_id(db, "db:5") == daily_codes.read(db, daily_codes.day_of_event_ts("2026-11-16 12:00:00"))
    assert daily_codes.write(db_path, "2026-11-16", "") == [], "порожньо — коди дня прибрано"
    assert [i["day"] for i in daily_codes.recent(db_path)] == ["2026-11-17"]
    with pytest.raises(ValueError):
        daily_codes.write(db_path, "17.11.2026", "48 62")


def test_panel_codes_and_hint_by_record_day(tmp_path):
    from api.local_dashboard import create_dashboard
    from tests.test_local_dashboard import KEY, ORIGIN
    from shared.event_store import init_db
    from shared.user_store import init_users_db, bootstrap_admin
    from shared.dictionary_store import init_db as init_dictionary_db
    import sqlite3
    db = tmp_path / "events.db"
    init_db(db); init_dictionary_db(db); init_users_db(db); bootstrap_admin(db, 1001)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO dictionary_entries (dictionary_type,line_number,raw_line) VALUES ('callsigns',1,'береза: 56 68')")
    app = create_dashboard(db, KEY, samples_dir=tmp_path / "samples", recordings_dir=tmp_path / "recordings")
    h = {"Origin": ORIGIN}
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 1)) as client:
        client.post("/session", json={"token": KEY}, headers=h)
        today = client.get("/api/daily-codes").json()["today"]
        assert client.put(f"/api/daily-codes/{today}", json={"codes": "48 62"}, headers=h).json()["codes"] == ["48 62"]
        assert client.put(f"/api/daily-codes/{today}", json={"codes": "1"}, headers=h).status_code == 422
        assert [(i["day"], i["codes"]) for i in client.get("/api/daily-codes").json()["items"]] == [(today, ["48 62"])]
        assert client.put("/api/daily-codes/permanent", json={"codes": "31 05, 7259"}, headers=h).json()["codes"] == ["31 05", "72 59"]
        listing = client.get("/api/daily-codes").json()
        assert listing["permanent"] == ["31 05", "72 59"] and [i["day"] for i in listing["items"]] == [today]
        check = lambda **body: [i["message"] for i in client.post("/api/text-check", json=body, headers=h).json()["issues"]]
        now = datetime.datetime.now().timestamp()
        assert any("48 62" in m for m in check(text="Береза 68, 48, 42", at=now))
        assert not any("48 62" in m for m in check(text="Береза 68, 48, 42", at=now - 3 * 24 * 3600)), "інший день — свої коди"
        assert any("31 05" in m for m in check(text="Береза 68, 31 08", at=now - 3 * 24 * 3600)), "постійні — щодня"


def test_miniapp_check_uses_codes_of_the_event_day(api):  # noqa: F811
    db_path, client = api
    from shared.sqlite_connection import get_db
    daily_codes.write(db_path, daily_codes.local_day(), "48 62")
    body = client.put("/api/corrections/check", headers=auth_header(3003), json={"text": "Береза 68, 48, 42"}).json()
    assert any("48 62" in i["message"] for i in body["issues"]), "без fix_id — коди сьогодні"
    with get_db(db_path) as db:
        db.execute("INSERT INTO radio_events (id, ts, callsigns, commands, recipient, sender, text) "
                   "VALUES (9, '2026-01-01 10:00:00', '', '', '', '', 'x')")
    body = client.put("/api/corrections/check", headers=auth_header(3003),
                      json={"text": "Береза 68, 48, 42", "fix_id": "db:9"}).json()
    assert not any("код доби" in i["message"] for i in body["issues"]), "подія іншого дня — без кодів сьогодні"


def test_code_day_changes_at_configured_hour(monkeypatch):
    import config
    monkeypatch.setattr(config, "DAILY_CODES_CHANGE_HOUR", 20, raising=False)
    at = lambda h, m=0, d=16: datetime.datetime(2026, 11, d, h, m)
    assert daily_codes.local_day(at(19, 59)) == "2026-11-16"
    assert daily_codes.local_day(at(20, 0)) == "2026-11-17", "о 20:00 — уже коди наступної доби"
    assert daily_codes.local_day(at(23, 5)) == "2026-11-17"
    assert daily_codes.local_day(at(7, 1, d=17)) == "2026-11-17"
    assert daily_codes.period("2026-11-17") == ("16.11 20:00", "17.11 20:00")


def test_permanent_codes_join_every_day(tmp_path):
    from shared.event_store import init_db
    from shared.sqlite_connection import get_db
    db_path = tmp_path / "events.db"
    init_db(db_path)
    daily_codes.write_permanent(db_path, "31 05, 72 59")
    daily_codes.write(db_path, "2026-11-17", "48 62, 31 05")
    with get_db(db_path) as db:
        assert daily_codes.read(db, "2026-11-17") == ["31 05", "72 59", "48 62"], "без повторів"
        assert daily_codes.read(db, "2026-11-01") == ["31 05", "72 59"]
    assert [i["day"] for i in daily_codes.recent(db_path)] == ["2026-11-17"], "постійні — не доба"
