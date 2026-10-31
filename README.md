# DCAT-AP Cross-Version Validator for EU MQA

Strumento Python per la validazione cross-version dei metadati DCAT-AP dei dataset del catalogo `dati-gov-it` presenti nel piveau Hub di
data.europa.eu. Le validazioni vengono eseguite tramite il validatore SHACL locale di EU MQA (`validating-shacl`); questo progetto ne
orchestra le richieste e raccoglie i risultati.

La pipeline è composta da **tre operazioni indipendenti**, eseguite tramite tre comandi CLI distinti:

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
                                    v
                              analysis.db
```

Il servizio pubblico di data.europa.eu non viene usato per la validazione. La validazione viene eseguita esclusivamente tramite l'istanza
locale di `validating-shacl`.

## Requisiti

- Python 3.11+
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

L’installazione crea il comando `dcat-ap-cv`. In alternativa, è possibile eseguire i comandi tramite `python -m dcat-ap-cv`.

## Comandi

### 1. `categories` e `discover` — categorie e inventario

L'elenco delle categorie non è codificato staticamente: MQA interroga la facet `categories` dell'API Hub Search, filtrando il catalogo
`dati-gov-it`. Le etichette italiane sono usate quando presenti nella risposta; gli identificativi (ad esempio `ENVI`) sono quelli da
passare all'API.

Elenca le categorie disponibili:

```bash
dcat-ap-cv categories
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
dcat-ap-cv discover
```

Esegui discovery per una categoria oppure per più categorie. Puoi indicare l'ID o l'etichetta restituita dall'API; le opzioni `--category` sono ripetibili e accettano anche una lista separata da virgole.

```bash
dcat-ap-cv discover --category ENVI
# equivalente, se l'etichetta è restituita dall'API:
dcat-ap-cv discover --category Ambiente

# più categorie
dcat-ap-cv discover --category ENVI --category ECON
# forma equivalente
dcat-ap-cv discover --category ENVI,ECON
```

Per limitare il numero di dataset in un test:

```bash
dcat-ap-cv discover --category ENVI --limit 20
```

Con una o più categorie, MQA invia una richiesta iniziale a `/search/search` con la facet `categories` e `scroll=true`, quindi recupera i batch successivi tramite `/scroll`. Non usa la paginazione ordinaria con `page` e non supera il limite API di 1000 risultati per risposta. I risultati delle categorie vengono uniti e deduplicati; `--limit`, se specificato, limita il totale raccolto.

Quando vengono selezionate più categorie, il campo `category_id` dell'inventario SQLite contiene gli identificativi selezionati separati da virgola. La discovery non elimina dal database dataset scoperti in precedenza. L'inventario locale può quindi contenere dataset provenienti da più categorie o da discovery precedenti.

La facet può restituire conteggi aggiornati al momento della richiesta: non vanno trattati come una lista immutabile né come una misura stabile del catalogo nel tempo. Per la riproducibilità, registra data/ora, categorie selezionate e limite usato.

### 2. `fetch` — scaricamento RDF

Scarica esclusivamente i dataset presenti nell'inventario locale che non sono ancora stati acquisiti oppure hanno avuto un errore di fetch
precedente.

```bash
python -m dcat-ap-cv fetch --limit 10
```

Senza `--limit` vengono elaborati tutti i dataset attualmente in coda:

```bash
python -m dcat-ap-cv fetch
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
python -m dcat-ap-cv run --limit 10
```

Senza `--limit` vengono elaborati tutti i dataset con RDF disponibile e una matrice di validazione ancora incompleta:

```bash
python -m dcat-ap-cv run
```

Per impostazione predefinita, `run` valida **tutti i profili dichiarati dal validator**, anche quando un livello precedente restituisce `conforms=false`. Per attivare opzionalmente lo short-circuit:

```bash
python -m dcat-ap-cv run --short-circuit
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

I profili esclusi non vengono pianificati per la validazione e non sono attesi nel completamento dei dataset. Le eventuali righe storiche già presenti nel database rimangono conservate.

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
- nessun livello = Base + vocabolari + proprietà raccomandate + ranges.

Perciò, se ad esempio:

```text
dcatap301level1 -> conforms=false
```

non vengono eseguite:

```text
dcatap301level2
dcatap301level3
dcatap301
```

Nel database vengono comunque registrate come `skipped`, con la motivazione e il profilo che ha bloccato la catena.

Se invece Level 1 restituisce un errore tecnico (HTTP diverso da 200, timeout, report RDF non interpretabile, ecc.), il profilo viene
registrato come `error` e **non** viene considerato equivalente a `conforms=false`. La catena resta quindi incompleta e sarà ritentata in
una successiva esecuzione.

Le versioni senza Level 1/2/3, come i profili storici `orig` o `conventions`, vengono trattate come catene indipendenti e non vengono
collegate artificialmente alle altre.

## Database

`analysis.db` costituisce il punto centrale della pipeline.

La tabella `datasets` contiene l'inventario e lo stato delle acquisizioni/validazioni.

La tabella `validations` contiene il risultato o lo stato di esecuzione per ogni combinazione:

```text
(dataset_id, validator_url, shape_model)
```

Oltre ai dati aggregati del report SHACL, la v0.4.3 conserva il corpo originale e indicizza ogni risultato individuale.

- `validations`: esito aggregato per dataset e profilo, contatori, stato HTTP, errore, checksum SHA-256, Content-Type e formato.
  Il corpo originale della risposta è conservato nella colonna `report_body_gzip` come BLOB compresso con gzip; le dimensioni sono
  ricavabili dal BLOB e dal corpo decompresso, quindi non vengono duplicate in colonne dedicate.
- `validation_results`: una riga per ciascun nodo `sh:ValidationResult`, con focus node, property path, value, source shape, constraint
  component, severity e messaggi. Il campo `properties_json` conserva **tutte le proprietà RDF direttamente associate al risultato**,
  comprese quelle non previste dalle colonne principali, con datatype e language tag dei literal. I risultati vengono ordinati in modo
  deterministico all'estrazione e persistiti in tale ordine; non serve una colonna `result_index`.

Ho scelto un BLOB compresso in `analysis.db` invece di un file per ogni coppia dataset/profilo: con decine di migliaia di dataset, la
soluzione a file separati rischierebbe di creare centinaia di migliaia di piccoli file. Il report completo e i dettagli indicizzati vengono
salvati nella stessa transazione SQLite, mentre gzip limita lo spazio richiesto. Il checksum SHA-256 viene calcolato sul corpo originale
non compresso; i metadati del nodo `sh:ValidationReport` non vengono duplicati in JSON perché sono già presenti nel report originale. Per corpi HTTP d'errore viene salvata la risposta
originale, utile alla diagnosi; gli errori di rete senza risposta non hanno un corpo da conservare. Il checksum SHA-256 si riferisce al
corpo originale non compresso.

La conformità viene determinata dal valore `sh:conforms` restituito dal report SHACL. Un errore HTTP, un errore di rete o un report non
interpretabile non vengono classificati come non-conformità. I livelli più alti saltati a causa di un livello precedente non conforme non
hanno un report prodotto dal validator: sono registrati in `validations` con `execution_status='skipped'`.

### Consultare i risultati individuali

Il comando `reports` stampa i risultati indicizzati in JSON su stdout, senza creare file `.csv` o `.md`:

```bash
# Risultati di un dataset e di uno specifico profilo
python -m dcat-ap-cv reports --dataset-id ID_DATASET --profile dcatap301level1

# Solo violazioni di un profilo
python -m dcat-ap-cv reports --profile dcatap301level1 --severity Violation --limit 200

# Stampa il report RDF originale decompresso su stdout
python -m dcat-ap-cv reports --dataset-id ID_DATASET --profile dcatap301level1 --raw-report
```

L'opzione `--raw-report` richiede entrambi i filtri `--dataset-id` e `--profile`; il corpo viene scritto su stdout, quindi può essere
reindirizzato se serve conservarlo come file, per esempio aggiungendo `> report.ttl` al comando. Il Content-Type originale si trova in
`validations.report_content_type`.

Le colonne comuni sono direttamente interrogabili in SQLite. Esempio: frequenza delle componenti SHACL che generano violazioni per profilo:

