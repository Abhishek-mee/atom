import logging
import os

import uvicorn
from api.routes import app

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("RELOAD", "false").lower() in ("1", "true", "yes"),
    )
