import logging
import sqlite3
from unittest.mock import Mock, patch

import pytest
import requests

from services.event_store import save_event
from services.miniapp_correction_editor import save_edit_draft, load_edit_draft
from services.telegram import TelegramService
from services.telegram_media_store import deletion_intent, deletion_result
from services.telegram_phrase_store import register_phrase
from services.telegram_sync import remember_delivery, sync_corrections, clipped
from tests.test_miniapp_management import setup


def response(result=None,status=200):
    value=Mock(status_code=status)
    value.json.return_value=result or {"ok":True}
    return value


def service(path):
    result=TelegramService("test",16000,path,logging.getLogger("test"))
    result._request=Mock(return_value=response())
    return result


def message(chat,mid=1): return {"message_id":mid,"chat":{"id":chat},"voice":{"file_id":"fake"}}


@pytest.mark.parametrize("temporary",[False,True])
def test_corrected_phrase_updates_every_copy_after_restart_without_extra_votes(setup,temporary):
    _,path=setup
    if temporary:
        register_phrase(path,"a"*32,"Вира 68")
        fix="mem:"+"a"*32
    else:
        fix=f'db:{save_event("","","","","Вира 68",path)}'
    for chat in [1001,2002,3003]:remember_delivery(path,fix,message(chat),"voice" if chat!=2002 else "text")
    save_edit_draft(path,fix,"Вира 68","Віра 68")
    save_edit_draft(path,fix,"Віра 68","Віра 68!")
    bot=service(path)
    assert sync_corrections(bot)==3
    assert bot._request.call_count==3
    for call in bot._request.call_args_list:
        payload=call.kwargs["data"]
        assert payload.get("caption",payload.get("text"))=="✏️ Виправлено оператором\nПодія: Ефір\nКому: віра 68 | Від: -\n\nВіра 68!"
    assert sync_corrections(bot)==0
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT MAX(confirmations) FROM word_corrections").fetchone()[0]==1


def test_edit_during_http_cannot_ack_a_newer_revision(setup):
    _,path=setup
    fix=f'db:{save_event("","","","","Вира",path)}'
    remember_delivery(path,fix,message(1001),"voice")
    save_edit_draft(path,fix,"Вира","Віра")
    bot=service(path)
    def concurrent(*args,**kwargs):
        save_edit_draft(path,fix,"Віра","Віра!")
        return response()
    bot._request.side_effect=concurrent
    sync_corrections(bot)
    bot._request.side_effect=None
    assert sync_corrections(bot)==1
    assert bot._request.call_args.kwargs["data"]["caption"].endswith("Віра!")


def test_late_delivery_still_gets_current_text(setup):
    _,path=setup
    fix=f'db:{save_event("","","","","Вира",path)}'
    save_edit_draft(path,fix,"Вира","Віра")
    remember_delivery(path,fix,message(1001),"voice")
    assert sync_corrections(service(path))==1


def test_failed_job_insert_rolls_back_text_and_learning(setup):
    _,path=setup
    fix=f'db:{save_event("","","","","Вира",path)}'
    with patch("services.miniapp_correction_editor.queue_correction",side_effect=sqlite3.OperationalError("test")):
        with pytest.raises(sqlite3.Error):save_edit_draft(path,fix,"Вира","Віра")
    assert load_edit_draft(path,fix)["current_text"]=="Вира"
    with sqlite3.connect(path) as db:assert db.execute("SELECT COUNT(*) FROM word_correction_votes").fetchone()[0]==0


@pytest.mark.parametrize("reason",["deleted","disabled","expired"])
def test_removed_or_expired_phrases_and_revoked_access_not_sent(setup,reason):
    _,path=setup
    register_phrase(path,"a"*32,"Вира")
    fix="mem:"+"a"*32
    remember_delivery(path,fix,message(3003),"voice")
    save_edit_draft(path,fix,"Вира","Віра")
    if reason=="deleted":
        deletion_intent(path,fix);deletion_result(path,fix,True)
    with sqlite3.connect(path) as db:
        if reason=="disabled":db.execute("UPDATE telegram_users SET enabled=0 WHERE user_id=3003")
        if reason=="expired":db.execute("UPDATE telegram_recent_phrases SET created_at=0")
    bot=service(path);sync_corrections(bot)
    bot._request.assert_not_called()


