import io
import json
import wave
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from api.local_dashboard import create_dashboard
from services.miniapp_management import list_users
from services.notification_samples import add_sample, delete_sample, list_samples, ogg_to_wav
from tests.test_miniapp_management import setup
from tests.test_web_api import auth_header
from tests.test_notification_filter import segment, pcm

ORIGIN="http://127.0.0.1:8766"
KEY="x"*43


def wav(samples=None):
    output=io.BytesIO()
    with wave.open(output,"wb") as audio:
        audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000)
        audio.writeframes(pcm(segment() if samples is None else samples))
    return output.getvalue()


@pytest.fixture
def local(setup,tmp_path):
    _,path=setup
    app=create_dashboard(path,KEY,samples_dir=tmp_path/"samples",diagnostics_path=tmp_path/"diagnostics.json",recordings_dir=tmp_path/"recordings")
    with TestClient(app,base_url=ORIGIN,client=("127.0.0.1",12345)) as client:
        assert client.post("/session",json={"token":KEY},headers={"Origin":ORIGIN}).status_code==204
        yield client,path,tmp_path


def test_local_auth_host_origin_and_remote_are_enforced(setup,tmp_path):
    _,path=setup
    app=create_dashboard(path,KEY,samples_dir=tmp_path/"samples")
    with TestClient(app,base_url=ORIGIN,client=("127.0.0.1",12345)) as client:
        assert client.get("/api/summary").status_code==401
        assert client.get("/preview/admin/api/phrases").status_code==401
        assert client.post("/session",json={"token":KEY},headers={"Origin":"https://attacker.example"}).status_code==403
        assert client.post("/session",json={"token":"wrong"},headers={"Origin":ORIGIN}).status_code==403
        result=client.post("/session",json={"token":KEY},headers={"Origin":ORIGIN})
        assert result.status_code==204
        assert "HttpOnly" in result.headers["set-cookie"] and "SameSite=strict" in result.headers["set-cookie"]
        assert client.get("/api/summary",headers={"Host":"attacker.example"}).status_code==403
        assert client.put("/api/settings",json={},headers={"Origin":"https://attacker.example"}).status_code==403
    with TestClient(app,base_url=ORIGIN,client=("192.0.2.1",12)) as remote:
        assert remote.get("/").status_code==403


@pytest.mark.parametrize("role",["viewer","editor","admin"])
def test_preview_uses_actual_role_permissions_and_all_writes_are_blocked(local,role):
    client,path,_=local
    prefix="/preview/"+role
    result=client.get(prefix+"/")
    assert result.status_code==200
    assert f'content="{prefix}"' in result.text
    assert "telegram.org/js" not in result.text
    assert client.get(prefix+"/api/me?selected=admin").json()["role"]==role
    assert client.get(prefix+"/api/status").status_code==200
    assert client.get(prefix+"/api/phrases").status_code==(403 if role=="viewer" else 200)
    assert client.get(prefix+"/api/users").status_code==(200 if role=="admin" else 403)
    before=list_users(path,1001)
    assert client.put(prefix+"/api/users/1001",json={"role":"viewer","enabled":False},headers={"Origin":ORIGIN}).status_code==403
    assert client.put(prefix+"/api/notifications/mode",json={"mode":"off"},headers={"Origin":ORIGIN}).status_code==403
    assert list_users(path,1001)==before


def test_dashboard_routes_never_exist_on_public_api(setup):
    client,_=setup
    for route in ["/api/summary","/api/diagnostics","/api/samples","/preview/admin/api/me","/admin.js","/session"]:
        assert client.get(route,headers=auth_header(1001)).status_code==404


def test_diagnostics_whitelist_and_no_cache(local):
    client,_,tmp=local
    (tmp/"diagnostics.json").write_text(json.dumps({"schema":1,"updated_at":100,"token":"secret","text":"private", "counters":{"asr.calls":4},"gauges":{"text":"private"}}))
    result=client.get("/api/diagnostics")
    assert result.status_code==200
    assert "secret" not in result.text and "private" not in result.text
    assert result.json()["counters"]["asr.calls"]==4
    assert result.headers["cache-control"]=="no-store"
    assert "frame-ancestors 'self'" in result.headers["content-security-policy"]


