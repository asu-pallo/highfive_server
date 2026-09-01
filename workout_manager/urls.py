from django.urls import path

from . import views


urlpatterns = [
    path(
        'workouts/upload/prepare/',
        views.prepare_workout_upload,
        name='workout-upload-prepare',
    ),
    path(
        'workouts/upload/create/',
        views.create_workout,
        name='workout-upload-create',
    ),
    path('workouts/', views.download_workouts, name='workout-download'),
    path(
        'workouts/high-fives/',
        views.download_workout_high_fives,
        name='workout-high-fives',
    ),
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
