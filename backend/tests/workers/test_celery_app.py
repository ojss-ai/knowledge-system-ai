from app.workers.celery_app import celery_app, task_session


def test_celery_app_name():
    assert celery_app.main == "kb"


def test_celery_config():
    conf = celery_app.conf
    assert conf.task_acks_late is True
    assert conf.task_serializer == "json"
    assert conf.result_serializer == "json"


def test_task_session_is_context_manager():
    """task_session() must produce an async context manager."""
    cm = task_session()
    assert hasattr(cm, "__aenter__")
    assert hasattr(cm, "__aexit__")
