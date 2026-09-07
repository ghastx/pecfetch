"""Test di pecdesk.

È un pacchetto — con il suo ``__init__.py`` — perché la suite di pecfetch ha
già un ``conftest`` e dei ``test_cli``/``test_pipeline``/``test_state``: senza
prefisso di pacchetto i due insiemi si sovrascriverebbero a vicenda.
Si chiama ``desk`` e non ``pecdesk`` per non collidere con il pacchetto vero.
"""
