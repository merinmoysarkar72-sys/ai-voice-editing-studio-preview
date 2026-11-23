import json
import math
import struct
import wave
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.storage import project_dir, read_project, write_project
from backend.app.audio import probe_audio


def make_wav(path, duration=2.0, rate=16000, freq=220):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(rate)
        frames = [int(5000 * math.sin(2 * math.pi * freq * i / rate)) for i in range(int(duration * rate))]
        out.writeframes(b"".join(struct.pack("<h", x) for x in frames))


def test_multi_user_project_isolation(monkeypatch):
    monkeypatch.setenv("AVES_USERS_JSON", json.dumps({"token-a-test": "owner-a", "token-b-test": "owner-b"}))
    a, b = TestClient(app), TestClient(app)
    assert a.post("/api/auth", json={"token": "token-a-test"}).status_code == 200
    assert b.post("/api/auth", json={"token": "token-b-test"}).status_code == 200
    created = a.post("/api/projects", json={"name": "private A"})
    project_id = created.json()["id"]
    assert created.json()["owner_id"] == "owner-a"
    assert len(a.get("/api/projects").json()) == 1
    assert b.get("/api/projects").json() == []
    assert b.get(f"/api/projects/{project_id}").status_code == 404
    assert b.delete(f"/api/projects/{project_id}").status_code == 404
    assert a.delete(f"/api/projects/{project_id}").status_code == 200


def test_waveform_timeline_and_real_export_preserve_original(tmp_path):
    client = TestClient(app)
    created = client.post("/api/projects", json={"name": "timeline integration", "script_text": "Hello world."})
    project_id = created.json()["id"]
    folder = project_dir(project_id)
    source = folder / "source.wav"
    make_wav(source)
    project = read_project(project_id)
    project.update({"audio_filename": source.name, "audio_original_name": source.name,
                    "audio_duration": 2.0, "audio_size": source.stat().st_size,
                    "analysis": {"sentences": [{"index": 0, "script": "Hello world.", "status": "matched",
                    "include": True, "start": .1, "end": 1.8,
                    "audio_ranges": [{"start": .1, "end": 1.8}]}]}})
    write_project(project)
    waveform = client.get(f"/api/projects/{project_id}/waveform?points=200")
    assert waveform.status_code == 200 and waveform.json()["peaks"]
    saved = client.put(f"/api/projects/{project_id}/timeline", json={"ranges": [{"start": .5, "end": 1.0}], "splits": [.75]})
    assert saved.status_code == 200 and saved.json()["splits"] == [.75]
    exported = client.post(f"/api/projects/{project_id}/export", json={"format": "wav", "noise_reduction": "off",
        "pause_mode": "natural", "normalize": False, "sample_rate": "22050", "channels": 1})
    assert exported.status_code == 200
    meta = probe_audio(folder / exported.json()["filename"])
    assert .55 <= meta["duration"] <= .8
    download = client.get(f"/api/projects/{project_id}/export/download")
    assert download.status_code == 200 and len(download.content) > 1000
    assert source.exists() and source.stat().st_size == 64044
    client.delete(f"/api/projects/{project_id}")


def test_persisted_job_claim_is_exclusive():
    from backend.app import jobs
    from uuid import uuid4
    job_id = str(uuid4())
    jobs.create(job_id, str(uuid4()), "test-owner")
    assert jobs.claim(job_id) is True
    assert jobs.claim(job_id) is False
    jobs.update(job_id, status="failed", stage="test complete", error="")
    assert jobs.get(job_id, "test-owner")["status"] == "failed"


def test_segment_replacement_creates_working_copy_without_touching_original():
    client = TestClient(app)
    created = client.post("/api/projects", json={"name": "replacement integration", "script_text": "Hello world."})
    project_id = created.json()["id"]
    folder = project_dir(project_id)
    source = folder / "original.wav"
    replacement = folder / "replacement-input.wav"
    make_wav(source, 2.0)
    make_wav(replacement, 1.0)
    project = read_project(project_id)
    project.update({"audio_filename": source.name, "audio_original_name": source.name,
                    "audio_duration": 2.0, "audio_size": source.stat().st_size})
    write_project(project)
    with replacement.open("rb") as stream:
        response = client.post(f"/api/projects/{project_id}/replace-segment",
            data={"start": "0.5", "end": "1.5", "take_start": "0"},
            files={"file": ("take.wav", stream, "audio/wav")})
    assert response.status_code == 200, response.text
    project = read_project(project_id)
    working = folder / project["working_audio_filename"]
    assert working.exists() and abs(probe_audio(working)["duration"] - 2.0) < .05
    assert source.exists() and source.stat().st_size == 64044
    assert client.get(f"/api/projects/{project_id}/working-audio").status_code == 200
    client.delete(f"/api/projects/{project_id}")


