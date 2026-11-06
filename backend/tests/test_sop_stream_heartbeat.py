from app.api import skills
from app.skills.stream_jobs import SkillStreamJobStore


def test_idle_generation_stream_keeps_connection_open_without_fake_job_events(monkeypatch):
    store = SkillStreamJobStore()
    job = store.create('generate', 'tenant', 'owner')
    store.start(job.id)
    clock = [0]
    monkeypatch.setattr(skills, 'stream_jobs', store)
    monkeypatch.setattr(skills, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(skills, 'sleep', lambda _duration: clock.__setitem__(0, clock[0] + 16))
    stream = skills._stream_skill_job(job.id)
    assert 'job_attached' in next(stream)
    assert next(stream) == ': keepalive\n\n'
    assert store.get(job.id).events == []
    store.complete(job.id)
    assert 'job_complete' in next(stream)