def test_timeouts_back_off_and_survive_restart(setup):
    _,path=setup
    fix=f'db:{save_event("","","","","Вира",path)}'
    remember_delivery(path,fix,message(1001),"voice")
    save_edit_draft(path,fix,"Вира","Віра")
    bot=service(path);bot._request.side_effect=requests.ReadTimeout()
    sync_corrections(bot,now=100)
    assert sync_corrections(service(path),now=110)==0
    assert sync_corrections(service(path),now=131)==1


def test_utf16_caption_limit():
    value=clipped("😀"*1024,1024)
    assert len(value.encode("utf-16-le"))<=2048
    assert value.endswith("…")


def test_status_card_reused_after_restart_and_removed_after_expiry(setup):
    _,path=setup
    bot=service(path)
    bot._request.return_value=response({"ok":True,"result":message(1001,22)})
    assert bot.send_status("Запит надіслано",chat_id=1001)
    assert bot._request.call_args.args[1]=="sendMessage"
    bot=service(path)
    assert bot.send_status("Доступ надано",chat_id=1001)
    assert bot._request.call_args.args[1]=="editMessageText"
    assert bot._request.call_args.kwargs["data"]["message_id"]==22
    with sqlite3.connect(path) as db:db.execute("UPDATE telegram_service_messages SET expires_at=0")
    bot.maintenance_once()
    assert bot._request.call_args.args[1]=="deleteMessage"
    with sqlite3.connect(path) as db:assert db.execute("SELECT COUNT(*) FROM telegram_service_messages").fetchone()[0]==0


def test_unknown_text_and_foreign_chat_never_deleted(setup):
    _,path=setup
    bot=service(path)
    for text,chat in [("звичайний текст",1001),("/all",9999)]:
        bot.handle_update({"message":{"message_id":1,"text":text,"from":{"id":1001},"chat":{"id":chat,"type":"private"}}})
    bot._request.assert_not_called()


def test_recognized_private_command_deleted_without_affecting_mode(setup):
    _,path=setup
    bot=service(path);bot.send_text=Mock(return_value=True)
    bot.handle_update({"message":{"message_id":1,"text":"/off","from":{"id":1001},"chat":{"id":1001,"type":"private"}}})
    assert bot._request.call_args.args[1]=="deleteMessage"
    from services.user_notify_store import load_user_mode
    assert load_user_mode(path,1001)==("admin","off")


def test_temporary_role_lookup_failure_preserves_pending_edit(setup):
    _,path=setup
    fix=f'db:{save_event("","","","","Вира",path)}'
    remember_delivery(path,fix,message(1001),"voice")
    save_edit_draft(path,fix,"Вира","Віра")
    bot=service(path);bot._user_state=Mock(return_value=None)
    sync_corrections(bot,now=100)
    bot._request.assert_not_called()
    assert sync_corrections(service(path),now=131)==1


@pytest.mark.parametrize("text_fallback",[False,True])
def test_actual_send_records_voice_or_fallback_message_id(setup,text_fallback):
    _,path=setup
    fix=f'db:{save_event("","","","","Вира",path)}'
    bot=service(path)
    bot._request.return_value=response({"ok":True,"result":message(1001,22)})
    bot.pcm_to_ogg=Mock(side_effect=FileNotFoundError() if text_fallback else None,return_value=b"OggSfake")
    bot.send_voice(b"\0"*32000,"Вира",fix,[{"chat_id":1001}])
    save_edit_draft(path,fix,"Вира","Віра")
    bot._request.reset_mock();bot._request.return_value=response()
    assert sync_corrections(bot)==1
    assert bot._request.call_args.args[1]==("editMessageText" if text_fallback else "editMessageCaption")
    assert bot._request.call_args.kwargs["data"]["message_id"]==22


