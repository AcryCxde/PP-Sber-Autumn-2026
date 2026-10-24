from ctrunner.main import LiveForks


class FakeSession:
    def __init__(self, text: str) -> None:
        self.text = text

    def fork_questions(self, fork_id: str) -> tuple[str, ...] | None:
        return (self.text,) if fork_id == "f1" else None


def test_live_forks_without_session_knows_nothing() -> None:
    assert LiveForks().questions("f1") is None


def test_live_forks_follows_current_session() -> None:
    live = LiveForks()
    live.session = FakeSession("Q?")  # type: ignore[assignment]
    assert live.questions("f1") == ("Q?",)
    assert live.questions("f2") is None
    live.session = FakeSession("Z?")  # type: ignore[assignment]  # фолбэк resume → fresh
    assert live.questions("f1") == ("Z?",)
