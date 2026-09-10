"""pecfetch - estrazione read-only di caselle PEC verso dati strutturati."""

#: Fonte unica del numero di versione: lo leggono pecdesk, `pyproject.toml`
#: (con `[tool.setuptools.dynamic]`) e ogni `messaggio.json` prodotto, nel campo
#: `generato_da`. Un numero scritto in tre posti è un numero che diverge.
#:
#: 1.1.0 cambia il contratto di output: schema a `/indice/2` e `/messaggio/2`,
#: date rese tutte nel fuso dichiarato, indirizzi normalizzati in minuscolo,
#: permessi dell'albero dichiarati e guardia sullo spazio.
__version__ = "1.1.0"