def test_local_settings_preserve_validation_and_conflicts(local):
    client,_,_=local
    data=client.get("/api/summary").json()["settings"]
    payload={"values":data["values"],"expected_revision":data["revision"]}
    assert client.put("/api/settings",json=payload,headers={"Origin":ORIGIN}).status_code==200
    assert client.put("/api/settings",json=payload,headers={"Origin":ORIGIN}).status_code==409
    payload["values"]["VAD_LEVEL"]=99
    assert client.put("/api/settings",json=payload,headers={"Origin":ORIGIN}).status_code==422


def test_sample_upload_test_and_delete_never_use_working_directory(local):
    client,_,tmp=local
    content=wav()
    assert client.put("/api/samples",content=content,headers={"Origin":ORIGIN}).status_code==422
    result=client.put("/api/samples",content=content,headers={"Origin":ORIGIN,"X-Confirm-Isolated":"yes"})
    assert result.status_code==200
    sample=result.json()["id"]
    assert (tmp/"samples"/sample).stat().st_mode & 0o777==0o600
    result=client.post("/api/samples/test",content=content,headers={"Origin":ORIGIN})
    assert result.json()=={"isolated_match":True,"templates":1}
    assert client.get("/api/samples").json()["items"][0]["id"]==sample
    assert client.delete("/api/samples/"+sample,headers={"Origin":ORIGIN}).status_code==200
    assert not (tmp/"samples"/sample).exists()
    assert client.put("/api/samples",content=b"x"*200001,headers={"Origin":ORIGIN,"X-Confirm-Isolated":"yes"}).status_code==413


def test_samples_invalid_audio_path_traversal_limit_and_symlink(tmp_path):
    directory=tmp_path/"samples"
    with pytest.raises(ValueError):add_sample(directory,b"not wav",confirmed_isolated=True)
    with pytest.raises(ValueError):add_sample(directory,wav())
    for _ in range(16):add_sample(directory,wav(),confirmed_isolated=True)
    with pytest.raises(ValueError):add_sample(directory,wav(),confirmed_isolated=True)
    with pytest.raises(ValueError):delete_sample(directory,"../secret.wav")
    linked=tmp_path/"linked";linked.symlink_to(directory,target_is_directory=True)
    with pytest.raises(ValueError):add_sample(linked,wav(),confirmed_isolated=True)


def test_phrase_enrollment_requires_admin_confirmation_and_available_audio(setup,tmp_path):
    client,path=setup
    from services.telegram_phrase_store import register_phrase
    from services.telegram_media_store import remember_voice
    register_phrase(path,"a"*32,"Звук")
    fix="mem:"+"a"*32
    remember_voice(path,fix,"test-file")
    payload={"fix_id":fix,"confirmed_isolated":True}
    for uid in [2002,3003]:
        assert client.put("/api/notification-sounds/from-phrase",json=payload,headers=auth_header(uid)).status_code==403
    with patch("api.management.download_voice",return_value=b"fake") as download, patch("services.notification_samples.ogg_to_wav",return_value=wav()), patch("config.NOTIFICATION_SOUNDS_DIR",tmp_path/"samples"):
        payload["confirmed_isolated"]=False
        assert client.put("/api/notification-sounds/from-phrase",json=payload,headers=auth_header(1001)).status_code==422
        download.assert_not_called()
        payload["confirmed_isolated"]=True
        assert client.put("/api/notification-sounds/from-phrase",json=payload,headers=auth_header(1001)).status_code==200
    assert len(list_samples(tmp_path/"samples")["items"])==1


def test_real_ffmpeg_conversion_creates_bounded_readable_pcm_wav():
    import shutil
    import subprocess
    from services.notification_samples import validate_wav
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    encoded=subprocess.run(["ffmpeg","-loglevel","error","-f","wav","-i","pipe:0","-c:a","libopus","-f","ogg","pipe:1"],input=wav(),capture_output=True,check=True,timeout=15).stdout
    converted=ogg_to_wav(encoded)
    _,duration=validate_wav(converted)
    assert 1.5<duration<2


