from django.urls import path

from . import views


urlpatterns = [
    path('workouts/upload/', views.upload_workouts, name='workout-upload'),
    path('workouts/', views.download_workouts, name='workout-download'),
    path(
        'workouts/<int:workout_id>/detail/',
        views.download_workout_detail,
        name='workout-detail',
    ),
    path(
        'workouts/<int:workout_id>/h3/',
        views.download_workout_h3,
        name='workout-h3',
    ),
]
