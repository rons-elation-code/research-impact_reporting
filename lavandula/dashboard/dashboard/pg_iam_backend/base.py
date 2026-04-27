"""PostgreSQL backend that injects a fresh IAM token on every new connection."""
from django.db.backends.postgresql import base as pg_base


class DatabaseWrapper(pg_base.DatabaseWrapper):

    def get_connection_params(self):
        params = super().get_connection_params()
        iam_token_manager = self.settings_dict.get("IAM_TOKEN_MANAGER")
        if iam_token_manager is not None:
            params["password"] = iam_token_manager.token()
        return params
