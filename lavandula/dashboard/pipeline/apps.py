from django.apps import AppConfig


class PipelineConfig(AppConfig):
    name = "pipeline"

    def ready(self):
        try:
            from .process_manager import cleanup_stale
            cleanup_stale()
        except Exception:
            pass
