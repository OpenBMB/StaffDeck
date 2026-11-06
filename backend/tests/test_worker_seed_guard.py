def test_background_worker_respects_deployment_seed_disable(monkeypatch):
    from types import SimpleNamespace
    import app.scheduled_tasks.worker as worker
    calls = []
    monkeypatch.setattr(worker, 'get_settings', lambda: SimpleNamespace(demo_seed_enabled=False))
    monkeypatch.setattr(worker, 'init_db', lambda: None)
    monkeypatch.setattr(worker, 'seed_demo_data', lambda db: calls.append('seed'))
    monkeypatch.setattr(worker, '_run_due_tasks', lambda: calls.append('scan'))
    monkeypatch.setattr(worker, '_stopped', False)
    worker.run_worker(once=True)
    assert calls == ['scan']
