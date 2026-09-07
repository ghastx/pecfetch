"""pecdesk — smistamento della coda prodotta da pecfetch.

pecfetch acquisisce e non giudica; pecdesk giudica e non tocca le caselle in
ingresso. La coda è il confine: pecdesk legge gli elementi prodotti da pecfetch,
non modifica mai i file di input, e ha configurazione, stato, lock ed esecuzione
propri. Se l'API del modello è irraggiungibile, l'acquisizione della posta
continua indisturbata.

L'obiettivo è la **selezione**, non l'analisi: capire che tipo di documento è
arrivato e dove va, non leggere gli atti.
"""

__version__ = "1.0.0"