# ---------- Доступ без перезапуску й розширена панель ----------

def test_session_survives_dashboard_restart_with_same_key(setup,tmp_path):
    _,path=setup
    first=create_dashboard(path,KEY,samples_dir=tmp_path/"samples")
    with TestClient(first,base_url=ORIGIN,client=("127.0.0.1",1)) as client:
        client.post("/session",json={"token":KEY},headers={"Origin":ORIGIN})
        cookie=client.cookies.get("radio345_local_admin")
        assert "Max-Age=2592000" in client.post("/session",json={"token":KEY},headers={"Origin":ORIGIN}).headers["set-cookie"]
    restarted=create_dashboard(path,KEY,samples_dir=tmp_path/"samples")
    with TestClient(restarted,base_url=ORIGIN,client=("127.0.0.1",1),cookies={"radio345_local_admin":cookie}) as client:
        assert client.get("/api/overview").status_code==200, "закладка працює після перезапуску панелі"
    other=create_dashboard(path,"y"*43,samples_dir=tmp_path/"samples")
    with TestClient(other,base_url=ORIGIN,client=("127.0.0.1",1),cookies={"radio345_local_admin":cookie}) as client:
        assert client.get("/api/overview").status_code==401, "новий ключ анулює старі сесії"


def test_overview_has_everything_for_home_page(local):
    client,path,_=local
    from services.event_store import save_event
    save_event("дуб","","","","Дуб 700",path)
    data=client.get("/api/overview").json()
    assert data["overview"]["events"]==1
    assert sum(data["events_by_hour"]["counts"])==1 and len(data["events_by_hour"]["counts"])==24
    assert {u["role"] for u in data["users"]}>={"admin","editor","viewer"}
    assert all(set(u)>={"user_id","role","enabled","name","mode"} for u in data["users"])
    assert data["backups"]==[] and "telegram_sync" in data and "diagnostics" in data


def test_phrases_audio_and_marking_notification_from_dashboard(local):
    client,path,tmp=local
    from services.telegram_phrase_store import register_phrase
    from services.telegram_media_store import remember_voice
    register_phrase(path,"b"*32,"дзинь")
    fix="mem:"+"b"*32
    remember_voice(path,fix,"voice-id")
    items=client.get("/api/phrases?q=дзинь").json()["items"]
    assert items[0]["fix_id"]==fix and items[0]["has_audio"] is True
    with patch("api.local_dashboard.download_voice",return_value=b"OggS") as download:
        audio=client.get(f"/api/phrases/audio?fix_id={fix}")
        assert audio.status_code==200 and audio.content==b"OggS" and audio.headers["content-type"]=="audio/ogg"
        bad=client.put("/api/phrases/notification-sample",json={"fix_id":fix,"confirmed_isolated":False},headers={"Origin":ORIGIN})
        assert bad.status_code==422
        with patch("services.notification_samples.ogg_to_wav",return_value=wav()):
            ok=client.put("/api/phrases/notification-sample",json={"fix_id":fix,"confirmed_isolated":True},headers={"Origin":ORIGIN})
        assert ok.status_code==200
        assert download.call_args.args[1]=="voice-id"
    assert len(list_samples(tmp/"samples")["items"])==1
    assert client.get("/api/phrases/audio?fix_id=mem:"+"c"*32).status_code==404
    assert client.get("/api/phrases/audio?fix_id=../../etc").status_code==422


def test_manual_backup_from_dashboard(setup,tmp_path):
    _,path=setup
    app=create_dashboard(path,KEY,samples_dir=tmp_path/"samples",backup_dir=tmp_path/"backups")
    with TestClient(app,base_url=ORIGIN,client=("127.0.0.1",1)) as client:
        client.post("/session",json={"token":KEY},headers={"Origin":ORIGIN})
        assert client.post("/api/backups").status_code==403, "запис без Origin заборонено"
        result=client.post("/api/backups",headers={"Origin":ORIGIN}).json()
        assert result["name"].startswith("events-auto-") and len(result["items"])==1
        assert client.get("/api/backups").json()["items"][0]["name"]==result["name"]


