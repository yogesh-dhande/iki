from django.apps import AppConfig


class TasksConfig(AppConfig):
    name = "iki_server.tasks"
    label = "iki_tasks"
    verbose_name = "Iki background tasks"
