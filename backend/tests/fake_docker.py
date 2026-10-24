"""Минимальная подмена docker.DockerClient для тестов ContainerManager."""

import io
import tarfile

from docker.errors import NotFound


def unpack(archive: bytes) -> dict[str, tuple[bytes, int, int]]:
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        return {m.name: (tar.extractfile(m).read(), m.mode, m.uid) for m in tar.getmembers() if m.isfile()}


class FakeContainer:
    def __init__(self, client, image, name=None, labels=None, **kwargs):
        self.client, self.image, self.name = client, image, name
        self.labels = labels or {}
        self.kwargs = kwargs
        self.archives: list[tuple[str, bytes]] = []
        self.files: dict[str, bytes] = {}
        self.attrs = {"State": {"Status": "created", "ExitCode": 0,
                                "StartedAt": "0001-01-01T00:00:00Z",
                                "FinishedAt": "0001-01-01T00:00:00Z"},
                      "RestartCount": 0}
        self.removed = False

    def start(self):
        self.attrs["State"].update(Status="running", StartedAt="2026-10-25T10:00:00.5Z")
        self.client.started.append(self.name)

    def stop(self, timeout=None):
        self.attrs["State"].update(Status="exited", ExitCode=0, FinishedAt="2020-01-01T00:00:00Z")
        self.client.stopped.append(self.name)

    def exit(self, code):
        self.attrs["State"].update(Status="exited", ExitCode=code, FinishedAt="2020-01-01T00:00:00Z")

    def reload(self):
        pass

    def put_archive(self, path, data):
        self.archives.append((path, data))
        return True

    def get_archive(self, path):
        data = self.files[path]
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            info = tarfile.TarInfo(path.rsplit("/", 1)[-1])
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        return [buffer.getvalue()], {"size": len(data)}

    def remove(self, force=False):
        self.removed = True
        if self.name is not None:
            self.client.removed_containers.append(self.name)
            self.client.containers._items.pop(self.name, None)


class FakeContainers:
    def __init__(self, client):
        self.client = client
        self._items: dict[str, FakeContainer] = {}
        self.helpers: list[FakeContainer] = []

    def create(self, image, name=None, **kwargs):
        container = FakeContainer(self.client, image, name=name, **kwargs)
        if name is None:
            self.helpers.append(container)
        else:
            self._items[name] = container
        return container

    def get(self, name):
        try:
            return self._items[name]
        except KeyError:
            raise NotFound(name) from None

    def list(self, all=False, filters=None):
        key, _, value = (filters or {}).get("label", "=").partition("=")
        return [c for c in self._items.values() if not key or c.labels.get(key) == value]


class FakeVolume:
    def __init__(self, client, name, labels=None):
        self.client, self.name = client, name
        self.attrs = {"Labels": labels or {}}

    def remove(self, force=False):
        self.client.volumes._items.pop(self.name, None)


class FakeVolumes:
    def __init__(self, client):
        self.client = client
        self._items: dict[str, FakeVolume] = {}

    def create(self, name, labels=None):
        self.client.created_volumes.append(name)
        self._items[name] = FakeVolume(self.client, name, labels)
        return self._items[name]

    def list(self, filters=None):
        key, _, value = (filters or {}).get("label", "=").partition("=")
        return [v for v in self._items.values() if not key or v.attrs["Labels"].get(key) == value]

    def get(self, name):
        try:
            return self._items[name]
        except KeyError:
            raise NotFound(name) from None


class FakeDocker:
    def __init__(self):
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.removed_containers: list[str] = []
        self.created_volumes: list[str] = []
        self.containers = FakeContainers(self)
        self.volumes = FakeVolumes(self)
