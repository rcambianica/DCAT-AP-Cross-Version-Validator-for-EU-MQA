# MQA Cross-Version Analysis

Analisi della conformità cross-version DCAT-AP dei metadati del catalogo `dati-gov-it` così come presenti nel piveau Hub di data.europa.eu.

La pipeline separa volutamente **sorgente dei metadata** e **validator SHACL**:

```text
data.europa.eu / piveau Hub
          |
          v
    discovery dataset
          |
          v
       fetch RDF
          |
          v
  validating-shacl LOCALE
          |
          v
      SQLite + reports
```

Per default il progetto usa il validator locale:

```text
http://192.168.122.221:8080/shacl/validation/report
```

Il servizio pubblico di data.europa.eu non viene usato per la validazione, ma il piveau Hub europeo continua a essere usato per recuperare i
metadata del catalogo `dati-gov-it`.

## Requisiti

- Python 3.11+
- accesso Internet verso `data.europa.eu` per discovery/fetch dei metadata
- un'istanza locale di `validating-shacl` raggiungibile sulla porta 8080

## Installazione

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Test minimo consigliato

Prima verificare direttamente il validator locale con un TTL già disponibile:

```bash
curl -sS \
  -o /tmp/report.ttl \
  -w 'HTTP %{http_code}\n' \
  -X POST \
  'http://192.168.122.221:8080/shacl/validation/report?shapeModel=dcatap300level1' \
  -H 'Content-Type: text/turtle' \
  -H 'Accept: text/turtle' \
  --data-binary '@/home/riccardo/bologna.ttl'
```

Se il validator risponde correttamente, il test del progetto può essere eseguito con un solo dataset:

```bash
python -m mqa_cv run --limit 1
```

`--limit 1` ora limita **l'intera pipeline**: discovery, fetch e validation. Non verranno quindi processati eventuali dataset rimasti nel
database da esecuzioni precedenti.

## Profili locali

Quando possibile il progetto usa l'`openapi-shacl.yaml` della stessa installazione locale di `validating-shacl`, così la lista degli
`shapeModel` è coerente con la versione realmente in esecuzione.

È possibile indicare esplicitamente il file:

```bash
python -m mqa_cv validate \
  --openapi /opt/mqa/validating-shacl/src/main/resources/webroot/openapi-shacl.yaml
```

Oppure tramite variabile d'ambiente:

```bash
export MQA_SHACL_OPENAPI=/opt/mqa/validating-shacl/src/main/resources/webroot/openapi-shacl.yaml
```

Se `--openapi` non viene specificato, vengono cercati automaticamente alcuni percorsi standard. Se nessuno esiste, viene usata una lista
incorporata di fallback. Il programma stampa sempre quale sorgente dei profili sta utilizzando.

## Pipeline incrementale

Il database mantiene un inventario persistente degli ID. `discover` aggiorna l'inventario senza scaricare RDF; `run` aggiorna automaticamente l'inventario e poi lavora sui dataset ancora da acquisire/validare. Gli ID sono ordinati localmente in modo deterministico per `dataset_id` crescente. Gli ID già presenti non vengono duplicati e i loro stati non vengono azzerati.

### Aggiornare soltanto l'inventario

```bash
python -m mqa_cv discover
```

Per un test limitato: `python -m mqa_cv discover --limit 10`. Il limite limita gli ID aggiunti dalla scansione, non effettua fetch o validazioni.

### Run incrementale completa

```bash
python -m mqa_cv run --limit 100
```

La `run` esegue in sequenza: (1) discovery/aggiornamento inventario, (2) fetch fino a 100 dataset in coda, (3) validazione fino a 100 dataset con RDF acquisito e validazione in attesa, (4) rigenerazione report. Il limite si applica separatamente alle fasi di fetch e validazione.

Per usare soltanto l'inventario già presente senza interrogare Hub Search: `python -m mqa_cv run --no-discover --limit 100`.

### Comandi disponibili

```bash
python -m mqa_cv discover [--limit N] [--catalogue ID]
python -m mqa_cv run [--limit N] [--catalogue ID] [--no-discover]
```

Gli errori di fetch e validazione rimangono registrati con stati distinti e possono essere ritentati nelle run successive. Un dataset è marcato `completed` soltanto quando tutti i profili configurati hanno restituito un esito interpretabile con HTTP 200.

## Timeout e retry

Per il validator locale i valori predefiniti sono volutamente conservativi:

```text
timeout       = 30 s
request_delay = 0 s
retries       = 0
```

Questo evita che un validator locale non raggiungibile rimanga apparentemente bloccato per diversi minuti a causa di retry e backoff.

I valori possono essere modificati dalla CLI, per esempio:

```bash
python -m mqa_cv validate --timeout 60 --retries 1
```

## Errori del validator

Un errore HTTP o di rete non viene interpretato come non-compliance:

```text
HTTP 200 + sh:conforms=true   -> COMPLIANT
HTTP 200 + sh:conforms=false  -> NON_COMPLIANT
HTTP 4xx/5xx                  -> VALIDATOR_ERROR
errore di rete                -> VALIDATOR_ERROR
report RDF non interpretabile -> VALIDATOR_ERROR
```

La fonte autorevole della conformità è `sh:conforms`; il numero di `sh:Violation` viene registrato separatamente.

## Database

`analysis.db` distingue i risultati mediante:

```text
(dataset_id, shape_model, validator_url)
```

È quindi possibile conservare nello stesso database risultati ottenuti da validator differenti senza confonderli.

## Output

- `analysis.db`: SQLite con dataset, validazioni e provenienza del validator
- `data/rdf/*.ttl`: grafi RDF recuperati dal piveau Hub
- `reports/datasets/*.md`: report per dataset
- `reports/summary.csv`: aggregazione per validator e profilo
- `reports/summary.md`: report generale

## Nota metodologica

Il progetto non scarica direttamente i metadata da `dati.gov.it`. I metadata sono quelli del catalogo `dati-gov-it` presenti nel piveau Hub
di data.europa.eu.

Questa scelta permette di mantenere costante la sorgente RDF mentre si cambia soltanto il validator/shape model. Un eventuale confronto con
il CKAN originale di `dati.gov.it` costituisce un esperimento separato.


### Recupero completo degli ID tramite Scroll API

La discovery usa la modalità scroll di Hub Search. La prima richiesta GET a `/search` applica il filtro `dataset`, il filtro sul catalogo (default `dati-gov-it`), `scroll=true` e `limit=1000`. La risposta contiene il primo batch e uno `scrollId`; le richieste successive interrogano `/scroll?scrollId=...` fino a quando l'API restituisce una pagina vuota. Gli ID vengono deduplicati e inseriti nell'inventario SQLite senza azzerare gli stati dei dataset già presenti.

`page_size` è impostato a 1000 (massimo richiesto dallo script di discovery). `discover --limit N` resta disponibile per i test e interrompe localmente l'estrazione dopo N ID; senza `--limit`, la discovery attraversa lo scroll fino al termine.
