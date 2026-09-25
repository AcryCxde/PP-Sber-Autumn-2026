"""Вырезание значений секретов из всего, что уходит в журнал и наружу."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Self, final

from ctrunner.protocol import JsonValue

MASK: Final = "***"
# Короткое значение совпадёт с обычным текстом и испортит вывод, не защитив ничего.
MIN_SECRET_LEN: Final = 8


@final
@dataclass(frozen=True, slots=True)
class Redactor:
    secrets: tuple[str, ...]

    def __post_init__(self) -> None:
        if any(len(s) < MIN_SECRET_LEN for s in self.secrets):
            raise ValueError("secret too short to redact safely")

    @classmethod
    def of(cls, secrets: Iterable[str]) -> Self:
        # Длинные первыми: иначе секрет, содержащий другой, останется частично виден.
        return cls(tuple(sorted(set(secrets), key=len, reverse=True)))

    def text(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, MASK)
        return value

    def apply(self, value: JsonValue) -> JsonValue:
        match value:
            case str():
                return self.text(value)
            case list():
                return [self.apply(item) for item in value]
            case dict():
                return {self.text(k): self.apply(v) for k, v in value.items()}
            case bool() | int() | float() | None:
                return value
