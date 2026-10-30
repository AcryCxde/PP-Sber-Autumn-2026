from backend.tests.databases import drop_created_databases


def pytest_sessionfinish(session, exitstatus) -> None:
    del session, exitstatus
    drop_created_databases()
