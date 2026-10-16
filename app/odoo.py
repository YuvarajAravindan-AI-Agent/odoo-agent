"""Odoo XML-RPC client. One short-lived proxy per call (xmlrpc proxies are not thread-safe)."""
import xmlrpc.client


class Odoo:
    def __init__(self, url: str, db: str, user: str, password: str):
        self.url, self.db, self.user, self.password = url, db, user, password
        self._uid: int | None = None

    def uid(self) -> int:
        if self._uid is None:
            common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
            self._uid = common.authenticate(self.db, self.user, self.password, {})
            if not self._uid:
                raise RuntimeError("Odoo authentication failed")
        return self._uid

    def call(self, model: str, method: str, args: list, kwargs: dict | None = None):
        models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", allow_none=True)
        return models.execute_kw(self.db, self.uid(), self.password, model, method, args, kwargs or {})

    def search_read(self, model: str, domain: list, fields: list[str], limit: int = 20, order: str = "") -> list[dict]:
        kw = {"fields": fields, "limit": limit}
        if order:
            kw["order"] = order
        return self.call(model, "search_read", [domain], kw)

    def ping(self) -> tuple[bool, str]:
        try:
            self.uid()
            return True, "odoo reachable"
        except Exception as e:  # noqa: BLE001
            return False, str(e)
