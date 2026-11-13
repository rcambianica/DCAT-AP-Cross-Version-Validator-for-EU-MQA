# DCAT-AP Cross-Version Validator for EU MQA

**Versione: 1.0.0**

Strumento Python per la validazione cross-version dei metadati DCAT-AP dei dataset del catalogo `dati-gov-it` presenti nel piveau Hub di
data.europa.eu. Le validazioni vengono eseguite tramite il validatore SHACL locale di EU MQA (`validating-shacl`); questo progetto ne
orchestra le richieste e raccoglie i risultati.

La pipeline separa **discovery, acquisizione RDF e validazione SHACL** in fasi indipendenti, esposte attraverso domini e sottocomandi della stessa CLI gerarchica:

```text
                 data.europa.eu / piveau Hub
                         |
              +----------+----------+
              |                     |
         Hub Search             Hub Repo
              |                     |
              v                     v
          discover                fetch
              |                     |
              v                     v
        SQLite inventory       data/rdf/*.ttl
                                    |
                                    v
                                  run
                                    |
                                    v
                         SHACL validator LOCALE
                                    |
                   +----------------+----------------+
                   |                                 |
                   v                                 v
              SQLite                              PyOxigraph
         datasets + validations             named graph per report
```

Il servizio pubblico di data.europa.eu non viene usato per la validazione. La validazione viene eseguita esclusivamente tramite l'istanza
locale di `validating-shacl`.

## Requisiti

- Python 3.10+
- accesso Internet verso `data.europa.eu` per discovery e fetch
- un'istanza locale di `validating-shacl` (disponibile su https://gitlab.com/dataeuropa/mqa/validating-shacl) raggiungibile sulla porta
  configurata

## Installazione

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

L’installazione crea il comando `dcat-ap-cv`. In alternativa, dalla directory del progetto è possibile usare `python -m src`: entrambi gli entry point invocano la stessa CLI gerarchica. I moduli applicativi non espongono CLI indipendenti.

## CLI gerarchica

Tutte le funzionalità condividono la grammatica `dcat-ap-cv <dominio> <comando> [opzioni]`. I domini separano le responsabilità operative: `catalogue` per Hub Search, `datasets` per l'acquisizione RDF, `validation` per l'esecuzione SHACL, `reports` per la consultazione dei report, `query` per SPARQL e `analysis` per le elaborazioni quantitative e qualitative. Ogni dominio richiede un sottocomando esplicito.

I sottocomandi disponibili sono `catalogue categories`, `catalogue discover`, `datasets fetch`, `validation run`, `reports show`, `query sparql` e `analysis quantitative|qualitative|all`. Non sono previsti comandi piatti alternativi né un entry point separato per l'analisi.

## Moduli Python

| Modulo | Responsabilità |
|---|---|
| `src/__main__.py` | Delega a `src.cli.main()` per l'entry point `python -m src`. |
| `src/cli.py` | Definisce la gerarchia CLI e coordina discovery, fetch, validazione, consultazione dei report, query e analisi. |
| `src/config.py` | Centralizza endpoint, timeout, percorsi e override tramite variabili d'ambiente. |
| `src/hub.py` | Incapsula le chiamate a Hub Search e Hub Repo. |
| `src/profiles.py` | Scopre e filtra gli `shapeModel` dal file OpenAPI locale e costruisce l'ordine di validazione. |
| `src/validator.py` | Chiama il validator SHACL locale e interpreta la risposta, preservando il payload RDF del report. |
| `src/db.py` | Gestisce inventario e metadati operativi in SQLite e coordina la persistenza recuperabile dei report. |
| `src/report_store.py` | Gestisce i named graph PyOxigraph, il catalogo RDF dei report e le query/serializzazioni. |
| `src/analysis.py` | Implementa l'analisi quantitativa su SQLite e l'estrazione/aggregazione qualitativa da PyOxigraph. Espone `load_qualitative_results()` per i moduli analitici downstream. |

I moduli di dominio non definiscono parser CLI autonomi. Le responsabilità di HTTP, profili, validazione e persistenza restano separate dall'orchestrazione in `cli.py`.

## Comandi

### 1. `categories` e `discover` — categorie e inventario

L'elenco delle categorie non è codificato staticamente: MQA interroga la facet `categories` dell'API Hub Search, filtrando il catalogo
`dati-gov-it`. Le etichette italiane sono usate quando presenti nella risposta; gli identificativi (ad esempio `ENVI`) sono quelli da
passare all'API.

Elenca le categorie disponibili:

```bash
dcat-ap-cv catalogue categories
```

Esempio di output:

```text
ID       DATASET COUNT   CATEGORY
ENVI     2979            Ambiente
ECON     5981            Economia e finanze
...
```

Esegui discovery sull'intero catalogo: l'endpoint `/search/datasets` restituisce l'elenco degli ID in una singola richiesta.

```bash
dcat-ap-cv catalogue discover
```

