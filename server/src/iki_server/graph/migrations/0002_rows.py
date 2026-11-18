from django.db import migrations

from iki_server.graph import sql


class Migration(migrations.Migration):
    dependencies = [("iki_graph", "0001_edges")]

    operations = [migrations.RunSQL(sql.ROWS, sql.DROP_ROWS)]
