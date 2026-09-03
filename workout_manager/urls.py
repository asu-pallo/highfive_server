from django.urls import path

from . import views


urlpatterns = [
    path('workouts/upload/', views.upload_workout, name='workout-upload'),
    path('workouts/', views.download_workouts, name='workout-download'),
    path(
        'workouts/encounters/',
        views.download_workout_encounters,
        name='workout-encounters',
    ),
    path(
        'workouts/<int:workout_id>/detail/',
        views.download_workout_detail,
        name='workout-detail',
    ),
    path(
        'workouts/<int:workout_id>/statistics/',
        views.download_workout_statistics,
        name='workout-statistics',
    ),
    path(
        'workouts/<int:workout_id>/statistics/comparison/',
        views.download_workout_comparison,
        name='workout-comparison',
    ),
    path(
        'workouts/<int:workout_id>/encounters/candidates/',
        views.download_encounter_candidates,
        name='workout-encounter-candidates',
    ),
    path(
        'workouts/<int:workout_id>/encounters/distribution/',
        views.download_encounter_distribution,
        name='workout-encounter-distribution',
    ),
    path(
        'encounters/users/<int:user_id>/familiarity/',
        views.refresh_encounter_familiarity,
        name='encounter-familiarity',
    ),
    path(
        'users/<int:user_id>/weekly-workout-stats/',
        views.download_user_weekly_workout_stats,
        name='user-weekly-workout-stats',
    ),
    path(
        'workouts/<int:workout_id>/high-fives/',
        views.create_high_five,
        name='workout-high-five-create',
    ),
    path(
        'workouts/<int:workout_id>/h3/',
        views.download_workout_h3,
        name='workout-h3',
    ),
]
