"""Repository-level build wiring; the frontend image has no Compose file."""

import re
from pathlib import Path


def test_frontend_compose_passes_explicit_default_off_build_flag():
    compose = (Path(__file__).parents[2] / "docker-compose.yml").read_text()
    frontend = re.search(r"(?ms)^  frontend:\n(?P<body>.*?)(?=^  [\w-]+:|^\S|\Z)", compose)
    assert frontend is not None
    build = re.search(r"(?ms)^    build:\n(?P<body>.*?)(?=^    \S|\Z)", frontend["body"])
    assert build is not None
    assert re.search(r"(?m)^      context: \.$", build["body"])
    assert re.search(r"(?m)^      dockerfile: frontend/Dockerfile$", build["body"])
    assert re.search(
        r"(?m)^      args:\n        VITE_ADMIN_SHELL_ENABLED: "
        r"\$\{VITE_ADMIN_SHELL_ENABLED:-false\}$",
        build["body"],
    )
