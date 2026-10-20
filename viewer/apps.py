import os
import sys

from django.apps import AppConfig


class ViewerConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'viewer'

    def ready(self):
        from . import retention
        if retention.should_start_enforcer(os.environ, sys.argv):
            retention.start_enforcer(self._enforce_retention)

    @staticmethod
    def _enforce_retention():
        from . import retention, views
        retention.enforce_once(
            views.RAW_DIR, views.STITCHED_DIR, views.METADATA_DIR,
            views.get_route_start_time, views.load_preserved_routes,
        )
