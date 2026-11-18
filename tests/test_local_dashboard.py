import io
import os
import json
import wave
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from api.local_dashboard import create_dashboard
from services.management.users import list_users
from services.notification_samples import add_sample, delete_sample, list_samples
from tests.test_miniapp_management import setup
from tests.test_web_api import auth_header
from tests.audio_helpers import segment, pcm

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
    assert client.get(prefix+"/api/users").status_code==404, "керування користувачами — лише в панелі"
    before=list_users(path,1001)
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
    assert result.json()=={"isolated_match":True,"templates":1,"mode":"active"}
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
    from api.panel.common import events_by_hour
    from services.event_store import init_db
    db=tmp_path/"e.db"; init_db(db)
    now=1_800_001_800  # 30 хв після початку години
    with sqlite3.connect(db) as conn:
        for offset in (0, 60, 3600*5, 3600*30):
            conn.execute("INSERT INTO radio_events (ts,text) VALUES (datetime(?, 'unixepoch'),'x')",(now-offset,))
    series=events_by_hour(db,now=now)
    assert series["counts"][-1]==2 and series["counts"][-6]==1 and sum(series["counts"])==3


def test_archive_labels_feed_filter_evaluation(local):
    client,path,tmp=local
    import numpy as np
    from services.recordings import RecordingStore
    from tests.audio_helpers import phone_sound, voice_like
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


def test_spectral_observations_reach_filter_page(local):
    client,path,tmp=local
    import time as clock
    ready=clock.time()
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
    assert client.get("/api/phrases").status_code==404, "журнал фраз тепер — вид архіву"


def test_current_archive_views_refresh_without_rewriting_samples(setup, tmp_path):
    _, path = setup
    from services.recordings import RecordingStore
    store = RecordingStore(tmp_path/'recordings')
    ids = [store.save(pcm(segment()), 16000, {'status':'ready', 'fix_id':f'db:{i+1}'}) for i in range(4)]
    store.review(ids[0], 'текст', 'speech', 0)
    app = create_dashboard(path, KEY, samples_dir=tmp_path/'samples', recordings_dir=store.root)
    with TestClient(app, base_url=ORIGIN, client=('127.0.0.1',1)) as client:
        client.post('/session', json={'token':KEY}, headers={'Origin':ORIGIN})
        page = client.get('/api/recordings?view=radio').json()
        assert {i['id'] for i in page['items']} == set(ids[1:])
        assert page['views']['radio'] == 3 and page['views']['reviewed'] == 1
        new = store.save(pcm(segment()), 16000, {'status':'ready', 'fix_id':'db:9'})
        assert client.get('/api/recordings?view=radio').json()['total'] == 4
        store.review(new, 'нова фраза', 'speech', 0)
        assert {i['id'] for i in client.get('/api/recordings?view=reviewed').json()['items']} == {ids[0], new}



def test_listening_hold_endpoint_requires_origin_and_bounds(local):
    client,path,_=local
    from services.receiver_control import reception_request
    assert client.put("/api/listening-hold",json={"seconds":10}).status_code==403
    assert client.put("/api/listening-hold",json={"seconds":10},headers={"Origin":ORIGIN}).json()["active"] is True
    assert reception_request(path)=="listening"
    assert client.put("/api/listening-hold",json={"seconds":0},headers={"Origin":ORIGIN}).status_code==200
    assert reception_request(path) is None
    assert client.put("/api/listening-hold",json={"seconds":500},headers={"Origin":ORIGIN}).status_code==422