Esegui discovery per una categoria oppure per più categorie. Puoi indicare l'ID o l'etichetta restituita dall'API; le opzioni `--category` sono ripetibili e accettano anche una lista separata da virgole.

```bash
dcat-ap-cv catalogue discover --category ENVI
# equivalente, se l'etichetta è restituita dall'API:
dcat-ap-cv catalogue discover --category Ambiente

# più categorie
dcat-ap-cv catalogue discover --category ENVI --category ECON
# forma equivalente
dcat-ap-cv catalogue discover --category ENVI,ECON
```

Per limitare il numero di dataset in un test:

```bash
dcat-ap-cv catalogue discover --category ENVI --limit 20
```

Con una o più categorie, MQA invia una richiesta iniziale a `/search/search` con la facet `categories` e `scroll=true`, quindi recupera i batch successivi tramite `/scroll`. Non usa la paginazione ordinaria con `page` e non supera il limite API di 1000 risultati per risposta. I risultati delle categorie vengono uniti e deduplicati; `--limit`, se specificato, limita il totale raccolto.

Quando vengono selezionate più categorie, il campo `category_id` dell'inventario SQLite contiene gli identificativi selezionati separati da virgola. La discovery non elimina dal database dataset scoperti in precedenza. L'inventario locale può quindi contenere dataset provenienti da più categorie o da discovery precedenti.

La facet può restituire conteggi aggiornati al momento della richiesta: non vanno trattati come una lista immutabile né come una misura stabile del catalogo nel tempo. Per la riproducibilità, registra data/ora, categorie selezionate e limite usato.

### 2. `fetch` — scaricamento RDF

Scarica esclusivamente i dataset presenti nell'inventario locale che non sono ancora stati acquisiti oppure hanno avuto un errore di fetch
precedente.

```bash
dcat-ap-cv datasets fetch --limit 10
```

Senza `--limit` vengono elaborati tutti i dataset attualmente in coda:

```bash
dcat-ap-cv datasets fetch
```

`fetch` non esegue discovery e non esegue validazioni.

Il recupero RDF usa l’endpoint corrente del Repo API `GET /api/hub/repo/resources/{resourceId}`. La richiesta resta separata per ciascun dataset, così gli errori e i tentativi di recupero sono isolati.

I file RDF vengono salvati in:

```text
data/rdf/
```

### 3. `run` — validazione SHACL

Valida esclusivamente i dataset RDF già scaricati tramite il validator SHACL locale.

```bash
dcat-ap-cv validation run --limit 10
```

Senza `--limit` vengono elaborati tutti i dataset con RDF disponibile e una matrice di validazione ancora incompleta:

```bash
dcat-ap-cv validation run
```

Per impostazione predefinita, `run` valida **tutti i profili dichiarati dal validator**, anche quando un livello precedente restituisce `conforms=false`. Per attivare opzionalmente lo short-circuit:

```bash
dcat-ap-cv validation run --short-circuit
```

Con `--short-circuit`, all'interno delle catene Level 1 → Level 2 → Level 3 → profilo completo, i livelli successivi vengono registrati come `skipped` dopo il primo risultato non conforme. La modalità completa predefinita considera invece tali record `skipped` come ancora da validare, così un dataset già elaborato con lo short-circuit viene ripreso ed eseguito su tutti i profili.

`run` non esegue discovery e non scarica dataset.

#### Esecuzione sequenziale

La validazione è deliberatamente sequenziale: MQA elabora un dataset alla volta e invia una richiesta HTTP alla volta al servizio SHACL.
All'interno di ciascuna catena, i profili Level 1 → Level 2 → Level 3 → profilo completo sono eseguiti in sequenza. Solo con l'opzione
`--short-circuit`, se un livello restituisce `conforms=false`, i livelli successivi della stessa catena vengono registrati come `skipped`;
la modalità predefinita esegue ogni profilo indipendentemente dall'esito di quelli precedenti.

Non esiste più il parametro `--workers` né la variabile `MQA_VALIDATION_WORKERS`. Questa scelta riduce la complessità della pipeline e
facilita la descrizione e la riproducibilità degli esperimenti. Non implica che l'esecuzione sequenziale sia più veloce: i benchmark
precedenti non hanno mostrato vantaggi consistenti dalla concorrenza nella configurazione testata.

I log riportano avanzamento, tempo trascorso e stima del tempo residuo. Per confrontare esperimenti diversi, usa copie dello stesso database
iniziale e gli stessi file RDF, profili SHACL e versione dell'immagine del validatore.

#### Configurazione del validator Docker

Il client Python può generare concorrenza, ma il servizio SHACL deve essere configurato per ricevere richieste contemporanee. Il codice di
`validator-shacl` non viene modificato da questo progetto. Eventuali prove sulle variabili d'ambiente del container devono essere registrate
separatamente, mantenendo invariata l'immagine upstream e modificando solo parametri documentati e verificati. Poiché MQA invia una
richiesta alla volta, aumentare le istanze lato server non produce necessariamente un vantaggio; serve una misurazione controllata per
dimostrarlo.