```sql
SELECT v.shape_model, r.source_constraint_component, COUNT(*) AS occurrences
FROM validation_results AS r
JOIN validations AS v ON v.id = r.validation_id
WHERE r.severity = 'Violation'
  AND v.execution_status = 'validated'
GROUP BY v.shape_model, r.source_constraint_component
ORDER BY v.shape_model, occurrences DESC;
```

Per confrontare gli stessi problemi tra dataset:

```sql
SELECT v.shape_model, r.result_path, r.source_constraint_component,
       r.severity, COUNT(DISTINCT v.dataset_id) AS datasets, COUNT(*) AS occurrences
FROM validation_results AS r
JOIN validations AS v ON v.id = r.validation_id
GROUP BY v.shape_model, r.result_path, r.source_constraint_component, r.severity
ORDER BY datasets DESC, occurrences DESC;
```

I report delle versioni precedenti non erano stati salvati. Quando la v0.4.3 aggiorna un database precedente, i dataset con validazioni
riuscite ma senza report vengono rimessi in coda `pending`, così `run` può acquisire il corpo originale e i dettagli individuali. I file RDF
già scaricati non vengono riscaricati. Nel database incluso nell’archivio la migrazione è già stata applicata: restano presenti 61.721
dataset e 22 validazioni aggregate precedenti; queste ultime non contengono ancora i nuovi report, per cui il dataset corrispondente è già
in coda per una nuova validazione.

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

Le tre fasi sono completamente separate:

```bash
dcat-ap-cv discover
dcat-ap-cv fetch --limit 100
dcat-ap-cv run --limit 100
```

È quindi possibile interrompere una fase e riprenderla successivamente senza ripetere le altre.

La selezione di `fetch` e `run` è deterministica e utilizza la `position` memorizzata nell'inventario, con `dataset_id` come criterio
secondario.

## Output del progetto

Il progetto non genera report `.csv` o `.md`.

Gli output persistenti sono:

- `analysis.db` — inventario e risultati/stati delle validazioni;
- `data/rdf/*.ttl` — copie locali dei metadata RDF recuperati dal piveau Hub.

Il database è quindi la fonte strutturata dei risultati dell'esperimento.

## Nota metodologica

La metodologia MQA distingue esplicitamente quattro configurazioni SHACL: Level 1 (Base), Level 2 (Base + vocabolari), Level 3 (Base +
vocabolari + proprietà raccomandate) e All shapes (con anche i ranges). Questa struttura motiva l'opzione `--short-circuit`, disattivata per impostazione predefinita; non è una prova che il servizio MQA stesso interrompa le validazioni successive.

Il progetto non scarica direttamente i metadata da `dati.gov.it`. I metadata sono quelli del catalogo `dati-gov-it` presenti nel piveau Hub
di data.europa.eu.

La discovery utilizza l'operazione catalog-specifica di Hub Search per ottenere l'insieme degli ID. La fase `fetch` recupera successivamente
il RDF di ciascun dataset dall'Hub Repo, mentre `run` sottopone lo stesso RDF al validator SHACL locale per i `shapeModel` effettivamente
dichiarati dal suo OpenAPI.


## Analisi statistica del database

Il modulo `src.analysis` legge `analysis.db` in sola lettura e produce riepiloghi CSV e grafici PNG, senza modificare il database. Richiede `matplotlib` (incluso tra le dipendenze del progetto).

```bash
python -m src.analysis
```

Percorsi personalizzati:

```bash
python -m src.analysis --db /percorso/analysis.db --out risultati-analisi
```

Output: `profile_summary.csv` e `profile_conformance.png` (conformità per profilo), `version_transitions.csv` e `version_transitions.png` (transizioni tra versioni DCAT-AP consecutive), `shacl_issues.csv` (frequenza delle issue per shape, componente e percorso). La conformità è calcolata solo sulle validazioni completate con HTTP 200 e `conforms` valorizzato; righe `error` e `skipped` non entrano nel denominatore. Il confronto tra versioni usa esclusivamente profili base `dcatap<versione>`, escludendo i profili `level1/2/3`.
