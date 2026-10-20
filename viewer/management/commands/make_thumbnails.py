from django.core.management.base import BaseCommand

from viewer import thumbnails, views


class Command(BaseCommand):
    help = "Make the route map thumbnails (all routes, or the ones named)."

    def add_arguments(self, parser):
        parser.add_argument("routes", nargs="*", help="route ids; default is every route")
        parser.add_argument("--force", action="store_true", help="redo thumbnails that are up to date")

    def handle(self, *args, routes, force, **options):
        if not routes:
            routes = sorted({views.normalize_route_id(d.name) for d in views.RAW_DIR.iterdir() if d.is_dir()})
        counts = {}
        for route_id in routes:
            result = thumbnails.make_thumbnail(
                route_id, views.RAW_DIR, views.METADATA_DIR, force=force, stitched_dir=views.STITCHED_DIR
            )
            counts[result] = counts.get(result, 0) + 1
            self.stdout.write(f"{route_id}: {result}")
        self.stdout.write(", ".join(f"{n} {name}" for name, n in sorted(counts.items())) or "no routes")