L'esecuzione tramite JAR e quella tramite Docker usano lo stesso servizio JVM: passare al JAR, da solo, non rende la validazione più veloce.
Per questa tesi, il servizio upstream va mantenuto invariato: le sue variabili d’ambiente possono essere oggetto di esperimenti separati,
purché siano documentate e i risultati siano misurati. La pipeline MQA non assume che una modifica del parallelismo lato server migliori il
throughput.

## Profili SHACL: unica fonte locale di verità

La lista dei profili utilizzati da `run` viene letta esclusivamente dal file `openapi-shacl.yaml` presente nel checkout locale di `validating-shacl`. Il progetto non mantiene una lista incorporata di profili, non recupera l'OpenAPI via HTTP e non utilizza fallback: in questo modo la matrice di validazione riflette il file effettivamente presente nell'installazione locale del validator.

Per indicare il percorso del file:

```bash
export MQA_SHACL_OPENAPI=/percorso/assoluto/validating-shacl/src/main/resources/webroot/openapi-shacl.yaml
```

Se la variabile non è impostata, il programma cerca `openapi-shacl.yaml` nella directory di lavoro corrente. Se il file manca, non è leggibile, contiene YAML non valido o non presenta profili `shapeModel` validi, il caricamento termina con errore: non viene usata una lista alternativa.

Dal parametro `shapeModel` dell'OpenAPI vengono estratti i valori `enum`; sono quindi applicati questi filtri, senza distinzione tra maiuscole e minuscole:

- esclusione degli identificativi contenenti `de`, per escludere i profili tedeschi;
- esclusione degli identificativi contenenti `orig`, per escludere le varianti storiche richieste;
- esclusione del valore `customized`, che richiede una configurazione aggiuntiva;
- rimozione dei duplicati, preservando l'ordine originale del file.

I profili esclusi non vengono pianificati per la validazione e non sono attesi nel completamento dei dataset. Un nuovo esperimento deve registrare gli identificativi e la versione del file OpenAPI effettivamente utilizzati.

Nei log sono riportati il percorso del file sorgente, il numero dei profili dopo il filtraggio e l'ordine delle catene di validazione. Per riprodurre un esperimento occorre quindi conservare anche la versione del file `openapi-shacl.yaml` utilizzato.

## Validazione intelligente per livelli

Per ogni catena DCAT-AP con Level 1/2/3, `run` ordina le validazioni nel seguente modo:

```text
dcatap301level1 -> dcatap301level2 -> dcatap301level3 -> dcatap301
```

Il principio è quello della metodologia MQA:

- Level 1 = Base;
- Level 2 = Base + vocabolari;
- Level 3 = Base + vocabolari + proprietà raccomandate;
- All shapes (profilo senza suffisso `levelN`) = Base + vocabolari + proprietà raccomandate + ranges.

Solo quando viene specificato `--short-circuit`, se ad esempio:

```text
dcatap301level1 -> conforms=false
```

non vengono eseguite le validazioni successive della stessa catena:

```text
dcatap301level2
dcatap301level3
dcatap301
```

Nel database vengono comunque registrate come `skipped`, con la motivazione e il profilo che ha bloccato la catena. Senza `--short-circuit`, tutti i profili vengono eseguiti indipendentemente dall'esito dei livelli precedenti.

Se invece Level 1 restituisce un errore tecnico (HTTP diverso da 200, timeout, report RDF non interpretabile, ecc.), il profilo viene
registrato come `error` e **non** viene considerato equivalente a `conforms=false`. La catena resta quindi incompleta e sarà ritentata in
una successiva esecuzione.

I profili senza suffisso `levelN` vengono trattati come configurazioni complete e associati alla catena che condivide lo stesso prefisso. I profili contenenti `orig` e quelli tedeschi vengono invece esclusi durante la discovery locale; non entrano nel piano di validazione.

## Persistenza: SQLite e PyOxigraph

La pipeline separa i dati operativi dai report RDF, mantenendo ciascuna tipologia nel sistema più adatto:

- **SQLite (`analysis.db`)** conserva l'inventario dei dataset e i metadati aggregati delle validazioni. Gestisce discovery, fetch, stato delle esecuzioni e validazione incrementale.
- **PyOxigraph (`data/validation-reports/`)** conserva il contenuto RDF completo dei validation report SHACL e un catalogo RDF separato che indicizza i report con i relativi metadati. Ogni report viene inserito in un named graph distinto.

### SQLite

La tabella `datasets` conserva gli identificativi dei dataset, il catalogo, le informazioni di acquisizione RDF e gli stati di fetch e validazione.

La tabella `validations` contiene una riga per ogni combinazione:

```text
(dataset_id, validator_url, shape_model)
```

Registra i metadati operativi e il riepilogo restituito dal validator:

