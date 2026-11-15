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
    app=create_dashboard(path,KEY,samples_dir=tmp_path/"samples",diagnostics_path=tmp_path/"diagnostics.json")
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
