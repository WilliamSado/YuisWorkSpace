import os
import uvicorn

from .main import app, config

if __name__ == "__main__":
    server = config.get("server", {})
    uvicorn.run(app, host=server.get("host", "0.0.0.0"), port=int(server.get("port", 8080)))

