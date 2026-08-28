from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('workout_manager', '0001_initial')]

    operations = [
        migrations.CreateModel(
            name='WorkoutDetail',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('objectKey', models.CharField(max_length=700, unique=True)),
                ('contentHash', models.CharField(max_length=64)),
                ('formatVersion', models.PositiveSmallIntegerField(default=1)),
                ('routePointCount', models.PositiveIntegerField(default=0)),
                ('heartRateSampleCount', models.PositiveIntegerField(default=0)),
                ('fileSize', models.PositiveBigIntegerField(default=0)),
                ('updatedAt', models.DateTimeField(auto_now=True)),
                ('workout', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='detail', to='workout_manager.workout')),
            ],
        ),
    ]
