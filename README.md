# MQA Cross-Version Analysis

Analisi della conformità cross-version DCAT-AP dei metadati del catalogo
`dati-gov-it` così come presenti nel piveau Hub di data.europa.eu.

Per default il progetto usa direttamente il **servizio SHACL pubblico di
data.europa.eu**:

```text
https://data.europa.eu/api/mqa/shacl/validation/report/
```

Non è quindi necessario installare `validating-shacl` localmente.

## Requisiti

- Python 3.11+
- accesso Internet verso `data.europa.eu`

## Installazione

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Primo test consigliato

Eseguire inizialmente un campione piccolo:

```bash
python -m mqa_cv run --limit 10
```

La pipeline esegue:

```text
data.europa.eu Hub
       |
       v
 discovery
       |
       v
 fetch RDF
       |
       v
 data.europa.eu SHACL API
       |
       v
 SQLite + reports
```

## Comandi

Solo discovery:

```bash
python -m mqa_cv discover --limit 100
```

Fetch dei metadati già scoperti:

```bash
python -m mqa_cv fetch
```

Validazione cross-version con il servizio UE:

```bash
python -m mqa_cv validate
```

Validazione di tutti i profili incorporati:

```bash
python -m mqa_cv validate --all-profiles
```

Validazione di profili espliciti:

```bash
python -m mqa_cv validate \
  --profiles dcatap211level1,dcatap300level1,dcatap301level1
```

Rigenerazione dei report:

```bash
python -m mqa_cv report
```

## Validatore locale opzionale

Il backend resta configurabile. Per confrontare il servizio UE con una tua istanza
locale puoi eseguire:

```bash
python -m mqa_cv validate \
  --validator http://127.0.0.1:8080/shacl/validation/report
```

Il database distingue i risultati tramite la tripla:

```text
(dataset_id, shape_model, validator_url)
```

quindi risultati locali e UE possono convivere nello stesso `analysis.db` senza
essere confusi. I report vengono raggruppati per endpoint del validatore.

## Profili

Il progetto contiene una lista incorporata dei profili DCAT-AP usati per la
cross-version analysis. Questo elimina la dipendenza da un checkout locale di
`validating-shacl`.

Se vuoi invece ricavare i profili da uno specifico checkout/versione del server:

```bash
python -m mqa_cv validate \
  --openapi /opt/mqa/validating-shacl/src/main/resources/webroot/openapi-shacl.yaml
```

## Rate limiting e retry

Il servizio UE è pubblico. Il client inserisce per default un intervallo di 0,25 s
tra le richieste e ritenta automaticamente le risposte 429 e alcuni errori 5xx.

Puoi aumentare l'intervallo:

```bash
python -m mqa_cv validate --request-delay 1.0
```

Per un'analisi dell'intero catalogo, il numero di richieste può diventare molto
elevato (`dataset × profili`). Il servizio pubblico è appropriato per campioni e
verifiche comparative; per batch molto grandi è metodologicamente preferibile usare
un'istanza locale della stessa release/configurazione oppure definire esplicitamente
una politica di carico compatibile con il servizio pubblico.

## Output

- `analysis.db`: SQLite con dataset, validazioni e provenienza del validatore
- `data/rdf/*.ttl`: grafi RDF recuperati dal piveau Hub
- `reports/datasets/*.md`: report per dataset
- `reports/summary.csv`: aggregazione per validatore e profilo
- `reports/summary.md`: report generale

## Nota metodologica

Il risultato `sh:conforms` del report RDF è la fonte autorevole per la conformità.
`sh:Violation`, `sh:Warning` e `sh:Info` vengono conteggiati separatamente.

La selezione cross-version predefinita usa i profili `level1` quando disponibili.
Per le versioni storiche 1.x la comparabilità metodologica degli shape set deve
essere trattata separatamente e non viene assunta automaticamente.