def test_callsign_suggestions_and_roles_for_two_callsigns(local):
    client,path,_=local
    import sqlite3
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM dictionary_entries WHERE dictionary_type='callsigns'")
        db.executemany("INSERT INTO dictionary_entries (dictionary_type,line_number,raw_line) VALUES ('callsigns',?,?)",
                       [(1,"вишня: 210 300"),(2,"клен: 300"),(3,"дуб")])
    body={"text":"Вишня 210, Вишня 210, я Клен 300, дуб, прийом"}
    assert client.post("/api/recordings/suggest-roles",json=body).status_code==403
    result=client.post("/api/recordings/suggest-roles",json=body,headers={"Origin":ORIGIN}).json()
    assert result["callsigns"]==["вишня 210","клен 300","дуб"], "без повторів"
    assert result["roles"]=={"recipient":"вишня 210","sender":"клен 300"}, "«я Х» — Х відправник, скільки б позивних не було"
    assert client.post("/api/recordings/suggest-roles",json={"text":"шум"},headers={"Origin":ORIGIN}).json()=={"callsigns":[],"roles":None}
    two=lambda text: client.post("/api/recordings/suggest-roles",json={"text":text},headers={"Origin":ORIGIN}).json()["roles"]
    assert two("Вишня 210, я Клен 300, прийом")=={"recipient":"вишня 210","sender":"клен 300"}
    assert two("Вишня 210, Клен 300, прийнято")=={"recipient":"вишня 210","sender":"клен 300"}
    assert two("Вишня 210, прийнято / Клен 300, прийнято") is None, "кілька передач — ролі не підставляються"
    assert two("Вишня 210, прийнято | Клен 300, прийнято") is None, "старий роздільник теж"
    assert two("Вишня 210, посилку отримав") is None, "один позивний — оператор вирішує сам"
    assert two("я Клен 300, 48 62, прийом")=={"recipient":None,"sender":"клен 300"}, "лише «я Х» — тільки «Від»"
    assert two("Вишня 210, дуб, я Клен 300, прийом")=={"recipient":None,"sender":"клен 300"}, "кілька адресатів — «Кому» не вгадуємо"
    assert two("Вишня 210, прийнято / я Клен 300") is None, "кілька передач — ролі не підставляються"



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
        found=lambda q: {i["id"] for i in client.get("/api/recordings",params={"q":q,"view":"speech"}).json()["items"]}
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
    body=client.post("/api/text-check",json={"text":"Береза 99, 4242, с півночі"},headers={"Origin":ORIGIN}).json()
    assert body["formatted"]=="Береза 99, 42 42, з півночі"
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
    clean_issues=items[clean]["text_check"]["issues"]
    assert [i.get("dictionary_entry") for i in clean_issues]==["вишня"], "лише пропозиція додати новий позивний (далі — код)"
    assert items[clean]["text_check"]["formatted"] is None
    assert "text_check" not in items[pending]


def test_archive_speech_and_radio_groups(setup, tmp_path):
    _,path=setup
    import sqlite3
    from services.recordings import RecordingStore
    store=RecordingStore(tmp_path/"recordings")
    def rec(status, duration, **data):
        rid=store.save(pcm(segment()),16000,{"status":status,**data})
        with sqlite3.connect(tmp_path/"recordings"/"index.sqlite3") as db:
            db.execute("UPDATE recordings SET data=json_set(data,'$.signal.duration_s',?) WHERE id=?",(duration,rid))
        return rid
    room=rec("ready",4.0,raw_text="розмова в кімнаті")
    radio=rec("ready",3.5,raw_text="Береза 56, прийом",predicted_recipient="береза 56")
    event=rec("ready",3.0,raw_text="злива 42 42",fix_id="db:7")
    loop=rec("empty_text",9.0,raw_text="")
    short=rec("ready",1.5,raw_text="Ага")
    rec("blacklist",3.0,raw_text="Дякую за перегляд!")
    rec("vad_short",0.3)
    app=create_dashboard(path,KEY,samples_dir=tmp_path/"samples",recordings_dir=tmp_path/"recordings")
    with TestClient(app,base_url=ORIGIN,client=("127.0.0.1",1)) as client:
        client.post("/session",json={"token":KEY},headers={"Origin":ORIGIN})
        page=lambda group: client.get("/api/recordings",params={"view":group}).json()
        assert {i["id"] for i in page("speech")["items"]}=={room,loop}, "інше мовлення не дублює ефір"
        assert {i["id"] for i in page("radio")["items"]}=={radio,event}, "позивний або подія"
        views=page("speech")["views"]
        assert (views["speech"],views["radio"])==(2,2)
        assert short not in {i["id"] for i in page("speech")["items"]}


