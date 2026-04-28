class PipelineRouter:
    """Route unmanaged pipeline models to the read-only pipeline DB alias."""

    def db_for_read(self, model, **hints):
        if not model._meta.managed:
            return "pipeline"
        return "default"

    def db_for_write(self, model, **hints):
        if not model._meta.managed:
            raise RuntimeError(
                f"Write blocked: {model._meta.label} is an unmanaged model; "
                "lava_corpus schema is owned by lavandula/migrations/rds/."
            )
        return "default"

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if db == "pipeline":
            return False
        return True
