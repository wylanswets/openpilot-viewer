import os
import sys

from django.apps import AppConfig


class ViewerConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'viewer'

    def ready(self):
        from . import prepare, retention
        if retention.should_start_enforcer(os.environ, sys.argv):
            retention.start_enforcer(self._enforce_retention)
        if prepare.should_start_worker(os.environ, sys.argv):
            prepare.start_worker(self._prepare_drives)

    @staticmethod
    def _prepare_drives():
        from . import prepare, views
        prepare.run_pass(views.RAW_DIR, views.METADATA_DIR, views.STITCHED_DIR)

    @staticmethod
    def _enforce_retention():
        from . import retention, views
        retention.enforce_once(
            views.RAW_DIR, views.STITCHED_DIR, views.METADATA_DIR,
            views.get_route_start_time, views.load_preserved_routes,
        )