- `conforms`, `results`, `violations`, `warnings`, `infos`;
- `http_status`, `error`, `execution_status`, `validated_at`;
- `report_graph_iri`, `report_sha256`, `report_content_type`, `report_format`;
- `report_status`, `report_triples`, `report_saved_at`.

SQLite **non conserva più il corpo del report né una tabella `validation_results`**. Non vengono salvate rappresentazioni JSON o relazionali dei singoli `sh:ValidationResult`: questi rimangono nel report RDF originale, interrogabile tramite SPARQL.

`report_graph_iri` collega la riga SQLite al named graph Oxigraph. L'IRI è costruito utilizzando l'identificativo della validazione e lo SHA-256 del payload RDF originale:

```text
urn:mqa:validation-report:<validation_id>:<sha256>
```

Lo SHA-256 è calcolato sui byte ricevuti dal validator. Il triplestore conserva il grafo RDF, non la serializzazione HTTP byte per byte: prefissi, ordine delle triple e identificatori dei blank node possono cambiare quando il report viene riletto e serializzato. Il contenuto semantico del grafo viene mantenuto.

### PyOxigraph e named graph

Il percorso predefinito dello store è `data/validation-reports/`; può essere modificato tramite:

```bash
export MQA_REPORT_STORE_PATH=/percorso/assoluto/validation-reports
```

Ogni risposta RDF interpretabile viene caricata nel named graph identificato da `report_graph_iri`. Il caricamento usa il parser PyOxigraph e rinomina i blank node, perché le loro etichette sono locali al documento e non devono collidere tra report differenti. Il parser rifiuta serializzazioni RDF dataset con named graph incorporati: il validation report atteso è un singolo grafo RDF.

Esempio di interrogazione globale dei risultati SHACL:

```sparql
PREFIX sh: <http://www.w3.org/ns/shacl#>

SELECT ?severity ?component (COUNT(?result) AS ?occurrences)
WHERE {
  GRAPH ?report {
    ?result a sh:ValidationResult ;
            sh:resultSeverity ?severity ;
            sh:sourceConstraintComponent ?component .
  }
}
GROUP BY ?severity ?component
ORDER BY DESC(?occurrences)
```

### Catalogo RDF dei validation report

Per rendere i report interrogabili direttamente tramite SPARQL, PyOxigraph mantiene anche un named graph dedicato:

```text
urn:mqa:validation-catalog
```

Il catalogo non duplica né modifica il contenuto dei report. Ogni report è descritto usando come soggetto l'IRI del suo named graph. Il vocabolario applicativo è `urn:mqa:vocab:` (`mqa:`):

| Proprietà | Significato |
|---|---|
| `mqa:validationId` | ID della riga di validazione SQLite |
| `mqa:datasetId` | Identificativo del dataset |
| `mqa:validatorUrl` | Endpoint del validatore |
| `mqa:shapeModel` | Identificativo esatto dello shape model passato al validator |
| `mqa:conforms` | Esito SHACL, quando disponibile |
| `mqa:results`, `mqa:violations`, `mqa:warnings`, `mqa:infos` | Conteggi riepilogativi |
| `mqa:reportSha256` | SHA-256 dei byte RDF originali ricevuti |
| `mqa:reportFormat`, `mqa:reportContentType` | Formato RDF rilevato e Content-Type HTTP |
| `mqa:reportTriples` | Numero di triple nel named graph del report |
| `mqa:storedAt` | Timestamp UTC di indicizzazione nel catalogo |

`mqa:shapeModel` è intenzionalmente un literal: conserva esattamente l'identificativo usato nell'invocazione del validator, senza assumere che sia un IRI o doverne derivare una versione. Non viene utilizzato `sh:sourceShape` per identificare il modello: tale proprietà individua lo shape che ha prodotto un singolo risultato e può riferirsi a blank node locali al report.

Esempio: conteggio dei risultati per shape model:

```sparql
PREFIX mqa: <urn:mqa:vocab:>
PREFIX sh:  <http://www.w3.org/ns/shacl#>

SELECT ?shapeModel (COUNT(DISTINCT ?result) AS ?results)
WHERE {
  GRAPH <urn:mqa:validation-catalog> {
    ?report a mqa:ValidationReport ; mqa:shapeModel ?shapeModel .
  }
  GRAPH ?report { ?result a sh:ValidationResult . }
}
GROUP BY ?shapeModel
ORDER BY ?shapeModel
```

Le query SPARQL pronte sono disponibili nella directory `queries/SPARQL/`:

- `queries/SPARQL/01-reports-per-shape-model.sparql`: numero di report per modello;
- `queries/SPARQL/02-results-per-shape-model.sparql`: numero totale di `sh:ValidationResult`;
- `queries/SPARQL/03-severity-per-shape-model.sparql`: distribuzione per severità;
- `queries/SPARQL/04-violations-per-shape-model.sparql`: sole `sh:Violation`;
- `queries/SPARQL/05-constraint-components-per-shape-model.sparql`: risultati per componente SHACL;
- `queries/SPARQL/06-result-paths-per-shape-model.sparql`: risultati per `sh:resultPath`;
- `queries/SPARQL/07-reports-for-shape-model.sparql`: elenco report e metadati per un modello (sostituire il literal di esempio);
- `queries/SPARQL/08-reports-for-dataset.sparql`: metadati dei report associati a un dataset (sostituire `DATASET_ID`);
- `queries/SPARQL/09-report-graphs-for-dataset.sparql`: triple dei named graph associati a un dataset (sostituire `DATASET_ID`);
- `queries/SPARQL/10-analysis-report-catalog.sparql`: query effettivamente utilizzata da `analysis.py` per leggere il catalogo dei report.

Le query possono essere eseguite direttamente tramite `ValidationReportStore.query()` oppure tramite un endpoint SPARQL collegato allo stesso store. I percorsi SHACL complessi e i blank node di `sh:resultPath` non devono essere interpretati come identificatori stabili tra report differenti.

È disponibile il sottocomando `query sparql`, che apre il triplestore in sola lettura ed esegue query `SELECT` o `ASK` senza modificare SQLite:

```bash
# Esegue una query salvata nel repository
dcat-ap-cv query sparql --query-file queries/SPARQL/04-violations-per-shape-model.sparql

# Query inline, output JSON
dcat-ap-cv query sparql --format json --query \
  'PREFIX mqa: <urn:mqa:vocab:> SELECT ?report WHERE { GRAPH <urn:mqa:validation-catalog> { ?report mqa:shapeModel "dcatap301level1" } }'
```

L'output predefinito delle query `SELECT` è TSV; `--format json` produce un array di oggetti. Per query `CONSTRUCT`/`DESCRIBE` usare l'API `ValidationReportStore.query()` o un endpoint Oxigraph Server/YASGUI.

### Coerenza tra i due sistemi

SQLite e PyOxigraph non condividono una transazione distribuita. La pipeline gestisce perciò la persistenza in quattro passaggi recuperabili:

1. registra in SQLite l'esito e `report_status='pending'`;
2. carica il report nel named graph e verifica che contenga triple;
3. registra nel named graph `urn:mqa:validation-catalog` i metadati del report, incluso lo `shape_model`;
4. aggiorna SQLite con IRI, hash, numero di triple e `report_status='stored'`.

Una validazione è considerata completata dalla coda incrementale soltanto se è `validated`, ha HTTP 200, `conforms` valorizzato e un report con `report_status='stored'` e `report_graph_iri` presente. Un'interruzione tra i passaggi lascia quindi la validazione da riprovare, anziché considerarla completa senza report.

Se viene ripetuta la validazione della stessa combinazione dataset/validator/profilo, il record SQLite viene aggiornato e il nuovo report viene memorizzato con un IRI basato sul suo hash. Dopo che il nuovo grafo e i relativi metadati sono stati registrati, il precedente grafo non più referenziato e la sua voce nel catalogo vengono rimossi.

### Sottocomando `reports show`

Il sottocomando `reports show` recupera i metadati da SQLite e i singoli risultati direttamente dal named graph Oxigraph; l'output JSON è costruito al momento e non viene persistito:

```bash
# Risultati di un dataset e di uno specifico profilo
dcat-ap-cv reports show --dataset-id ID_DATASET --profile dcatap301level1

# Solo violation di un profilo
dcat-ap-cv reports show --profile dcatap301level1 --severity Violation --limit 200

# Serializza il grafo conservato su stdout
dcat-ap-cv reports show --dataset-id ID_DATASET --profile dcatap301level1 --raw-report
```

`--raw-report` richiede `--dataset-id` e `--profile`. Il report viene serializzato dal triplestore nel formato registrato in `report_format`: non è una copia byte per byte della risposta HTTP originale. Il checksum dei byte originali è disponibile in `report_sha256`.

### Ripartenza da zero e migrazione

Per iniziare un nuovo esperimento senza alcun dato precedente, eliminare entrambi gli archivi persistenti e, se si vuole ripetere anche l'acquisizione dei metadata, la cache RDF:

```bash
rm -f analysis.db analysis.db-wal analysis.db-shm
rm -rf data/validation-reports data/rdf
```

Successivamente eseguire `discover`, `fetch` e `run` nell'ordine descritto sopra. SQLite e PyOxigraph vengono creati automaticamente.

Se viene avviata la nuova versione su un vecchio `analysis.db`, `init_schema()` elimina le precedenti tabelle `validations` e `validation_results` e svuota anche `datasets`: i dati del vecchio esperimento non vengono migrati. I file RDF già scaricati non vengono cancellati automaticamente; per una ripartenza completamente pulita, compresa la cache RDF, usare i comandi precedenti. In seguito eseguire nuovamente `discover`, `fetch` e `run`.

## Stati

### Fetch

