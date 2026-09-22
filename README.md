# MQA Cross-Version Analysis

Pipeline per l'analisi cross-version della conformità DCAT-AP dei metadata presenti nel piveau Hub di data.europa.eu.

## Architettura

```text
Piveau Hub Search API
        |
        | POST /search
        | filtro catalogue + dataset
        v
   dataset IDs
        |
        v
Piveau Hub Repo API
        |
        | GET /datasets/{id}
        | RDF via content negotiation
        v
    RDF metadata
        |
        v
validating-shacl LOCALE
        |
        v
SQLite + reports
```

Il progetto **non interroga Elasticsearch direttamente**. La ricerca è effettuata tramite la Search API di Piveau Hub, che espone l'indice di ricerca come API pubblica. Il Repo API viene poi usato separatamente per ottenere il grafo RDF del dataset. Questa separazione evita di confondere il documento indicizzato con il metadata RDF usato dalla validazione.

Le API ufficiali descrivono `/search/search` come endpoint POST della Search API, con filtri per tipo di risorsa, facets per catalogo, paginazione e altri parametri. Il Repo API espone `GET /datasets/{datasetId}` per recuperare il grafo RDF del dataset. Gli endpoint legacy esistono ancora, ma sono marcati deprecated nella documentazione corrente.

## Configurazione predefinita

```text
search = https://data.europa.eu/api/hub/search
repo   = https://data.europa.eu/api/hub/repo
catalogue = dati-lombardia-it
validator = http://192.168.122.221:8080/shacl/validation/report
```

Modificare `catalogue_id` in `mqa_cv/config.py` se il catalogo target cambia.

## Acquisizione

La discovery usa la Search API con un payload equivalente a:

```json
{
  "q": "",
  "filters": ["dataset"],
  "facets": {"catalog": ["dati-lombardia-it"]},
  "page": 0,
  "limit": 100,
  "includes": ["id"],
  "aggregation": false,
  "scroll": false,
  "searchAfter": false
}
```

Gli ID ottenuti non vengono trasformati in URL di download arbitrari. Per ogni ID il client costruisce il corrispondente endpoint Repo:

```text
GET https://data.europa.eu/api/hub/repo/datasets/{id}
```

La risposta RDF viene normalizzata localmente in Turtle prima di essere salvata e passata al validator. In questo modo il formato usato dal validator è sempre `text/turtle`, anche se il Repo API restituisce RDF/XML o un altro formato RDF.

## Comandi

```bash
python -m mqa_cv discover --limit 1
```

Cerca un dataset tramite Search API senza scaricarlo.

```bash
python -m mqa_cv fetch --limit 1
```

Cerca un dataset e scarica il relativo RDF tramite Repo API.

```bash
python -m mqa_cv validate --limit 1 --profiles dcatap300level1
```

Valida il primo dataset già scaricato.

```bash
python -m mqa_cv validate --all-profiles --limit 1
```

Valida il primo dataset contro tutti i profili disponibili nell'OpenAPI del validator locale.

```bash
python -m mqa_cv run --limit 1
```

Esegue l'intera pipeline: discovery, fetch, validazione e report.

```bash
python -m mqa_cv report
```

Rigenera i report senza effettuare nuove richieste HTTP.

## Profili SHACL

Quando disponibile, viene letto l'`openapi-shacl.yaml` della stessa installazione locale di `validating-shacl`.

```bash
python -m mqa_cv validate \
  --openapi /opt/mqa/validating-shacl/src/main/resources/webroot/openapi-shacl.yaml \
  --all-profiles --limit 1
```

Oppure:

```bash
export MQA_SHACL_OPENAPI=/opt/mqa/validating-shacl/src/main/resources/webroot/openapi-shacl.yaml
```

Se non viene trovato un OpenAPI locale, il progetto usa una lista fallback incorporata.

## Validator locale

Default:

```text
http://192.168.122.221:8080/shacl/validation/report
```

Per evitare attese apparentemente indefinite:

```text
validator timeout = 30 s
validator retries = 0
```

Gli errori del validator non vengono conteggiati come non-compliance:

```text
HTTP 200 + sh:conforms=true  -> COMPLIANT
HTTP 200 + sh:conforms=false -> NON_COMPLIANT
HTTP 4xx/5xx                 -> VALIDATOR_ERROR
errore di rete               -> VALIDATOR_ERROR
```

`sh:conforms` rimane la fonte autorevole dell'esito.

## Output

- `analysis.db`: dataset, acquisizione e risultati SHACL
- `data/rdf/*.ttl`: RDF normalizzato recuperato dal Repo API
- `reports/datasets/*.md`: report per dataset
- `reports/summary.csv`: aggregazione per validator e shapeModel
- `reports/summary.md`: riepilogo generale

## Nota metodologica

La sorgente dei metadata e il motore di validazione sono volutamente separati:

```text
metadata: data.europa.eu / piveau Hub
validator: validating-shacl locale
```

Questo permette di studiare l'effetto dei diversi shapeModel mantenendo costante il grafo RDF analizzato.
