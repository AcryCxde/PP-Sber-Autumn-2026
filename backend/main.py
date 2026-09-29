import json
import subprocess
from pathlib import Path
from fastapi import FastAPI, WebSocket

from parser import parse_event

app = FastAPI()


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def start_claude(prompt: str):
    process = subprocess.Popen(
        [
            "claude",
            "--print",
            "--verbose",
            "--output-format",
            "stream-json",
        ],
        cwd=PROJECT_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    process.stdin.write(prompt)
    process.stdin.close()

    return process


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    prompt = await websocket.receive_text()

    process = start_claude(prompt)

    for line in process.stdout:
        event = parse_event(line)

        if event is None:
            continue

        await websocket.send_text(
            json.dumps(
                event,
                ensure_ascii=False,
            )
        )

    process.wait()

    await websocket.close()
