"""
Transport ABC for Datecs ISL — same shape as the PM driver's transport
(serial / tcp). Different driver, different package, but identical
contract: open / close / write / read / read_until.
"""

from abc import ABC, abstractmethod


class TransportError(Exception):
    pass


class TransportTimeout(TransportError):
    pass


class TransportClosed(TransportError):
    pass


class Transport(ABC):
    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def is_open(self) -> bool: ...

    @abstractmethod
    def write(self, data: bytes) -> None: ...

    @abstractmethod
    def read(self, n: int, timeout: float) -> bytes: ...

    @abstractmethod
    def read_until(self, terminator: bytes, max_bytes: int, timeout: float) -> bytes: ...

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
