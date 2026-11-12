from __future__ import annotations

from fastapi.templating import Jinja2Templates

from app.core.config import APP_ROOT
from app.core.system_info import get_system_resources

templates = Jinja2Templates(directory=str(APP_ROOT / "app" / "modules"))
templates.env.globals["get_system_resources"] = get_system_resources
