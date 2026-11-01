"""Run Phoenix without its unnecessary wildcard-bound OTLP/gRPC listener."""

from __future__ import annotations

import inspect
from typing import Any


def main() -> None:
    from phoenix.server.grpc_server import GrpcServer

    original = GrpcServer.__init__
    if "disabled" not in inspect.signature(original).parameters:
        raise RuntimeError("Unsupported Phoenix gRPC configuration; refusing to start")

    def local_only(self: Any, *args: Any, **kwargs: Any) -> None:
        if "disabled" in kwargs:
            kwargs["disabled"] = True
        else:
            raise RuntimeError("Unexpected Phoenix gRPC initialization; refusing to start")
        original(self, *args, **kwargs)

    GrpcServer.__init__ = local_only
    from phoenix.server.main import main as phoenix_main

    phoenix_main()


if __name__ == "__main__":
    main()