def test_events_by_hour_buckets(tmp_path):
    import sqlite3
    from api.local_dashboard import events_by_hour
    from services.event_store import init_db
    db=tmp_path/"e.db"; init_db(db)
    now=1_800_001_800  # 30 хв після початку години
    with sqlite3.connect(db) as conn:
        for offset in (0, 60, 3600*5, 3600*30):
            conn.execute("INSERT INTO radio_events (ts,text) VALUES (datetime(?, 'unixepoch'),'x')",(now-offset,))
    series=events_by_hour(db,now=now)
    assert series["counts"][-1]==2 and series["counts"][-6]==1 and sum(series["counts"])==3


def _phrase_with_voice(path, token, text):
    from services.telegram_phrase_store import register_phrase
    from services.telegram_media_store import remember_voice
    register_phrase(path, token, text)
    fix = "mem:" + token
    remember_voice(path, fix, "voice-" + token[:4])
    return fix


def test_archive_labels_feed_filter_evaluation(local):
    client,path,tmp=local
    import numpy as np
    from services.recordings import RecordingStore
    from tests.test_notification_filter import phone_sound, voice_like
    add_sample(tmp/"samples",wav(phone_sound()),confirmed_isolated=True)
    store=RecordingStore(tmp/"recordings")
    signal=store.save(pcm(segment(phone_sound()*0.4)),16000,{"status":"ready"})
    speech=store.save(pcm(np.concatenate((np.zeros(4000),voice_like(1.5)*0.2,np.zeros(8000)))),16000,{"status":"ready"})
    store.review(signal,"","notification",0)
    store.review(speech,"вишня","speech",0)
    state=client.get("/api/notification-filter").json()
    assert state["labels"]=={"notification":1,"speech":1,"noise":0} and state["samples"]==1
    assert state["spectral"]["mode"] in ("shadow","active","off")
    report=client.get("/api/notification-filter/evaluation").json()
    by_id={i["record_id"]:i for i in report["items"]}
    assert by_id[signal]["candidate"] is True and by_id[speech]["candidate"] is False
    assert report["summary"]["status"]=="need_more"
    # Окремої розмітки фраз більше немає: мітки — лише в аудіоархіві.
    assert client.put("/api/phrases/label",json={"fix_id":"mem:"+"d"*32,"label":"speech"},headers={"Origin":ORIGIN}).status_code in (404,405)


def test_spectral_observations_reach_filter_page_and_phrases(local):
    client,path,tmp=local
    import time as clock
    fix=_phrase_with_voice(path,"a"*32,"дзінь")
    created=[i for i in client.get("/api/phrases").json()["items"] if i["fix_id"]==fix][0]["created_at"]
    from api.local_dashboard import _utc_seconds
    ready=_utc_seconds(created)+0.4
    row={"segment_id":7,"processing_started_unix_s":ready-1,"text_ready_unix_s":ready,"audio_duration_s":1.2,
         "status":"ready","notification_score":0.97,"notification_outside_s":0.0,"notification_extra":0.001,
         "notification_candidate":True,"text":"private"}
    far={**row,"segment_id":8,"text_ready_unix_s":ready-600,"notification_score":0.6,"notification_candidate":False}
    (tmp/"diagnostics.json").write_text(json.dumps({"schema":1,"updated_at":clock.time(),
        "counters":{"asr.spectral.scored":5,"asr.spectral.candidate":1,"asr.spectral.candidate.ready":1,"asr.whisper":3},
        "notification_spectral":{"buckets":[0]*19+[1],"recent":[far,row]}}))
    state=client.get("/api/notification-filter").json()
    assert state["counters"]=={"asr.spectral.scored":5,"asr.spectral.candidate":1,"asr.spectral.candidate.ready":1}
    assert state["observed"]["buckets"][-1]==1 and len(state["observed"]["recent"])==2
    assert state["observed"]["recent"][1].get("text") is None, "довільні рядки не проходять білий список"
    item=[i for i in client.get("/api/phrases").json()["items"] if i["fix_id"]==fix][0]
    assert item["spectral"]=={"score":0.97,"outside_s":0.0,"extra_ratio":0.001,"candidate":True}


