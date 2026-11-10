"""Live updates: one Server-Sent Events stream per browser tab, fed by Postgres LISTEN/NOTIFY.

Code calls :func:`iki_server.realtime.events.publish` in the transaction of
the change; every web pod's :class:`~iki_server.realtime.hub.Hub` receives it
on commit and passes it to the streams of the people it's for. See ADR 0030.
"""