def test_archive_views_split_archive_and_period_limits(setup, tmp_path):
    _,path=setup
    import sqlite3, time
    from services.recordings import RecordingStore
    store=RecordingStore(tmp_path/"recordings")
    def rec(status, duration, age_h=0, **data):
        rid=store.save(pcm(segment()),16000,{"status":status,**data})
        with sqlite3.connect(tmp_path/"recordings"/"index.sqlite3") as db:
            db.execute("UPDATE recordings SET created=?, data=json_set(data,'$.signal.duration_s',?) WHERE id=?",
                       (time.time()-age_h*3600, duration, rid))
        return rid
    radio=rec("ready",3.0,predicted_recipient="береза 56")
    old_radio=rec("ready",3.0,age_h=48,fix_id="db:7")
    room=rec("ready",4.0,raw_text="розмова")
    noise=rec("vad_short",0.4)
    done=rec("ready",3.0,predicted_sender="клен 15")
    store.review(done,"Клен 15, прийом","speech",0)
    app=create_dashboard(path,KEY,samples_dir=tmp_path/"samples",recordings_dir=tmp_path/"recordings")
    with TestClient(app,base_url=ORIGIN,client=("127.0.0.1",1)) as client:
        client.post("/session",json={"token":KEY},headers={"Origin":ORIGIN})
        ids=lambda **p: [i["id"] for i in client.get("/api/recordings",params=p).json()["items"]]
        assert ids(view="radio")==[radio,old_radio], "нерозмічений ефір, нові першими"
        assert ids(view="speech")==[room] and ids(view="filtered")==[noise]
        assert ids(view="reviewed")==[done] and set(ids(view="all"))=={radio,old_radio,room,noise,done}
        day=time.time()-24*3600
        assert ids(view="radio",since=day)==[radio], "період"
        counts=client.get("/api/recordings",params={"view":"radio","since":day}).json()["views"]
        assert (counts["radio"],counts["speech"],counts["filtered"],counts["reviewed"],counts["all"])==(1,1,1,1,4)
        assert client.get("/api/recordings",params={"view":"nope"}).status_code==422


def test_reference_from_panel_updates_linked_event_and_telegram(setup, tmp_path):
    _,path=setup
    import sqlite3
    from services.event_store import save_event
    from services.recordings import RecordingStore
    from services.telegram_sync import remember_delivery
    heard="Берега 56, прийом"
    event=save_event("береза 56","","","",heard,path,raw_text=heard)
    fix=f"db:{event}"
    remember_delivery(path,fix,{"message_id":5,"chat":{"id":1001},"voice":{"file_id":"x"}},"voice")
    store=RecordingStore(tmp_path/"recordings")
    linked=store.save(pcm(segment()),16000,{"status":"ready","fix_id":fix})
    lonely=store.save(pcm(segment()),16000,{"status":"ready"})
    app=create_dashboard(path,KEY,samples_dir=tmp_path/"samples",recordings_dir=tmp_path/"recordings")
    with TestClient(app,base_url=ORIGIN,client=("127.0.0.1",1)) as client:
        client.post("/session",json={"token":KEY},headers={"Origin":ORIGIN})
        review=lambda rid,text,label="speech",rev=0: client.put(f"/api/recordings/{rid}/review",
            json={"reference":text,"label":label,"expected_revision":rev},headers={"Origin":ORIGIN}).json()
        assert review(linked,"Береза 56, прийнято, прийом")["event_sync"]=="updated"
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT text, raw_text FROM radio_events WHERE id=?",(event,)).fetchone()==("Береза 56, прийнято, прийом",heard)
            assert db.execute("SELECT revision FROM correction_sources WHERE fix_id=?",(fix,)).fetchone()[0]>=1, "підпис у Telegram стоїть у черзі"
        assert review(linked,"Береза 56, прийнято, прийом",rev=1)["event_sync"]=="same"
        assert review(lonely,"Береза 56")["event_sync"] is None, "без події нічого не змінюється"
        assert review(linked,"","room",rev=2)["event_sync"] is None, "шум не переписує подію"


