"""The local console and RPC share configuration, safety rules and task evidence."""
from .administration import Administration
from .operation_templates import OperationTemplates, validate_templates


class ConsoleManagement:
    def __init__(self, manager):
        self.administration = Administration(manager)
        self.templates = OperationTemplates(manager)
        manager.config.extra_validators["operation_templates"] = validate_templates
        manager.config.mutation_guard = self.administration.guard_target_change
        def audit(action, name, before, after, revision):
            import uuid
            from .core import REQUEST_SOURCE
            manager.store.put({"id": uuid.uuid4().hex, "kind": "console_audit", "action": action,
                               "name": name, "state": "recorded", "source": REQUEST_SOURCE.get(),
                               "revision": revision, "fields": [key for key in sorted(set(before or {}) | set(after or {}))
                                                                  if (before or {}).get(key) != (after or {}).get(key)]})
        manager.config.mutation_audit = audit

    async def call(self, action, params=None):
        from .errors import RemoteError
        if not isinstance(action, str) or (params is not None and not isinstance(params, dict)):
            raise RemoteError("invalid_params", "An action and parameter object are required")
        if action.startswith("template."):
            return await self.templates.call(action, params or {})
        result = await self.administration.call(action, params or {})
        if action == "state":
            result["operation_templates"] = self.templates.list()
        return result

    def close(self):
        close = getattr(self.administration, "close", None)
        if close:
            close()
