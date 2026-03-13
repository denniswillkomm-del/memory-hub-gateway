from __future__ import annotations

import uvicorn

from gateway.app import create_app


def main() -> None:
    app = create_app()
    uvicorn.run(app, host="127.0.0.1", port=8080)


if __name__ == "__main__":
    main()