def test_each_spectral_observation_labels_at_most_one_phrase():
    from api.local_dashboard import attach_spectral
    items=[{"fix_id":"db:1","created_at":"2026-11-15 10:00:00"},{"fix_id":"db:2","created_at":"2026-11-15 10:00:01"},
           {"fix_id":"db:3","created_at":"2026-11-15 11:00:00"}]
    from api.local_dashboard import _utc_seconds
    base=_utc_seconds("2026-11-15 10:00:01")
    attach_spectral(items,[{"text_ready_unix_s":base+0.4,"notification_score":0.9,"notification_candidate":True}])
    assert "spectral" not in items[0] and items[1]["spectral"]["score"]==0.9 and "spectral" not in items[2]


def test_review_queue_filters_archive_and_reports_progress(setup, tmp_path):
    _,path=setup
    from services.recordings import RecordingStore
    store=RecordingStore(tmp_path/"recordings")
    ids=[store.save(pcm(segment()),16000,{"status":"ready"}) for _ in range(4)]
    store.review(ids[0],"текст","speech",0)
    app=create_dashboard(path,KEY,samples_dir=tmp_path/"samples",recordings_dir=tmp_path/"recordings")
    with TestClient(app,base_url=ORIGIN,client=("127.0.0.1",1)) as client:
        client.post("/session",json={"token":KEY},headers={"Origin":ORIGIN})
        state=client.get("/api/recordings/queue").json()
        assert state["available"] is True and state["live"] is True
        a=state["groups"][0]
        assert (a['key'],a['total'],a['reviewed'],a['pending'])==('A',4,1,3)
        page=client.get("/api/recordings?queue=A&review=pending").json()
        assert [i["id"] for i in page["items"]]==ids[1:] and page["total"]==3
        assert {i["id"] for i in client.get("/api/recordings?queue=all").json()["items"]}==set(ids)
        assert len(client.get("/api/recordings").json()["items"])==4, "без черги — увесь архів"
        # Arriving audio immediately enters the same queue, without recreating a file or app.
        new=store.save(pcm(segment()),16000,{'status':'ready'})
        page=client.get('/api/recordings?queue=A&review=pending').json()
        assert [i['id'] for i in page['items']]==ids[1:]+[new]
        store.review(new,'нова фраза','speech',0)
        a=client.get('/api/recordings/queue').json()['groups'][0]
        assert (a['total'],a['reviewed'],a['pending'])==(5,2,3)
        assert {i['id'] for i in client.get('/api/recordings?queue=A&review=reviewed').json()['items']}=={ids[0],new}
        assert client.get("/api/recordings?queue=Z").status_code==404
        assert client.get("/api/recordings?queue=a;drop").status_code==422


def test_listening_hold_endpoint_requires_origin_and_bounds(local):
    client,path,_=local
    from services.receiver_control import reception_request
    assert client.put("/api/listening-hold",json={"seconds":10}).status_code==403
    assert client.put("/api/listening-hold",json={"seconds":10},headers={"Origin":ORIGIN}).json()["active"] is True
    assert reception_request(path)=="listening"
    assert client.put("/api/listening-hold",json={"seconds":0},headers={"Origin":ORIGIN}).status_code==200
    assert reception_request(path) is None
    assert client.put("/api/listening-hold",json={"seconds":500},headers={"Origin":ORIGIN}).status_code==422