def test_learning_queue_shows_why_rules_wait_and_allows_approval(local):
    client,path,_=local
    h={"Origin":ORIGIN}
    import sqlite3
    from services.word_store import init_word_db
    init_word_db(path)
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO word_corrections (original, corrected, confirmations, status) VALUES (?,?,?,?)",
                       [("прием","прийом",2,"active"),("прайом","прийом",1,"pending")])
        db.execute("INSERT INTO word_correction_votes (source_id, original, corrected) VALUES ('db:7','прием','прийом')")
    data=client.get("/api/learning").json()
    by={item["original"]:item for item in data["items"]}
    assert by["прием"]["state"]=="needs_approval" and by["прием"]["sources"]==["db:7"]
    assert by["прайом"]["state"]=="needs_confirmation"
    assert data["counts"]=={"needs_approval":1,"needs_confirmation":1} and data["min_confirmations"]==2
    rule=by["прием"]
    url=f"/api/learning/{rule['id']}/approval"
    assert client.put(url,json={"approved":True,"expected_version":rule["version"]}).status_code==403, "без Origin"
    assert client.put(url,json={"approved":True,"expected_version":rule["version"]},headers=h).status_code==200
    assert {i["original"]:i["state"] for i in client.get("/api/learning").json()["items"]}["прием"]=="applies"
    assert client.put(url,json={"approved":False,"expected_version":rule["version"]},headers=h).status_code==409, "застаріла версія"
    fresh={i["original"]:i for i in client.get("/api/learning").json()["items"]}["прием"]
    assert client.put(f"/api/learning/{fresh['id']}",json={"disabled":True,"expected_version":fresh["version"]},headers=h).status_code==200
    assert {i["original"]:i["state"] for i in client.get("/api/learning").json()["items"]}["прием"]=="disabled"
    assert client.put("/api/learning/999/approval",json={"approved":True,"expected_version":"0"*64},headers=h).status_code==404


def test_launcher_buttons_use_the_authenticated_control_channel(setup, tmp_path):
    from services.launcher_lifecycle import LauncherSession
    _, path = setup
    root = tmp_path / "launcher"
    root.mkdir()
    app = create_dashboard(path, KEY, samples_dir=tmp_path / "samples", diagnostics_path=tmp_path / "d.json",
                           recordings_dir=tmp_path / "recordings", launcher_root=root)
    h = {"Origin": ORIGIN}
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 12345)) as client:
        assert client.post("/session", json={"token": KEY}, headers=h).status_code == 204
        assert client.get("/api/launcher").json() == {"managed": False, "reachable": False, "state": "inactive", "receiver_stopped": False}
        assert client.post("/api/launcher/restart", headers=h).status_code == 409, "панель без лаунчера"
        assert client.post("/api/launcher/kill", headers=h).status_code == 422
        session = LauncherSession(root, ["api"]).__enter__()
        try:
            assert client.get("/api/launcher").json()["managed"] is True
            assert client.post("/api/launcher/restart").status_code == 403, "без Origin панелі"
            fifo = root / ".radio345-launcher/control.fifo"
            reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
            session.thread_stop.set(); session.thread.join(timeout=1)   # читаємо канал самі
            try:
                assert client.post("/api/launcher/restart", headers=h).json() == {"sent": "restart"}
                assert os.read(reader, 1024) == f"restart {session.token}\n".encode()
            finally:
                os.close(reader)
        finally:
            session.__exit__(None, None, None)