```text
pending     -> dataset da scaricare
ok          -> RDF scaricato correttamente
fetch_error -> errore durante lo scaricamento
```

### Validation

```text
pending          -> RDF disponibile ma mai validato
validating       -> validazione in corso
completed        -> tutte le catene/profili attesi sono terminali
validation_error -> una o più operazioni di validazione devono essere ritentate
```

### Stato dei singoli profili

```text
validated -> il validator ha prodotto un report interpretabile
skipped   -> il profilo non è stato eseguito perché il precedente livello della stessa catena era non conforme
error     -> impossibile ottenere un esito di conformità
```

## Validator locale

Per default:

```text
http://localhost:8080/shacl/validation/report
```

È possibile sovrascrivere il percorso OpenAPI senza modificare il codice:

```bash
export MQA_SHACL_OPENAPI=/percorso/assoluto/openapi-shacl.yaml
```

### Verifica manuale di un profilo

Per isolare completamente eventuali problemi del progetto dal validator, la stessa richiesta costruita dal client può essere eseguita
direttamente con `curl`:

```bash
curl -sS \\
  -o /tmp/report.ttl \\
  -w 'HTTP=%{http_code} TIME=%{time_total}s\\n' \\
  -X POST \\
  'http://localhost:8080/shacl/validation/report?shapeModel=dcatap200' \\
  -H 'Content-Type: text/turtle' \\
  -H 'Accept: text/turtle' \\
  --data-binary '@/percorso/dataset.ttl'
```

Il progetto invia lo stesso metodo `POST`, lo stesso parametro `shapeModel` e lo stesso `Content-Type`. Se il validator restituisce:

```text
HTTP 400: Could not initialize class io.piveau.validating.DCATAP200
```

il riferimento alla classe Kotlin/Java `io.piveau.validating.DCATAP200` indica un errore durante l'inizializzazione interna del validator,
non un errore generato dal client Python. In questo caso il test con `curl` costituisce la verifica decisiva.

L'OpenAPI pubblica corrente del servizio elenca comunque `dcatap200` tra i valori ammessi di `shapeModel`, mentre il progetto
`validating-shacl` documenta che le shape devono essere associate nel codice del servizio e registrate anche nell'OpenAPI. Questo rende
particolarmente plausibile una discrepanza interna all'istanza del validator se `dcatap200` è esposto dall'OpenAPI ma fallisce a runtime. La
versione effettivamente installata sulla macchina locale resta quella da verificare.

## Esecuzione tipica

Le fasi operative sono completamente separate:

```bash
dcat-ap-cv catalogue discover
dcat-ap-cv datasets fetch --limit 100
dcat-ap-cv validation run --limit 100
```

È quindi possibile interrompere una fase e riprenderla successivamente senza ripetere le altre.

La selezione di `fetch` e `run` è deterministica e utilizza la `position` memorizzata nell'inventario, con `dataset_id` come criterio
secondario.

## Output del progetto

Gli output della pipeline sono:

- `analysis.db` — inventario dei dataset e metadati operativi/aggregati delle validazioni;
- `data/validation-reports/` — database persistente PyOxigraph con un named graph per ogni report SHACL;
- `data/rdf/*.ttl` — copie locali dei metadata RDF recuperati dal piveau Hub;
- `analysis-output/` — CSV e grafici PNG prodotti su richiesta dal modulo di analisi.

SQLite e PyOxigraph costituiscono insieme la persistenza dell'esperimento. Gli output di analisi sono derivati e possono essere rigenerati in qualsiasi momento.

## Nota metodologica

La metodologia MQA distingue esplicitamente quattro configurazioni SHACL: Level 1 (Base), Level 2 (Base + vocabolari), Level 3 (Base +
vocabolari + proprietà raccomandate) e All shapes (con anche i ranges). Questa struttura motiva l'opzione `--short-circuit`, disattivata per impostazione predefinita; non è una prova che il servizio MQA stesso interrompa le validazioni successive.

Il progetto non scarica direttamente i metadata da `dati.gov.it`. I metadata sono quelli del catalogo `dati-gov-it` presenti nel piveau Hub
di data.europa.eu.

La discovery utilizza l'operazione catalog-specifica di Hub Search per ottenere l'insieme degli ID. La fase `fetch` recupera successivamente
il RDF di ciascun dataset dall'Hub Repo, mentre `run` sottopone lo stesso RDF al validator SHACL locale per i `shapeModel` effettivamente
dichiarati dal suo OpenAPI.


## Analisi quantitativa e qualitativa

L'analisi è esposta esclusivamente tramite il dominio CLI `analysis`. I due rami hanno sorgenti indipendenti:

- **Quantitativa — SQLite.** Legge inventario, stato di fetch, stato delle esecuzioni e conteggi aggregati delle validazioni. Include solo le validazioni con `execution_status='validated'`, HTTP 200, `conforms` valorizzato e nessun errore. Non usa i singoli `sh:ValidationResult` come fonte quantitativa e non modifica il database.
- **Qualitativa — PyOxigraph.** Legge il catalogo RDF e i named graph dei report SHACL. Estrae i singoli `sh:ValidationResult` in memoria e genera CSV aggregati. Non legge SQLite e non modifica il triplestore.

