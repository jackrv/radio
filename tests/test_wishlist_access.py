"""Права, персональні режими та зміст лічильників; лише тимчасові SQLite."""
import sqlite3

import pytest

from services.event_store import save_event
from services.miniapp_correction_editor import save_edit_draft
from services.miniapp_management import ManagementConflict, list_users, write_user, overview
from services.user_notify_store import load_user_mode, save_user_mode
from tests.test_miniapp_management import setup
from tests.test_web_api import auth_header


@pytest.mark.parametrize("route", ["phrases","receiver/control","notifications/mode"])
def test_viewer_has_only_minimal_status(setup,route):
    client,_=setup
    assert client.get("/api/"+route,headers=auth_header(2002)).status_code==403
    assert client.get("/api/status",headers=auth_header(2002)).json()=={"api":"ready","receiver":"offline"}
    assert client.get("/api/me",headers=auth_header(2002)).json()["role"]=="viewer"


def test_viewer_cannot_download_audio_or_write_preferences(setup):
    client,_=setup
    assert client.get("/api/phrases/audio?fix_id=db:1",headers=auth_header(2002)).status_code==403
    assert client.put("/api/notifications/mode",headers=auth_header(2002),json={"mode":"off"}).status_code==403


def test_admin_assigns_mode_atomically_and_conflicts_include_mode(setup):
    client,path=setup
    users=list_users(path,1001)["items"]
    row=next(x for x in users if x["user_id"]=="3003")
    assert row["notify_mode"]=="commands"
    save_user_mode(path,3003,"off")
    with pytest.raises(ManagementConflict):
        write_user(path,1001,3003,"editor",True,row["version"],notify_mode="watch")
    row=next(x for x in list_users(path,1001)["items"] if x["user_id"]=="3003")
    write_user(path,1001,3003,"viewer",True,row["version"],notify_mode="watch")
    assert load_user_mode(path,3003)==("viewer","watch")
    row=next(x for x in list_users(path,1001)["items"] if x["user_id"]=="3003")
    with pytest.raises(ValueError):
        write_user(path,1001,3003,"editor",True,row["version"],notify_mode="all")
    assert load_user_mode(path,3003)==("viewer","watch")


def test_defaults_and_existing_silence_survive_role_edits(setup):
    _,path=setup
    write_user(path,1001,4004,"editor",True,None)
    assert load_user_mode(path,4004)==("editor","commands")
    save_user_mode(path,4004,"off")
    row=next(x for x in list_users(path,1001)["items"] if x["user_id"]=="4004")
    write_user(path,1001,4004,"admin",True,row["version"])
    assert load_user_mode(path,4004)==("admin","off")


def test_stats_count_phrases_even_when_no_word_rule_can_be_learned(setup):
    _,path=setup
    eid=save_event("","","","","Прийом",path)
    save_edit_draft(path,f"db:{eid}","Прийом","Прийом!")
    save_edit_draft(path,f"db:{eid}","Прийом!","Прийом?")
    data=overview(path)
    assert data["corrections"]==1
    assert data["active_rules"]==0


def test_correction_permission_rechecked_inside_transaction(setup):
    _,path=setup
    eid=save_event("","","","","Вира",path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE telegram_users SET role='viewer' WHERE user_id=3003")
    with pytest.raises(PermissionError):
        save_edit_draft(path,f"db:{eid}","Вира","Віра",actor_id=3003)
    assert overview(path)["corrections"]==0


def test_noop_save_is_not_counted_as_correction(setup):
    _,path=setup
    eid=save_event("","","","","Прийом",path)
    save_edit_draft(path,f"db:{eid}","Прийом","Прийом")
    assert overview(path)["corrections"]==0