def test_attention_lists_what_waits_for_the_operator(local):
    client, path, _ = local
    import sqlite3
    from services import daily_codes
    from services.word_store import init_word_db
    data = client.get("/api/attention").json()
    assert data["codes_missing"] is True and data["codes_day"] == daily_codes.local_day()
    assert data["approvals"] == 0 and data["telegram_failed"] == 0 and data["unlabeled_radio_24h"] == 0
    daily_codes.write(path, data["codes_day"], "48 62")
    init_word_db(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO word_corrections (original, corrected, confirmations, status) VALUES ('прием','прийом',2,'active')")
    data = client.get("/api/attention").json()
    assert data["codes_missing"] is False and data["approvals"] == 1


def test_lists_and_dictionaries_are_checked_while_typing_without_saving(local):
    client, path, _ = local
    h = {"Origin": ORIGIN}
    revision = client.get("/api/text-lists").json()["revision"]
    bad = client.put("/api/text-lists/short_garbage/check", headers=h,
                     json={"entries": [{"raw_line": "вишня 12", "enabled": True}]}).json()
    assert bad["errors"], "число в короткому шумі — помилка ще до збереження"
    ok = client.put("/api/text-lists/short_garbage/check", headers=h,
                    json={"entries": [{"raw_line": "пау пау", "enabled": True}]}).json()
    assert ok == {"errors": [], "warnings": []}
    assert client.get("/api/text-lists").json()["revision"] == revision, "перевірка нічого не записує"
    assert client.put("/api/text-lists/other/check", headers=h, json={"entries": []}).status_code == 404
    wrong = client.put("/api/dictionaries/callsigns/check", headers=h,
                       json={"entries": [{"raw_line": "береза мала: 5", "enabled": True}]}).json()
    assert wrong["errors"] and "одним словом" in wrong["errors"][0]
    assert client.put("/api/dictionaries/callsigns/check", headers=h,
                      json={"entries": [{"raw_line": "береза: 5", "enabled": True}]}).json()["errors"] == []
    assert client.get("/api/text-lists/noise-candidates").json() == {"items": [], "records": 0}


def test_archive_shows_what_went_to_telegram_and_turns_a_clean_signal_into_a_sample(setup, tmp_path):
    _, path = setup
    from services.recordings import RecordingStore
    store = RecordingStore(tmp_path/'recordings')
    sent = store.save(pcm(segment()), 16000, {'status': 'ready', 'fix_id': 'mem:' + 'a'*32, 'notification_score': 0.91})
    store.save(pcm(segment()), 16000, {'status': 'low_rms'})
    reviewed = store.save(pcm(segment()), 16000, {'status': 'ready', 'fix_id': 'db:5'})
    store.review(reviewed, 'текст', 'speech', 0)
    app = create_dashboard(path, KEY, samples_dir=tmp_path/'samples', recordings_dir=store.root)
    with TestClient(app, base_url=ORIGIN, client=('127.0.0.1', 1)) as client:
        client.post('/session', json={'token': KEY}, headers={'Origin': ORIGIN})
        page = client.get('/api/recordings?view=telegram').json()
        assert {i['id'] for i in page['items']} == {sent, reviewed}, "і розмічені, і ні — усе надіслане"
        url = f'/api/recordings/{sent}/notification-sample'
        assert client.post(url, json={'confirmed_isolated': False}, headers={'Origin': ORIGIN}).status_code == 422
        assert client.post(url, json={'confirmed_isolated': True}).status_code == 403, "без Origin панелі"
        assert client.post(url, json={'confirmed_isolated': True}, headers={'Origin': ORIGIN}).status_code == 200
        assert len(list_samples(tmp_path/'samples')['items']) == 1
        assert client.post('/api/recordings/' + '0'*32 + '/notification-sample', json={'confirmed_isolated': True},
                           headers={'Origin': ORIGIN}).status_code == 404


def test_live_feed_quality_experiments_and_pause_for_the_panel(setup, tmp_path):
    _, path = setup
    import json as _json
    from services.recordings import RecordingStore
    store = RecordingStore(tmp_path/'recordings')
    heard = store.save(pcm(segment()), 16000, {'status': 'ready', 'raw_text': 'Вишня 12, прийом', 'final_text': 'Вишня 12, прийом', 'fix_id': 'db:3'})
    store.save(pcm(segment()), 16000, {'status': 'low_rms'})   # тихий після показаного — лише підраховується
    store.review(heard, 'Вишня 12, прийом', 'speech', 0)
    runs = tmp_path/'experiments'
    run = runs/'20261117T120000Z_test-run'
    run.mkdir(parents=True)
    (run/'params.json').write_text(_json.dumps({'variant': 'whisper', 'prompt': 'receiver', 'records': 1}))
    (run/'results.jsonl').write_text(_json.dumps({'id': 'x', 'label': 'speech', 'reference': 'а', 'hypothesis': 'а',
        'score': {'word_errors': 0, 'reference_words': 1, 'char_errors': 0, 'reference_chars': 1, 'reference_has_digits': False}}) + '\n')
    (run/'metrics.json').write_text(_json.dumps({'summary': {'wer': 0.0, 'records': 1}}))
    app = create_dashboard(path, KEY, samples_dir=tmp_path/'samples', recordings_dir=store.root, experiments_dir=runs)
    with TestClient(app, base_url=ORIGIN, client=('127.0.0.1', 1)) as client:
        client.post('/session', json={'token': KEY}, headers={'Origin': ORIGIN})
        feed = client.get('/api/live').json()
        assert [i['id'] for i in feed['items']] == [heard] and feed['skipped_quiet'] == 1
        assert feed['items'][0]['fix_id'] == 'db:3' and feed['items'][0]['reference'] == 'Вишня 12, прийом'
        days = client.get('/api/quality').json()['days']
        assert len(days) == 1 and days[0]['records'] == 1 and days[0]['wer_final'] == 0 and days[0]['numbers'] == 1
        listed = client.get('/api/experiments').json()['items']
        assert listed[0]['label'] == 'test-run' and listed[0]['complete'] and listed[0]['summary']['wer'] == 0.0
        same = client.get('/api/experiments/compare', params={'a': run.name, 'b': run.name}).json()
        assert same['common'] == 1 and same['a']['wer'] == same['b']['wer'] == 0
        assert client.get('/api/experiments/compare', params={'a': '../x', 'b': run.name}).status_code == 422
        pause = client.get('/api/receiver-pause').json()
        assert pause['pause_requested'] is False
        on = client.put('/api/receiver-pause', json={'minutes': 15, 'expected_revision': pause['revision']}, headers={'Origin': ORIGIN}).json()
        assert on['pause_requested'] is True and on['remaining_seconds'] > 0
        assert client.put('/api/receiver-pause', json={'minutes': 0, 'expected_revision': pause['revision']}, headers={'Origin': ORIGIN}).status_code == 409
        assert client.put('/api/receiver-pause', json={'minutes': 0, 'expected_revision': on['revision']}, headers={'Origin': ORIGIN}).json()['pause_requested'] is False


def test_signals_page_lists_dropped_segments_and_plays_samples(setup, tmp_path):
    _, path = setup
    from services.recordings import RecordingStore
    store = RecordingStore(tmp_path/'recordings')
    dropped = store.save(pcm(segment()), 16000, {'status': 'notification', 'notification_score': 0.93})
    store.save(pcm(segment()), 16000, {'status': 'ready'})
    add_sample(tmp_path/'samples', wav(), confirmed_isolated=True)
    app = create_dashboard(path, KEY, samples_dir=tmp_path/'samples', recordings_dir=store.root)
    with TestClient(app, base_url=ORIGIN, client=('127.0.0.1', 1)) as client:
        client.post('/session', json={'token': KEY}, headers={'Origin': ORIGIN})
        items = client.get('/api/notification-filter/dropped').json()['items']
        assert [i['id'] for i in items] == [dropped] and items[0]['score'] == 0.93 and items[0]['reviewed'] is False
        store.review(dropped, '', 'notification', 0)
        result = client.get('/api/notification-filter/dropped').json()
        assert result['items'] == [] and result['unreviewed'] == 0 and result['total'] == 1, "розмічене зникає"
        assert client.get('/api/notification-filter/dropped?reviewed=true').json()['items'][0]['reviewed'] is True
        sample = client.get('/api/samples').json()['items'][0]
        assert sample['created'] > 0
        audio = client.get(f"/api/samples/{sample['id']}/audio")
        assert audio.status_code == 200 and audio.content[:4] == b'RIFF'
        assert client.get('/api/samples/..%2F..%2Fevents.db/audio').status_code == 404
        assert client.get('/api/samples/missing.wav/audio').status_code == 404


def test_editor_quick_add_preserves_dictionary_rows_and_checks_version(local):
    client, _, _ = local
    headers = {'Origin': ORIGIN}
    current = client.get('/api/dictionaries').json()['callsigns']
    seeded = client.put('/api/dictionaries/callsigns', headers=headers, json={
        'entries': current['entries'] + [{'raw_line':'береза: 56', 'enabled':True}],
        'expected_version': current['version'],
    })
    assert seeded.status_code == 200
    hint = client.post('/api/text-check', headers=headers, json={'text':'Береза 57, прийом'}).json()
    entry = next(i['dictionary_entry'] for i in hint['issues'] if 'dictionary_entry' in i)
    current = client.get('/api/dictionaries').json()['callsigns']
    added = client.put('/api/dictionaries/callsigns', headers=headers, json={
        'entries': current['entries'] + [{'raw_line':entry, 'enabled':True}],
        'expected_version': current['version'],
    })
    assert added.status_code == 200
    assert added.json()['entries'][:-1] == current['entries']
    assert added.json()['entries'][-1] == {'raw_line':'береза: 57', 'enabled':True}


def test_editor_quick_add_conflict_keeps_concurrent_dictionary_change(local):
    client, _, _ = local
    headers = {'Origin': ORIGIN}
    old = client.get('/api/dictionaries').json()['callsigns']
    new = client.put('/api/dictionaries/callsigns', headers=headers, json={
        'entries': old['entries'] + [{'raw_line':'ясень: 90', 'enabled':True}],
        'expected_version': old['version'],
    })
    assert new.status_code == 200
    stale = client.put('/api/dictionaries/callsigns', headers=headers, json={
        'entries': old['entries'] + [{'raw_line':'береза: 57', 'enabled':True}],
        'expected_version': old['version'],
    })
    assert stale.status_code == 409
    assert client.get('/api/dictionaries').json()['callsigns'] == new.json()


def test_storage_status_endpoint_requires_auth_and_does_not_create_archive(setup, tmp_path):
    _, path = setup
    directory = tmp_path/'not-created'
    app = create_dashboard(path, KEY, recordings_dir=directory, samples_dir=tmp_path/'samples', diagnostics_path=tmp_path/'diagnostics.json')
    with TestClient(app, base_url=ORIGIN, client=('127.0.0.1',1)) as client:
        assert client.get('/api/storage').status_code == 401
        client.post('/session', json={'token':KEY}, headers={'Origin':ORIGIN})
        result = client.get('/api/storage')
        assert result.status_code == 200
        assert result.json()['audio']['available'] is False
        assert result.json()['backups']['keep'] == 7
        assert not directory.exists()


def test_list_is_evaluated_on_reviewed_records_without_saving(setup, tmp_path):
    """Кнопка «Перевірити на моїх еталонах»: порівняння до/після, нічого не зберігається."""
    _, path = setup
    import sqlite3
    from services.recordings import RecordingStore
    store = RecordingStore(tmp_path/'recordings')
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO dictionary_entries (dictionary_type, line_number, raw_line) VALUES ('callsigns', 2, 'Ліна: 63')")
    rec = store.save(pcm(segment()), 16000, {'status': 'ready', 'raw_text': 'ліна 63, приняте, прийом', 'before_llm': 'ліна 63, приняте, прийом'})
    store.review(rec, 'Ліна 63, прийнято, прийом', 'speech', 0)
    app = create_dashboard(path, KEY, samples_dir=tmp_path/'samples', recordings_dir=store.root)
    h = {'Origin': ORIGIN}
    with TestClient(app, base_url=ORIGIN, client=('127.0.0.1', 1)) as client:
        client.post('/session', json={'token': KEY}, headers=h)
        revision = client.get('/api/text-lists').json()['revision']
        r = client.put('/api/text-lists/phrase_patterns/evaluate', headers=h,
                       json={'entries': [{'raw_line': '{П}, прийнято, прийом', 'enabled': True}]}).json()
        assert r['records'] == 1 and r['better'] == 1 and r['worse'] == 0
        assert r['examples'][0]['after'] == 'ліна 63, прийнято, прийом'
        assert client.get('/api/text-lists').json()['revision'] == revision, "перевірка нічого не зберегла"
        bad = client.put('/api/text-lists/phrase_patterns/evaluate', headers=h, json={'entries': [{'raw_line': '{Х}', 'enabled': True}]})
        assert bad.status_code == 422
        assert client.put('/api/text-lists/blacklist/evaluate', headers=h, json={'entries': []}).status_code == 404


def test_stale_events_are_updated_from_references_on_request(setup, tmp_path):
    _, path = setup
    import sqlite3
    from services.event_store import save_event
    from services.recordings import RecordingStore
    event = save_event("віра 68", "", "", "", "вира 68 прийом", path)
    store = RecordingStore(tmp_path/'recordings')
    rid = store.save(pcm(segment()), 16000, {'status': 'ready', 'fix_id': f'db:{event}'})
    store.review(rid, 'Віра 68, прийом', 'speech', 0)        # еталон є, але подія ще з текстом Whisper
    app = create_dashboard(path, KEY, samples_dir=tmp_path/'samples', recordings_dir=store.root)
    with TestClient(app, base_url=ORIGIN, client=('127.0.0.1', 1)) as client:
        client.post('/session', json={'token': KEY}, headers={'Origin': ORIGIN})
        assert client.post('/api/suggestions/sync-events').status_code == 403, "без Origin панелі"
        result = client.post('/api/suggestions/sync-events', headers={'Origin': ORIGIN}).json()
        assert result['updated'] == 1
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT text FROM radio_events WHERE id=?", (event,)).fetchone()[0] == 'Віра 68, прийом'
        assert client.post('/api/suggestions/sync-events', headers={'Origin': ORIGIN}).json()['updated'] == 0


def test_done_button_hides_a_suggestion_kind_and_can_be_undone(local):
    from services.recordings import RecordingStore
    from services.suggestions import refresh
    client, path, tmp_path = local
    store = RecordingStore(tmp_path / "recordings")
    for label in ("room", "noise"):
        rid = store.save(pcm(segment()), 16000, {"status": "ready", "raw_text": "та па", "before_llm": "та па"})
        store.review(rid, "", label, 0)
    refresh(path, store)
    assert [g["kind"] for g in client.get("/api/attention").json()["suggestions"]] == ["noise"]
    assert client.post("/api/suggestions/dismiss", headers={"Origin": ORIGIN}, json={"kind": "bogus"}).status_code == 422
    assert client.post("/api/suggestions/dismiss", headers={"Origin": ORIGIN}, json={"kind": "noise"}).json() == {"dismissed": 1}
    data = client.get("/api/attention").json()
    assert data["suggestions"] == [] and data["suggestions_hidden"] == 1
    assert client.post("/api/suggestions/restore", headers={"Origin": ORIGIN}).json() == {"restored": 1}
    assert client.get("/api/attention").json()["suggestions_hidden"] == 0


def test_archive_card_deletes_a_sent_phrase_for_every_recipient(local):
    import time
    from services.event_store import save_event
    from services.recordings import RecordingStore
    from services.telegram_sync import remember_delivery
    client, path, tmp_path = local
    store = RecordingStore(tmp_path / "recordings")
    fix = f'db:{save_event("", "", "", "", "Вишня 12, прийом", path)}'
    for chat in (1001, 2002):
        remember_delivery(path, fix, {"message_id": chat + 1, "chat": {"id": chat}}, "voice")
    fresh = store.save(pcm(segment()), 16000, {"status": "ready", "fix_id": fix})
    old = store.save(pcm(segment()), 16000, {"status": "ready", "fix_id": fix})
    with store.connect() as db:
        db.execute("UPDATE recordings SET created=? WHERE id=?", (time.time() - 49 * 3600, old))
    items = {i["id"]: i for i in client.get("/api/recordings?view=telegram").json()["items"]}
    assert items[fresh]["telegram"]["copies"] == 2 and items[fresh]["telegram"]["deletable"]
    assert not items[old]["telegram"]["deletable"]
    h = {"Origin": ORIGIN}
    assert client.post(f"/api/recordings/{old}/telegram-delete", headers=h).status_code == 422
    result = client.post(f"/api/recordings/{fresh}/telegram-delete", headers=h).json()
    assert result["queued"] == 2 and result["pending"] == 2 and result["hidden"]


def test_saved_panel_changes_are_journaled_but_checks_are_not(local):
    from services import user_activity
    client, path, _ = local
    h = {"Origin": ORIGIN}
    lists = client.get("/api/text-lists").json()
    client.put("/api/text-lists/short_garbage/check", headers=h, json={"entries": [{"raw_line": "пау пау", "enabled": True}]})
    saved = client.put("/api/text-lists/short_garbage", headers=h,
                       json={"entries": [{"raw_line": "пау пау", "enabled": True}], "expected_revision": lists["revision"]})
    assert saved.status_code == 200
    admin = client.get("/api/users").json()["items"][0]["user_id"]
    actions = [(i["action"], i["target"]) for i in user_activity.overview(path, admin)["items"]]
    assert actions.count(("panel", "Фільтри тексту: short_garbage")) == 1