def test_retries_bounded_and_new_revision_retries_again(setup):
    from services.telegram_sync import sync_status
    _,path=setup
    fix=f'db:{save_event("","","","","Вира",path)}'
    remember_delivery(path,fix,message(1001),"voice")
    save_edit_draft(path,fix,"Вира","Віра")
    bot=service(path);bot._request.return_value=response({"ok":False,"description":"test failure"},500)
    for attempt in range(10):sync_corrections(bot,now=100+attempt*31)
    assert bot._request.call_count==8
    assert sync_status(path)["exhausted"]==1
    save_edit_draft(path,fix,"Віра","Віра!")
    assert sync_corrections(service(path),now=1000)==1


def test_resolved_access_cards_are_removed_for_all_admins(setup):
    _,path=setup
    bot=service(path)
    for admin in [1001,4004]:
        bot._request.return_value=response({"ok":True,"result":message(admin,admin+1)})
        assert bot.send_status("Запит на доступ",chat_id=admin,slot="access:5005")
    bot.finish_access_cards(5005)
    bot._request.reset_mock();bot._request.return_value=response()
    bot.maintenance_once()
    deletes=[call.kwargs["data"]["chat_id"] for call in bot._request.call_args_list if call.args[1]=="deleteMessage"]
    assert sorted(deletes)==[1001,4004]


def test_unexpected_maintenance_error_does_not_kill_worker(setup):
    _,path=setup
    bot=service(path)
    def work():
        if bot.maintenance_once.call_count==1:
            raise RuntimeError("synthetic unexpected failure")
        bot._maintenance_stop.set()
    bot.maintenance_once=Mock(side_effect=work)
    bot._maintenance_stop.wait=Mock(return_value=False)
    bot.start_maintenance()
    bot._maintenance_thread.join(timeout=1)
    assert bot.maintenance_once.call_count==2
    assert not bot._maintenance_thread.is_alive()


@pytest.mark.parametrize("temporary",[False,True])
def test_corrected_message_header_is_recalculated_from_new_text(setup,temporary):
    """«Кому / Від / Подія» в Telegram перераховуються з виправленого тексту, а не зникають."""
    _,path=setup
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM dictionary_entries")
        db.executemany("INSERT INTO dictionary_entries (dictionary_type,line_number,raw_line) VALUES (?,?,?)",
            [("callsigns",1,"вишня: 210 300"),("callsigns",2,"клен"),("callsigns",3,"ясень"),("telegram",1,"перевірка")])
    # Синтетична фраза: неправильні позивні й номер, справжнього ефіру тут немає.
    original="Віхта-210, віхта-210, віхта-450, клен, ясень, неясно"
    corrected="Вишня 210, Вишня 210, я Вишня 300, перевірка звʼязку, прийом"
    if temporary:
        register_phrase(path,"b"*32,original); fix="mem:"+"b"*32
    else:
        fix=f'db:{save_event("клен, ясень","","клен, ясень","",original,path,raw_text=original)}'
    remember_delivery(path,fix,message(1001),"voice")
    save_edit_draft(path,fix,original,corrected)
    bot=service(path)
    assert sync_corrections(bot)==1
    assert bot._request.call_args.kwargs["data"]["caption"]==(
        "✏️ Виправлено оператором\nПодія: перевірка\nКому: вишня 210 | Від: вишня 300\n\n"+corrected)
    if not temporary:
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT recipient,sender FROM radio_events WHERE id=?",(int(fix[3:]),)).fetchone()==("вишня 210","вишня 300")
    # Словник змінено без перезапуску: наступна правка бере нову шапку.
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM dictionary_entries WHERE dictionary_type='telegram'")
    save_edit_draft(path,fix,corrected,corrected+"!")
    sync_corrections(bot)
    assert bot._request.call_args.kwargs["data"]["caption"].startswith("✏️ Виправлено оператором\nПодія: Ефір\nКому: вишня 210 | Від: вишня 300\n")