Esecuzione di entrambe le analisi:

```bash
dcat-ap-cv analysis all
```

Solo analisi quantitativa, con filtro sul catalogo e senza grafici:

```bash
dcat-ap-cv analysis quantitative \
  --db /percorso/analysis.db \
  --catalogue dati-gov-it \
  --category INTR \
  --no-charts \
  --out risultati-analisi
```

Solo analisi qualitativa, limitata a un modello e a una severità:

```bash
dcat-ap-cv analysis qualitative \
  --store /percorso/validation-reports \
  --shape-model dcatap301level1 \
  --severity Violation \
  --out risultati-analisi
```

I percorsi predefiniti sono `analysis.db` e `data/validation-reports/`. Possono essere sovrascritti rispettivamente con `--db` e `--store`, oppure tramite `MQA_DB_PATH` e `MQA_REPORT_STORE_PATH`. La directory di output predefinita è `analysis-output/`. Il comando `analysis all` salva i risultati dei due rami in sottodirectory distinte (`quantitative/` e `qualitative/`) e scrive un riepilogo comune in `analysis_summary.json`.

### Output quantitativi

Il modulo genera i seguenti CSV:

- `dataset_fetch_status.csv`: numero di dataset per stato di acquisizione RDF.
- `validation_execution_status.csv`: conteggio delle righe di validazione per stato operativo, HTTP status ed eventuale errore.
- `profile_summary.csv`: validazioni, dataset, conformità, conteggi e percentuali di violation, warning e info, statistiche descrittive e distribuzione degli esiti per shape model. Le classi di esito sono mutuamente esclusive: conformi (`conforms=1`), non conformi con almeno una violation (`conforms=0` e `violations>0`) e non conformi senza violation (`conforms=0` e `violations=0`). Quest'ultima classe comprende i casi con soli warning, sole info o entrambi.
- `validation_distribution.csv`: conteggi individuali per dataset e shape model.
- `paired_version_deltas.csv`: variazioni individuali di violation e warning tra versioni adiacenti fra quelle presenti nel campione, a parità di dataset, validator e livello. Questo CSV mantiene il confronto generale tra versioni con lo stesso livello.
- `paired_version_summary.csv`: numero di dataset con conteggi aumentati, diminuiti o invariati per coppia di versioni disponibili e livello.
- `sequence_profile_counts.csv`: conteggi totali di violation e warning per ciascun profilo delle cinque sequenze comparative, insieme al numero di validazioni e dataset inclusi.

Con i grafici abilitati e dati disponibili vengono generati i seguenti PNG. I tre grafici aggregati per ShapeModel (`validation_outcomes.png`, `severity_totals.png` e `result_distributions.png`) condividono un ordine canonico esplicito, applicato alle righe complete dei dati prima del disegno: le etichette non vengono riordinate separatamente dai valori.

```text
dcatap11 -> dcatap12 -> dcatap121 -> dcatap201
-> dcatap210level1 -> dcatap210
-> dcatap211level1 -> dcatap211level2 -> dcatap211level3 -> dcatap211
-> dcatap300level1 -> dcatap300level2 -> dcatap300level3 -> dcatap300
-> dcatap301level1 -> dcatap301level2 -> dcatap301level3 -> dcatap301
```


1. `validation_outcomes.png`: distribuzione percentuale delle validazioni conformi (verde), non conformi con almeno una violation (rosso) e non conformi senza violation (giallo).
2. `severity_totals.png`: conteggio complessivo per shape model, con violation in rosso, warning in arancione e info in giallo.
3. `result_distributions.png`: boxplot dei conteggi di violation e warning per validazione e shape model. I cerchi rappresentano gli outlier oltre i baffi del boxplot (regola predefinita: 1,5 × IQR), non valori fuori da un intervallo di media.
4. `profile_severity_trends_dcatap301.png`, `profile_severity_trends_dcatap300.png`, `profile_severity_trends_dcatap211.png`, `profile_severity_trends_dcatap210.png` e `profile_severity_trends_legacy.png`: cinque grafici a linee distinti. In ciascuno, l'asse x riporta i profili della sequenza nell'ordine indicato e l'asse y riporta il totale delle violazioni (pannello superiore, rosso) o dei warning (pannello inferiore, arancione) nelle validazioni riuscite presenti in SQLite. Le etichette mostrano anche il numero di validazioni incluse (`n`), perché il totale può risentire di coperture differenti tra profili. Se un profilo non ha validazioni riuscite, il relativo valore resta mancante e la linea si interrompe in quel punto.

`quantitative_summary.json` registra sorgente, filtri, dimensione del campione, validazioni incluse/escluse, confronti appaiati e grafici generati. `--no-charts` disabilita soltanto la produzione PNG, non i CSV.