def test_production_api_refuses_unauthenticated_access(monkeypatch):
    monkeypatch.setenv("AVES_ENV", "production")
    monkeypatch.delenv("AVES_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("AVES_USERS_JSON", raising=False)
    client = TestClient(app)
    assert client.get('/api/health').status_code == 200
    assert client.get('/api/projects').status_code == 503
    assert client.post('/api/auth',json={'token':'x'}).status_code == 503


def test_empty_timeline_does_not_fall_back_to_full_source():
    from backend.app.audio import export_audio
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as tmp:
        tmp=__import__('pathlib').Path(tmp)
        source=tmp/'source.wav'; output=tmp/'out.wav'; make_wav(source,2.0)
        rows=[{"index":0,"start":0.0,"end":1.0,"include":True,"audio_ranges":[{"start":0.0,"end":1.0}]}]
        try:
            export_audio(source,output,rows,{"timeline_ranges":[],"format":"wav","noise_reduction":"off"})
        except ValueError as exc:
            assert 'did not produce usable audio' in str(exc) or 'No approved' in str(exc)
        else:
            raise AssertionError('An empty timeline must not export the original source.')


def test_mp3_export_and_noise_reduction_filter(tmp_path):
    from backend.app.audio import export_audio
    source=tmp_path/'source.wav'; output=tmp_path/'final.mp3'; make_wav(source,2.0)
    rows=[{"index":0,"start":0.1,"end":1.8,"include":True,"audio_ranges":[{"start":0.1,"end":1.8}]}]
    result=export_audio(source,output,rows,{"format":"mp3","bitrate":"192k","sample_rate":"44100","channels":1,
        "noise_reduction":"light","pause_mode":"balanced","normalize":True,"timeline_ranges":None})
    assert output.exists() and result['noise_reduction']=='light' and result['normalized'] is True
    assert probe_audio(output)['codec']=='mp3'


def test_working_audio_undo_redo_restores_prior_audio_version():
    client=TestClient(app)
    created=client.post('/api/projects',json={'name':'undo redo test'}).json();pid=created['id']
    folder=project_dir(pid);original=folder/'original.wav';working=folder/'working.wav'
    make_wav(original,2.0);make_wav(working,2.0, freq=440)
    project=read_project(pid);project.update({'audio_filename':original.name,'audio_duration':2.0,'audio_size':original.stat().st_size,
        'working_audio_filename':working.name,'working_audio_history':[None],'working_audio_redo':[]});write_project(project)
    undo=client.post(f'/api/projects/{pid}/working-audio/undo');assert undo.status_code==200 and undo.json()['restored_original'] is True
    redo=client.post(f'/api/projects/{pid}/working-audio/redo');assert redo.status_code==200 and redo.json()['filename']==working.name
    assert original.exists() and working.exists()
    client.delete(f'/api/projects/{pid}')


def test_per_take_signal_quality_returns_real_measurements(tmp_path):
    from backend.app.audio import analyze_take_signal_quality
    source=tmp_path/'quality.wav';make_wav(source,2.0)
    result=analyze_take_signal_quality(source,[{'start':.2,'end':1.2}])
    metric=result['0.200:1.200']
    assert -80 < metric['rms_db'] < 0
    assert metric['peak_db'] <= 0.1
    assert 0 <= metric['clipping_ratio'] <= 1
    assert 0 <= metric['signal_score'] <= 1


def test_persistent_background_analysis_job_completes_with_real_signal_analysis(monkeypatch):
    import time
    import backend.app.main as main
    client=TestClient(app)
    created=client.post('/api/projects',json={'name':'background pipeline','script_text':'Hello world.'}).json()
    pid=created['id'];folder=project_dir(pid);source=folder/'source.wav';make_wav(source,2.0)
    project=read_project(pid);project.update({'audio_filename':source.name,'audio_original_name':source.name,'audio_duration':2.0,'audio_size':source.stat().st_size});write_project(project)
    def fake_transcribe(path, language_hint='auto'):
        assert language_hint=='en'
        return ([{'start':.1,'end':.8,'text':'Hello world.','avg_logprob':-.1,'words':[
            {'word':' Hello','start':.1,'end':.4,'probability':.95},{'word':' world.','start':.4,'end':.8,'probability':.96}]}], 'en')
    monkeypatch.setattr(main,'transcribe',fake_transcribe)
    queued=client.post(f'/api/projects/{pid}/analyze',json={'language':'en'})
    assert queued.status_code==200 and queued.json()['job_id']
    job_id=queued.json()['job_id']
    for _ in range(100):
        job=client.get(f'/api/jobs/{job_id}').json()
        if job['status'] in {'completed','failed'}:break
        time.sleep(.05)
    assert job['status']=='completed',job
    report=client.get(f'/api/projects/{pid}/analysis').json()['analysis']
    assert report['detected_language']=='en'
    assert report['audio_quality']['mean_volume_db'] is not None
    assert report['take_signal_metrics'] >= 1
    client.delete(f'/api/projects/{pid}')


def test_project_delete_removes_job_registry_rows():
    from backend.app import jobs
    from uuid import uuid4
    client=TestClient(app);created=client.post('/api/projects',json={'name':'delete job rows'}).json();pid=created['id']
    job_id=str(uuid4());jobs.create(job_id,pid,'local')
    assert jobs.get(job_id,'local') is not None
    deleted=client.delete(f'/api/projects/{pid}')
    assert deleted.status_code==200
    assert jobs.get(job_id,'local') is None


def test_manual_take_selection_updates_export_timestamps():
    client=TestClient(app);created=client.post('/api/projects',json={'name':'manual take selection'}).json();pid=created['id']
    project=read_project(pid)
    project['analysis']={'sentences':[{'index':0,'script':'Hello world.','status':'review','include':False,'start':.1,'end':.5,
      'audio_ranges':[{'start':.1,'end':.5}], 'take_candidates':[{'start':1.0,'end':1.8,'complete':True,'confidence':.94,'coverage':1.0,'recognized':'Hello world.'}]}]}
    write_project(project)
    response=client.patch(f'/api/projects/{pid}/sentences/0',json={'include':True,'take_start':1.0,'take_end':1.8})
    assert response.status_code==200,response.text
    updated=read_project(pid)['analysis']['sentences'][0]
    assert updated['include'] is True and updated['start']==1.0 and updated['end']==1.8
    client.delete(f'/api/projects/{pid}')
