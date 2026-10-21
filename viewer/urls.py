from django.urls import path
from . import views

urlpatterns = [
    path("drive/<str:route_id>/toggle_preserve/", views.toggle_preserve, name="toggle_preserve"),
    path("", views.drive_list, name="drive_list"),
    path("storage/", views.retention_page, name="retention_page"),
    path("drive/<str:route_id>/", views.drive_detail, name="drive_detail"),
    path("drive/<str:route_id>/track.json", views.drive_track, name="drive_track"),
    path("drive/<str:route_id>/summary.json", views.drive_summary, name="drive_summary"),
    path("drive/<str:route_id>/diagnostics.json", views.drive_diagnostics, name="drive_diagnostics"),
    path("drive/<str:route_id>/thumbnail.png", views.drive_thumbnail, name="drive_thumbnail"),
    path("drive/<str:route_id>/delete/", views.delete_drive, name="delete_drive"),
    path("drive/<str:route_id>/undo_delete/", views.undo_delete, name="undo_delete"),
    path("drive/<str:route_id>/recreate_stitched/", views.recreate_stitched, name="recreate_stitched"),
    path('drive/<str:route_id>/segments/', views.segment_list, name='segment_list'),
    path('drive/<str:route_id>/log/<str:segment_num>/', views.log_detail, name='log_detail'),
]