Le query SQL sono disponibili in `queries/SQL/`. I file `01-general-stats-per-shape-model.sql` e `02-avg-violations-and-warning-per-dataset.sql` sono query esplorative di riepilogo. I file `03-datasets-in-scope.sql`, `04-validation-operational-status.sql`, `05-successful-validations.sql` e `06-total-validation-rows.sql` sono invece le query operative caricate direttamente da `analysis.py`. Queste ultime usano i parametri nominati `:catalogue_id` e `:category_id`; impostando un parametro a `NULL` il relativo filtro viene disabilitato. La query `05` applica la stessa condizione di inclusione del ramo quantitativo (`execution_status='validated'`, HTTP 200, `conforms` valorizzato e nessun errore). Non è richiesto che il report RDF sia presente in Oxigraph, perché i conteggi quantitativi provengono da SQLite.

### Output qualitativi

Il ramo qualitativo genera:

- `qualitative_report_inventory.csv`: report presenti nel catalogo RDF e relativi metadati.
- `qualitative_results.csv`: una riga per ogni `sh:ValidationResult`, con severità, componente, percorso, messaggio, focus node, valore, shape e constraint sorgente.
- `qualitative_profile_summary.csv`: risultati aggregati per shape model e severità.
- `qualitative_components.csv`: frequenza delle componenti SHACL, report e dataset interessati.
- `qualitative_paths.csv`: frequenza dei percorsi SHACL risolti e classificati.
- `qualitative_messages.csv`: raggruppamento dei messaggi tramite firma testuale normalizzata.
- `qualitative_focus_nodes.csv`: frequenza dei focus node.
- `qualitative_structural_patterns.csv`: aggregazione per componente, tipo/percorso e severità.
- `qualitative_report_errors.csv`: report presenti nel catalogo ma non leggibili.

`qualitative_summary.json` riporta report selezionati, report letti/non leggibili, shape model, risultati estratti e filtri applicati. `--shape-model` e `--severity` sono ripetibili.

### Percorsi SHACL

Il valore di `sh:resultPath` non è sempre una proprietà RDF semplice. SHACL consente percorsi composti o inversi rappresentati mediante blank node e liste RDF. L'analisi classifica quindi i percorsi come:

- `simple_iri`: percorso costituito da un IRI diretto;
- `complex_resolved`: percorso complesso ricostruito dal grafo RDF;
- `complex_unresolved`: percorso complesso non ricostruibile;
- `missing` o `unclassified`: percorso assente o termine non classificabile.

I blank node vengono interpretati esclusivamente all'interno del report di appartenenza. Le loro etichette non sono identificatori stabili e non vengono usate per creare categorie confrontabili tra report.

### Confine per il clustering semantico

L'estrazione qualitativa espone anche la funzione Python `src.analysis.load_qualitative_results()`. Questa funzione restituisce report, risultati individuali ed eventuali errori di lettura senza scrivere file o modificare Oxigraph. Costituisce il punto di ingresso comune per le elaborazioni successive che operano sui singoli risultati SHACL.

Ogni riga contiene il campo `embedding_text`, composto da componente SHACL, espressione leggibile del percorso e messaggio di validazione. Sono esclusi da questo testo gli identificativi di dataset, lo shape model, la severità, i focus node e i valori RDF specifici: tali campi restano metadati utilizzabili per filtrare e interpretare i cluster, ma non influenzano direttamente la similarità testuale. Il campo `result_node` è locale al named graph e deve essere associato almeno a `report_graph_iri` per ricostruire il risultato d'origine.

Il successivo modulo di clustering potrà riutilizzare questa funzione, generare embedding semantici e applicare HDBSCAN senza duplicare l'accesso a PyOxigraph o la logica di estrazione SHACL. La futura interfaccia CLI appartiene allo stesso dominio `analysis` (ad esempio `analysis clusters`); le dipendenze di embedding e clustering saranno introdotte soltanto con quel modulo. In questa versione non è ancora eseguito alcun clustering e non sono generate etichette di cluster.

### Interpretazione e limiti

Le versioni DCAT-AP sono confrontate soltanto a parità di configurazione: `level1`, `level2`, `level3` oppure `all`. Un identificativo senza suffisso `levelN` rappresenta la configurazione **All shapes** e viene classificato come `all`, non come `base`. I confronti appaiati richiedono lo stesso `dataset_id`, lo stesso `validator_url` e una validazione conclusa in entrambe le versioni.

Un aumento dei risultati SHACL non dimostra da solo un peggioramento intrinseco dei metadata: tra versioni possono cambiare gli shape e i vincoli applicati. Occorrenze e diffusione tra dataset sono misure differenti e devono essere interpretate separatamente. Inoltre, `conforms=false` non equivale necessariamente alla presenza di `sh:Violation`: un report contenente soltanto warning può comunque risultare non conforme.
