# Generated by Django 5.0.2 on 2024-04-02 16:33

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("submissions", "0009_alter_submissiondata_score_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="submissiondata",
            name="status",
            field=models.CharField(blank=True, max_length=10000, null=True),
        ),
    ]