def test_callsign_suggestions_come_from_operator_text_without_role_guess(local):
    client,path,_=local
    import sqlite3
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM dictionary_entries WHERE dictionary_type='callsigns'")
        db.executemany("INSERT INTO dictionary_entries (dictionary_type,line_number,raw_line) VALUES ('callsigns',?,?)",
                       [(1,"вишня: 210 300"),(2,"клен: 300"),(3,"дуб")])
    body={"text":"Вишня 210, Вишня 210, я Клен 300, дуб, прийом"}
    assert client.post("/api/recordings/suggest-roles",json=body).status_code==403
    result=client.post("/api/recordings/suggest-roles",json=body,headers={"Origin":ORIGIN}).json()
    assert result=={"callsigns":["вишня 210","клен 300","дуб"]}, "без повторів і без підказки ролей"
    assert client.post("/api/recordings/suggest-roles",json={"text":"шум"},headers={"Origin":ORIGIN}).json()=={"callsigns":[]}



def test_review_accepts_room_label_and_multiple_messages(local):
    client,path,tmp=local
    from services.recordings import RecordingStore
    store=RecordingStore(tmp/"recordings")
    record=store.save(pcm(segment()),16000,{"status":"ready","final_text":"текст"})
    url=f"/api/recordings/{record}/review"
    bad=client.put(url,json={"reference":"щось","label":"room","expected_revision":0},headers={"Origin":ORIGIN})
    assert bad.status_code==422
    ok=client.put(url,json={"reference":"Вишня 210 прийом Клен 300","label":"speech","expected_revision":0,
                            "multiple_messages":True},headers={"Origin":ORIGIN})
    assert ok.status_code==200 and ok.json()["multiple_messages"]==1
    room=client.put(url,json={"reference":"","label":"room","expected_revision":1},headers={"Origin":ORIGIN})
    assert room.json()["label"]=="room" and room.json()["multiple_messages"] is None


def test_callsign_alias_candidates_read_archive_without_writing(local):
    client,path,tmp=local
    import sqlite3
    from services.recordings import RecordingStore
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM dictionary_entries WHERE dictionary_type='callsigns'")
        db.executemany("INSERT INTO dictionary_entries (dictionary_type,line_number,raw_line) VALUES ('callsigns',?,?)",
                       [(1,"вишня: 210"),(2,"клен: 300")])
    store=RecordingStore(tmp/"recordings")
    one=store.save(pcm(segment()),16000,{"status":"ready","raw_text":"гуща 210, прийом"})
    noise=store.save(pcm(segment()),16000,{"status":"ready","raw_text":"гуща 210"})
    store.review(one,"Вишня 210, прийом","speech",0)
    store.review(noise,"","noise",0)
    before=client.get("/api/text-lists").json()["revision"]
    data=client.get("/api/text-lists/callsign-alias-candidates").json()
    assert data["records"]==1 and [(c["heard"],c["callsign"],c["count"]) for c in data["items"]]==[("гуща","вишня",1)]
    assert client.get("/api/text-lists").json()["revision"]==before


def test_archive_search_by_id_event_number_and_text(setup, tmp_path):
    _,path=setup
    from services.recordings import RecordingStore
    store=RecordingStore(tmp_path/"recordings")
    a=store.save(pcm(segment()),16000,{"status":"ready","raw_text":"Берега 56, приєм","fix_id":"db:82"})
    b=store.save(pcm(segment()),16000,{"status":"ready","raw_text":"шум"})
    c=store.save(pcm(segment()),16000,{"status":"vad_short","raw_text":""})
    store.review(b,"Вишня 13, 42 42, прийом","speech",0)
    app=create_dashboard(path,KEY,samples_dir=tmp_path/"samples",recordings_dir=tmp_path/"recordings")
    with TestClient(app,base_url=ORIGIN,client=("127.0.0.1",1)) as client:
        client.post("/session",json={"token":KEY},headers={"Origin":ORIGIN})
        found=lambda q: {i["id"] for i in client.get("/api/recordings",params={"q":q,"review":"pending","group":"normal"}).json()["items"]}
        assert found(f"{a[:8]}, {c[:8]}")=={a,c}, "кілька ID; пошук ігнорує групу й стан перевірки"
        assert found("№82")=={a} and found("#82")=={a}
        assert found("ВИШНЯ 13")=={b} and found("берега")=={a}, "текст — без урахування регістру, і еталон, і Whisper"
        assert found("4242")==set() and found("42 42")=={b}
        assert found("нема такого")==set()
        assert client.get("/api/recordings",params={"q":"x"*201}).status_code==422


