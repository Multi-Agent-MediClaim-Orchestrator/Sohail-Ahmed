"""Render Redis ACL (and, via infra/keycloak/render_realms.py, Keycloak realms) from .env."""

import pathlib
import string

env = {}
for ln in pathlib.Path(".env").read_text().splitlines():
    if "=" in ln and not ln.startswith("#"):
        k, v = ln.split("=", 1)
        env[k] = v
tpl = pathlib.Path("infra/redis/users.acl.tpl").read_text()
out = pathlib.Path("infra/redis/rendered")
out.mkdir(exist_ok=True)
(out / "users.acl").write_text(string.Template(tpl).substitute(env))
print("rendered redis ACL")