def test_text_check_endpoint_returns_hints_without_writing(local):
    import sqlite3
    client,path,_=local
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM dictionary_entries")
        db.execute("INSERT INTO dictionary_entries (dictionary_type,line_number,raw_line) VALUES ('callsigns',1,'береза: 31 56')")
    body=client.post("/api/text-check",json={"text":"береза 99, 4242, с півночі"},headers={"Origin":ORIGIN}).json()
    assert body["formatted"]=="береза 99, 42 42, з півночі"
    assert [i["kind"] for i in body["issues"]]==["format","format","info"]
    assert client.post("/api/text-check",json={"text":"x"*5001},headers={"Origin":ORIGIN}).status_code==422
    assert client.post("/api/text-check",json={"text":"x"}).status_code==403, "без Origin — відмова"


def test_dictionaries_are_edited_from_the_panel_with_miniapp_rules(local):
    client,path,_=local
    h={"Origin":ORIGIN}
    data=client.get("/api/dictionaries").json()
    assert set(data)=={"callsigns","commands","telegram"}
    entries=[{"raw_line":"береза: 31 56","enabled":True},{"raw_line":"вишня","enabled":False}]
    saved=client.put("/api/dictionaries/callsigns",json={"entries":entries,"expected_version":data["callsigns"]["version"]},headers=h)
    assert saved.status_code==200 and saved.json()["entries"]==entries
    import sqlite3
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT raw_line, enabled FROM dictionary_entries WHERE dictionary_type='callsigns' ORDER BY line_number").fetchall()==[("береза: 31 56",1),("вишня",0)]
    stale={"entries":entries,"expected_version":data["callsigns"]["version"]}
    assert client.put("/api/dictionaries/callsigns",json=stale,headers=h).status_code==409, "застаріла версія"
    version=saved.json()["version"]
    bad=client.put("/api/dictionaries/callsigns",json={"entries":[{"raw_line":"береза мала: 5","enabled":True}],"expected_version":version},headers=h)
    assert bad.status_code==422 and "одним словом" in bad.json()["detail"]
    empty=client.put("/api/dictionaries/callsigns",json={"entries":[{"raw_line":"вишня","enabled":False}],"expected_version":version},headers=h)
    assert empty.status_code==422, "приймач не лишається без позивних"
    assert client.put("/api/dictionaries/other",json={"entries":[],"expected_version":version},headers=h).status_code==404
    assert client.put("/api/dictionaries/callsigns",json={"entries":entries,"expected_version":version}).status_code==403


def test_archive_listing_carries_text_check_of_references(setup, tmp_path):
    _,path=setup
    from services.recordings import RecordingStore
    store=RecordingStore(tmp_path/"recordings")
    remark=store.save(pcm(segment()),16000,{"status":"ready"})
    clean=store.save(pcm(segment()),16000,{"status":"ready"})
    pending=store.save(pcm(segment()),16000,{"status":"ready"})
    store.review(remark,"Вишня, 4242, с півночі","speech",0)
    store.review(clean,"Вишня, 42 42, з півночі","speech",0)
    app=create_dashboard(path,KEY,samples_dir=tmp_path/"samples",recordings_dir=tmp_path/"recordings")
    with TestClient(app,base_url=ORIGIN,client=("127.0.0.1",1)) as client:
        client.post("/session",json={"token":KEY},headers={"Origin":ORIGIN})
        items={i["id"]:i for i in client.get("/api/recordings").json()["items"]}
    assert items[remark]["text_check"]["formatted"]=="Вишня, 42 42, з півночі"
    assert items[clean]["text_check"]=={"issues":[],"formatted":None}
    assert "text_check" not in items[pending]
